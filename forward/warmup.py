"""
Warmup manager for the forward test engine.

Fetches multi-day historical intraday data (options + spot)
and pre-fills the PriceBuffer so indicators have enough history.
"""

import logging
from datetime import datetime, timedelta
from typing import List

import config
from forward.helpers import IST, make_event, safe_timestamp
from forward.price_buffer import PriceBuffer

logger = logging.getLogger(__name__)

# How far back (calendar days) to fetch intraday data.
# The security_id is tied to the current contract+expiry, so the API
# only returns data from when this contract actually started trading.
# 14 days is generous enough to cover any weekly contract start.
WARMUP_LOOKBACK_DAYS = 14


def run_warmup(data_feed, instrument: str, buffer: PriceBuffer,
               warmup_strike_range: int = 10) -> dict:
    """
    Fetch multi-day intraday data and pre-fill the PriceBuffer.

    Uses a 14-day lookback window. Since each security_id is tied to
    the current contract+expiry, the API naturally returns data only
    from the contract's first trading day — no expiry math needed.

    Warms up ±warmup_strike_range strikes around ATM so that
    indicators have history even if ATM shifts after startup.
    Also warms up spot data for spot-based indicators.

    Args:
        data_feed:            DhanDataFeed instance
        instrument:           e.g. "NIFTY", "SENSEX"
        buffer:               PriceBuffer to fill
        warmup_strike_range:  strikes above/below ATM to warm up

    Returns:
        dict with keys: events (list), spot, atm_strike, expiry
    """
    events: List[dict] = []
    now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")

    # Multi-day: from_date = today - WARMUP_LOOKBACK_DAYS
    lookback_date = (now - timedelta(days=WARMUP_LOOKBACK_DAYS))
    from_date_str = lookback_date.strftime("%Y-%m-%d")

    events.append(make_event(
        "info",
        f"Warmup: fetching intraday data for {instrument} "
        f"({from_date_str} to {today_str})..."
    ))

    # 1. Get current spot and ATM
    spot = data_feed.get_spot_price(instrument)
    if spot is None:
        events.append(make_event(
            "error", "Warmup failed: could not fetch spot price"
        ))
        return {"events": events, "spot": None,
                "atm_strike": None, "expiry": None}

    atm_strike = data_feed.get_atm_strike(spot, instrument)

    # 2. Get nearest expiry from Dhan API (equivalent to expiry_code=1)
    expiry = data_feed.get_weekly_expiry(instrument)

    # Tell the buffer which expiry we're on
    buffer.set_expiry(expiry)

    # 3. Build list of strikes to warm up: ATM ± warmup_strike_range
    step = config.STRIKE_ROUNDING.get(instrument, 100)
    strikes = [
        atm_strike + (i * step)
        for i in range(-warmup_strike_range, warmup_strike_range + 1)
    ]

    events.append(make_event(
        "info",
        f"Warmup: spot={spot:.2f} ATM={atm_strike} expiry={expiry} | "
        f"warming {len(strikes)} strikes "
        f"({strikes[0]} to {strikes[-1]}, step={step}) | "
        f"lookback={from_date_str}"
    ))

    # 4. Fetch multi-day intraday data for each strike's CE and PE
    total_option_bars = 0
    failed_fetches = 0
    earliest_data_date = None

    for strike in strikes:
        for opt_type in ("CE", "PE"):
            try:
                df = data_feed.get_historical_data(
                    instrument, strike, expiry, opt_type,
                    from_date=from_date_str, to_date=today_str,
                )
                if df is not None and len(df) > 0:
                    if 'timestamp' in df.columns:
                        df = df.sort_values('timestamp')
                    bar_count = 0
                    for _, row in df.iterrows():
                        ts = safe_timestamp(row.get('timestamp', now))
                        close = float(row['close'])
                        high = float(row.get('high', close))
                        low = float(row.get('low', close))
                        open_price = float(row.get('open', close))
                        buffer.fill_option(
                            ts, strike, opt_type, close,
                            high=high, low=low, open_price=open_price,
                        )
                        bar_count += 1
                        # Track earliest data date
                        if hasattr(ts, 'date'):
                            d = ts.date()
                        else:
                            d = ts
                        if earliest_data_date is None or d < earliest_data_date:
                            earliest_data_date = d
                    total_option_bars += bar_count
                    logger.debug(
                        f"Warmup: {strike} {opt_type} -> {bar_count} bars"
                    )
                else:
                    logger.debug(
                        f"Warmup: no data for {strike} {opt_type}"
                    )
            except Exception as e:
                failed_fetches += 1
                logger.warning(
                    f"Warmup: {strike} {opt_type} fetch failed: {e}"
                )

    # Set current key to actual ATM (fill_option doesn't touch _current_key)
    buffer._current_key["CE"] = f"{atm_strike}_CE"
    buffer._current_key["PE"] = f"{atm_strike}_PE"
    # Ensure the ATM buffers exist (even if API returned no data)
    if f"{atm_strike}_CE" not in buffer.option_bars:
        buffer.option_bars[f"{atm_strike}_CE"] = []
    if f"{atm_strike}_PE" not in buffer.option_bars:
        buffer.option_bars[f"{atm_strike}_PE"] = []

    # 5. Fetch multi-day spot (index) intraday data
    spot_bars = 0
    try:
        spot_df = data_feed.get_index_historical_data(
            instrument,
            from_date=from_date_str, to_date=today_str,
        )
        if spot_df is not None and len(spot_df) > 0:
            if 'timestamp' in spot_df.columns:
                spot_df = spot_df.sort_values('timestamp')
            for _, row in spot_df.iterrows():
                ts = safe_timestamp(row.get('timestamp', now))
                buffer.add_spot(ts, float(row['close']))
            spot_bars = len(spot_df)
        else:
            logger.warning(
                "Warmup: no spot intraday data "
                "(spot indicators will warm up from live data)"
            )
    except Exception as e:
        logger.warning(
            f"Warmup: spot fetch failed ({e}). "
            f"Spot indicators will warm up from live data."
        )

    # Log ATM buffer sizes for quick verification
    ce_bars = len(buffer.option_bars.get(f"{atm_strike}_CE", []))
    pe_bars = len(buffer.option_bars.get(f"{atm_strike}_PE", []))

    # Build summary with data span info
    date_span = ""
    if earliest_data_date:
        date_span = f" | data from {earliest_data_date}"
        all_dates = set()
        for key, bars in buffer.option_bars.items():
            for bar in bars:
                dt = bar.get("datetime")
                if hasattr(dt, 'date'):
                    all_dates.add(dt.date())
        if all_dates:
            date_span += f" ({len(all_dates)} trading days)"

    summary = (
        f"Warmup done: {len(strikes)} strikes × CE+PE = "
        f"{total_option_bars} option bars, "
        f"spot={spot_bars} bars | "
        f"ATM CE={ce_bars} bars, PE={pe_bars} bars"
        f"{date_span}"
    )
    if failed_fetches > 0:
        summary += f" | {failed_fetches} fetches failed"

    events.append(make_event("info", summary))
    logger.info(summary)

    return {
        "events": events,
        "spot": spot,
        "atm_strike": atm_strike,
        "expiry": expiry,
    }
