"""
Dhan API data feed package.

Provides DhanDataFeed as a drop-in replacement for the old
monolithic dhan_datafeed.py module. The same public interface
is preserved so existing code (forward engine, UI, CLI) works
without changes.

Internally delegates to:
  - rest_client.py   — low-level API wrapper + rate limiting
  - security_map.py  — option security ID lookups
  - option_chain.py  — option chain, expiry, ATM, historical data
"""

import logging
from typing import Dict, List, Optional

import pandas as pd

from datafeed.rest_client import DhanRestClient
from datafeed.security_map import SecurityMap
from datafeed import option_chain as oc

logger = logging.getLogger(__name__)


class DhanDataFeed:
    """
    Facade for Dhan market data.

    Same public interface as the old monolithic dhan_datafeed.DhanDataFeed,
    so all call sites (forward engine, UI, CLI) keep working.
    """

    # Expose SECURITY_IDS at class level for backward compat
    SECURITY_IDS = oc.SECURITY_IDS

    def __init__(self, client_id: str, access_token: str):
        # Store credentials (needed by WebSocket feed)
        self.client_id = client_id
        self.access_token = access_token

        # Internal components
        self._rest = DhanRestClient(client_id, access_token)
        self._security_map = SecurityMap(self._rest)

        # Expose the option_symbols_cache for WebSocket instrument building.
        # This is the same dict that SecurityMap uses internally.
        self.option_symbols_cache = self._security_map._cache

        logger.info("DhanDataFeed initialized (modular)")

    # ------------------------------------------
    # SPOT / INDEX
    # ------------------------------------------
    def get_spot_price(self, instrument: str) -> Optional[float]:
        """Get current spot price for an instrument."""
        return oc.get_spot_price(self._rest, instrument)

    def get_atm_strike(self, spot_price: float, instrument: str) -> int:
        """Calculate ATM strike based on spot price."""
        return oc.get_atm_strike(spot_price, instrument)

    # ------------------------------------------
    # EXPIRY
    # ------------------------------------------
    def get_weekly_expiry(self, instrument: str) -> str:
        """Get nearest weekly expiry (equivalent to expiry_code=1)."""
        return oc.get_weekly_expiry(self._rest, instrument)

    # ------------------------------------------
    # OPTION DATA
    # ------------------------------------------
    def get_option_security_id(self, instrument: str, strike: int,
                               expiry: str,
                               option_type: str) -> Optional[int]:
        """Get security ID for an option contract (cached)."""
        return self._security_map.get_option_security_id(
            instrument, strike, expiry, option_type
        )

    def get_option_price(self, instrument: str, strike: int,
                         expiry: str,
                         option_type: str) -> Optional[Dict]:
        """Get option OHLC + LTP data."""
        return oc.get_option_price(
            self._rest, self._security_map,
            instrument, strike, expiry, option_type,
        )

    def get_option_chain(self, instrument: str, spot_price: float,
                         expiry: str,
                         num_strikes: int = 5) -> Optional[pd.DataFrame]:
        """Get full option chain data."""
        security_info = self.SECURITY_IDS.get(instrument)
        if not security_info:
            return None
        try:
            response = self._rest.option_chain(
                under_security_id=security_info['security_id'],
                under_exchange_segment=security_info['exchange'],
                expiry=expiry,
            )
            if response and response.get('status') == 'success':
                data = response.get('data', [])
                if data:
                    return pd.DataFrame(data)
            return None
        except Exception as e:
            logger.error(f"Error fetching option chain: {e}")
            return None

    # ------------------------------------------
    # HISTORICAL DATA
    # ------------------------------------------
    def get_historical_data(self, instrument: str, strike: int,
                            expiry: str, option_type: str,
                            from_date: str,
                            to_date: str) -> Optional[pd.DataFrame]:
        """Get historical intraday 1-min data for an option contract."""
        return oc.get_historical_data(
            self._rest, self._security_map,
            instrument, strike, expiry, option_type,
            from_date, to_date,
        )

    def get_index_historical_data(self, instrument: str,
                                  from_date: str,
                                  to_date: str) -> Optional[pd.DataFrame]:
        """Get historical intraday 1-min data for an index (spot)."""
        return oc.get_index_historical_data(
            self._rest, instrument, from_date, to_date,
        )
