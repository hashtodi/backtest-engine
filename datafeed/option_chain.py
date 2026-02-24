"""
Option chain, expiry, and ATM strike helpers.

Business logic for option-specific operations:
  - Nearest weekly expiry lookup
  - ATM strike calculation
  - Option price fetching
  - Option chain retrieval
  - Historical option data
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict

import pandas as pd
import pytz

import config

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')


# Security IDs for spot/index instruments (Dhan API)
SECURITY_IDS = {
    'NIFTY': {'exchange': 'IDX_I', 'security_id': 13},
    'SENSEX': {'exchange': 'IDX_I', 'security_id': 51},
    'BANKNIFTY': {'exchange': 'IDX_I', 'security_id': 25},
    'RELIANCE': {'exchange': 'NSE_EQ', 'security_id': 2885},
    'HDFCBANK': {'exchange': 'NSE_EQ', 'security_id': 1333},
}

# Exchange segment mapping for options
OPTION_EXCHANGE_MAP = {
    'NIFTY': 'NSE_FNO',
    'BANKNIFTY': 'NSE_FNO',
    'FINNIFTY': 'NSE_FNO',
    'MIDCPNIFTY': 'NSE_FNO',
    'SENSEX': 'BSE_FNO',
}

# Instrument type mapping for historical data
OPTION_INSTRUMENT_TYPE = {
    'NIFTY': 'OPTIDX',
    'BANKNIFTY': 'OPTIDX',
    'SENSEX': 'OPTIDX',
}


def get_option_exchange(instrument: str) -> str:
    """Return the exchange segment string for an instrument's options."""
    return OPTION_EXCHANGE_MAP.get(instrument, 'NSE_FNO')


def get_option_instrument_type(instrument: str) -> str:
    """Return the instrument type for historical data API."""
    return OPTION_INSTRUMENT_TYPE.get(instrument, 'OPTSTK')


def get_spot_price(rest_client, instrument: str) -> Optional[float]:
    """
    Get current spot price for an instrument via ticker_data API.

    Returns:
        Spot price as float, or None on failure
    """
    security_info = SECURITY_IDS.get(instrument)
    if not security_info:
        logger.error(f"Security ID not found for {instrument}")
        return None

    try:
        response = rest_client.ticker_data(
            securities={
                security_info['exchange']: [security_info['security_id']]
            }
        )

        if response and response.get('status') == 'success':
            outer_data = response.get('data', {})
            inner_data = outer_data.get('data', {})
            exchange_data = inner_data.get(security_info['exchange'], {})
            security_data = exchange_data.get(
                str(security_info['security_id']), {}
            )
            ltp = security_data.get('last_price')
            if ltp:
                return float(ltp)

        logger.warning(f"No LTP data for {instrument}. Response: {response}")
        return None

    except Exception as e:
        logger.error(f"Error fetching spot price for {instrument}: {e}")
        return None


def get_atm_strike(spot_price: float, instrument: str) -> int:
    """Calculate ATM strike based on spot price and rounding interval."""
    rounding = config.STRIKE_ROUNDING.get(instrument, 50)
    return int(round(spot_price / rounding) * rounding)


def get_weekly_expiry(rest_client, instrument: str) -> str:
    """
    Get nearest weekly expiry from the Dhan expiry_list API.

    Equivalent to expiry_code=1 in backtesting.
    No hardcoded weekday fallback — raises RuntimeError on failure.

    Returns:
        Expiry date in DD-MMM-YYYY format (e.g. '20-FEB-2026')
    """
    security_info = SECURITY_IDS.get(instrument)
    if not security_info:
        raise RuntimeError(
            f"Security ID not found for {instrument}. "
            f"Available: {list(SECURITY_IDS.keys())}"
        )

    response = rest_client.expiry_list(
        under_security_id=security_info['security_id'],
        under_exchange_segment=security_info['exchange'],
    )

    if response and response.get('status') == 'success':
        expiry_dates = response.get('data', {}).get('data', [])
        if expiry_dates and len(expiry_dates) > 0:
            next_expiry = expiry_dates[0]
            expiry_date = datetime.strptime(next_expiry, '%Y-%m-%d')
            formatted = expiry_date.strftime('%d-%b-%Y').upper()
            logger.info(
                f"Nearest expiry for {instrument}: {formatted} "
                f"(from API, {len(expiry_dates)} expiries available)"
            )
            return formatted

    raise RuntimeError(
        f"Could not fetch nearest expiry for {instrument}. "
        f"Response: {response}"
    )


def get_option_price(rest_client, security_map, instrument: str,
                     strike: int, expiry: str,
                     option_type: str) -> Optional[Dict]:
    """
    Get option OHLC + LTP data via ohlc_data API.

    Returns dict with ltp, high, low, open, or None.
    """
    security_id = security_map.get_option_security_id(
        instrument, strike, expiry, option_type
    )
    if not security_id:
        return None

    exchange = get_option_exchange(instrument)

    try:
        response = rest_client.ohlc_data(
            securities={exchange: [security_id]}
        )

        if response and response.get('status') == 'success':
            outer_data = response.get('data', {})
            inner_data = outer_data.get('data', {})
            exchange_data = inner_data.get(exchange, {})
            security_data = exchange_data.get(str(security_id), {})

            if security_data:
                ohlc = security_data.get('ohlc', {})
                ltp = security_data.get('last_price', 0)
                return {
                    'ltp': float(ltp) if ltp else 0.0,
                    'high': float(ohlc.get('high', 0)),
                    'low': float(ohlc.get('low', 0)),
                    'open': float(ohlc.get('open', 0)),
                    'volume': 0,
                    'oi': 0,
                }

        return None

    except Exception as e:
        logger.error(f"Error fetching option price: {e}", exc_info=True)
        return None


def get_historical_data(rest_client, security_map, instrument: str,
                        strike: int, expiry: str, option_type: str,
                        from_date: str, to_date: str) -> Optional[pd.DataFrame]:
    """
    Get historical intraday 1-min data for an option contract.

    Returns DataFrame with columns: open, high, low, close, volume, timestamp
    """
    security_id = security_map.get_option_security_id(
        instrument, strike, expiry, option_type
    )
    if not security_id:
        return None

    exchange = get_option_exchange(instrument)
    inst_type = get_option_instrument_type(instrument)

    try:
        response = rest_client.intraday_minute_data(
            security_id=str(security_id),
            exchange_segment=exchange,
            instrument_type=inst_type,
            from_date=from_date,
            to_date=to_date,
        )

        if response and response.get('status') == 'success':
            data = response.get('data', [])
            if data:
                df = pd.DataFrame(data)
                if 'start_Time' in df.columns:
                    df.rename(
                        columns={'start_Time': 'timestamp'}, inplace=True
                    )
                if 'timestamp' in df.columns:
                    df['timestamp'] = pd.to_datetime(df['timestamp'])
                return df

        return None

    except Exception as e:
        logger.error(f"Error fetching historical data: {e}")
        return None


def get_index_historical_data(rest_client, instrument: str,
                              from_date: str,
                              to_date: str) -> Optional[pd.DataFrame]:
    """
    Get historical intraday 1-min data for an index (spot).

    Returns DataFrame with columns: open, high, low, close, volume, timestamp
    """
    security_info = SECURITY_IDS.get(instrument)
    if not security_info:
        logger.error(f"Security ID not found for {instrument}")
        return None

    try:
        response = rest_client.intraday_minute_data(
            security_id=str(security_info['security_id']),
            exchange_segment=security_info['exchange'],
            instrument_type='INDEX',
            from_date=from_date,
            to_date=to_date,
        )

        if response and response.get('status') == 'success':
            data = response.get('data', [])
            if data:
                df = pd.DataFrame(data)
                if 'start_Time' in df.columns:
                    df.rename(
                        columns={'start_Time': 'timestamp'}, inplace=True
                    )
                if 'timestamp' in df.columns:
                    df['timestamp'] = pd.to_datetime(df['timestamp'])
                return df

        logger.warning(f"No index historical data for {instrument}")
        return None

    except Exception as e:
        logger.error(f"Error fetching index historical data: {e}")
        return None
