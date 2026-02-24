"""
Low-level REST API wrapper for Dhan.

Handles rate limiting, request formatting, and response parsing.
All methods return raw parsed dictionaries — business logic is
in the other datafeed submodules.
"""

import time
import logging
from typing import Optional, Dict

from dhanhq import dhanhq

logger = logging.getLogger(__name__)


class DhanRestClient:
    """
    Thin wrapper around the dhanhq SDK with rate limiting.

    Stores the raw Dhan client instance and credentials.
    All higher-level modules (option_chain, security_map) receive
    this client instead of creating their own.
    """

    # Dhan API limits: 5 data calls/second, 10 order calls/second
    DEFAULT_MIN_INTERVAL = 1.5  # seconds between API calls

    def __init__(self, client_id: str, access_token: str,
                 min_api_interval: float = DEFAULT_MIN_INTERVAL):
        self.client_id = client_id
        self.access_token = access_token
        self.dhan = dhanhq(client_id, access_token)

        # Rate limiting
        self._last_api_call = 0.0
        self.min_api_interval = min_api_interval

        logger.info("DhanRestClient initialized")

    def rate_limit(self):
        """Enforce rate limiting — wait if needed."""
        now = time.time()
        elapsed = now - self._last_api_call
        if elapsed < self.min_api_interval:
            time.sleep(self.min_api_interval - elapsed)
        self._last_api_call = time.time()

    # ------------------------------------------
    # MARKET DATA
    # ------------------------------------------
    def ticker_data(self, securities: Dict) -> Optional[dict]:
        """Get LTP via ticker_data API. Returns raw response dict."""
        self.rate_limit()
        try:
            return self.dhan.ticker_data(securities=securities)
        except Exception as e:
            logger.error(f"ticker_data error: {e}")
            return None

    def ohlc_data(self, securities: Dict) -> Optional[dict]:
        """Get OHLC via ohlc_data API. Returns raw response dict."""
        self.rate_limit()
        try:
            return self.dhan.ohlc_data(securities=securities)
        except Exception as e:
            logger.error(f"ohlc_data error: {e}")
            return None

    def intraday_minute_data(self, security_id: str, exchange_segment: str,
                             instrument_type: str, from_date: str,
                             to_date: str) -> Optional[dict]:
        """Get historical 1-min candles. Returns raw response dict."""
        self.rate_limit()
        try:
            return self.dhan.intraday_minute_data(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
                from_date=from_date,
                to_date=to_date,
            )
        except Exception as e:
            logger.error(f"intraday_minute_data error: {e}")
            return None

    def expiry_list(self, under_security_id: int,
                    under_exchange_segment: str) -> Optional[dict]:
        """Get expiry list for an underlying. Returns raw response dict."""
        self.rate_limit()
        try:
            return self.dhan.expiry_list(
                under_security_id=under_security_id,
                under_exchange_segment=under_exchange_segment,
            )
        except Exception as e:
            logger.error(f"expiry_list error: {e}")
            return None

    def option_chain(self, under_security_id: int,
                     under_exchange_segment: str,
                     expiry: str) -> Optional[dict]:
        """Get option chain. Returns raw response dict."""
        self.rate_limit()
        try:
            return self.dhan.option_chain(
                under_security_id=under_security_id,
                under_exchange_segment=under_exchange_segment,
                expiry=expiry,
            )
        except Exception as e:
            logger.error(f"option_chain error: {e}")
            return None

    def fetch_security_list(self, mode: str = 'compact'):
        """Fetch security master list. Returns DataFrame or None."""
        self.rate_limit()
        try:
            return self.dhan.fetch_security_list(mode)
        except Exception as e:
            logger.error(f"fetch_security_list error: {e}")
            return None
