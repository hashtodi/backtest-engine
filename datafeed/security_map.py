"""
Security ID mapping and lookup.

Loads the Dhan security master list on demand and provides
cached lookups for option contract security IDs.
"""

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class SecurityMap:
    """
    Lazy-loaded security ID mapper.

    Caches lookups to avoid repeated DataFrame scans.
    The underlying DataFrame is loaded once from the Dhan API
    on the first lookup.
    """

    def __init__(self, rest_client):
        """
        Args:
            rest_client: DhanRestClient instance
        """
        self._client = rest_client
        self._df = None
        self._loaded = False
        self._cache = {}  # "NIFTY_25000_19-FEB-2026_CE" -> security_id

    def _load(self):
        """Load security list from Dhan API (called once, on demand)."""
        if self._loaded:
            return
        try:
            logger.info("Loading security list from Dhan API...")
            self._df = self._client.fetch_security_list('compact')
            self._loaded = True
            if self._df is not None:
                logger.info(f"Security list loaded: {len(self._df)} instruments")
            else:
                logger.error("Security list returned None")
        except Exception as e:
            logger.error(f"Error loading security list: {e}")
            self._loaded = False

    def get_option_security_id(self, instrument: str, strike: int,
                               expiry: str, option_type: str) -> Optional[int]:
        """
        Get security ID for an option contract.

        Searches Dhan's security list and caches the result.

        Args:
            instrument:  e.g. "NIFTY", "SENSEX"
            strike:      strike price (integer)
            expiry:      DD-MMM-YYYY format (e.g. '19-FEB-2026')
            option_type: 'CE' or 'PE'

        Returns:
            Security ID (int) or None
        """
        cache_key = f"{instrument}_{strike}_{expiry}_{option_type}"

        if cache_key in self._cache:
            return self._cache[cache_key]

        self._load()

        if self._df is None:
            logger.error("Security list not available")
            return None

        try:
            # Convert expiry from DD-MMM-YYYY to YYYY-MM-DD for matching
            expiry_date = datetime.strptime(expiry, '%d-%b-%Y')
            expiry_str = expiry_date.strftime('%Y-%m-%d')

            # Regex ^INSTRUMENT- to prevent "NIFTY" matching "FINNIFTY"
            symbol_pattern = f"^{instrument}-"
            filtered = self._df[
                (self._df['SEM_TRADING_SYMBOL'].str.match(
                    symbol_pattern, na=False, case=False)) &
                (self._df['SEM_STRIKE_PRICE'] == float(strike)) &
                (self._df['SEM_EXPIRY_DATE'].str.contains(
                    expiry_str, na=False)) &
                (self._df['SEM_OPTION_TYPE'] == option_type)
            ]

            if len(filtered) == 0:
                logger.warning(
                    f"No security found for {instrument} {strike} "
                    f"{expiry} {option_type}"
                )
                return None

            # Prefer NSE for NIFTY/BANKNIFTY, BSE for SENSEX
            if len(filtered) > 1:
                preferred_exch = 'BSE' if instrument == 'SENSEX' else 'NSE'
                exch_filtered = filtered[
                    filtered['SEM_EXM_EXCH_ID'] == preferred_exch
                ]
                if len(exch_filtered) > 0:
                    filtered = exch_filtered

            security_id = int(filtered.iloc[0]['SEM_SMST_SECURITY_ID'])

            self._cache[cache_key] = security_id

            logger.info(
                f"Found security ID {security_id} for "
                f"{instrument} {strike} {expiry} {option_type}"
            )
            return security_id

        except Exception as e:
            logger.error(f"Error looking up security ID for {cache_key}: {e}")
            return None
