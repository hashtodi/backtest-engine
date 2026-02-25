"""
Rolling price buffer for spot and option bars.

Maintains:
  - spot_bars: continuous list of (datetime, price) for underlying
  - option_bars: per-contract list of (datetime, close, high, low, open)

Contract identity = strike + option_type (mirrors backtest grouping).
Resets on expiry change (new nearest weekly — resets ALL option buffers).
"""

import logging
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Maximum rolling bars to keep in the price buffer.
# Covers any reasonable indicator period (RSI 100, EMA 200, etc.).
# Multi-day warmup can produce ~375 bars/day × 5 days = ~1875 bars.
# 2500 gives comfortable headroom for a full contract week + live bars.
MAX_BUFFER_BARS = 2500


class PriceBuffer:
    """
    Rolling buffer for spot and option prices.

    Contract identity = strike + option_type (mirrors backtest grouping).
    Resets happen on:
      - ATM strike shift (new strike for that option_type)
      - Expiry change (new nearest weekly — resets ALL option buffers)
    This matches the backtest where contract = strike + option_type +
    expiry_type + expiry_code, and indicators reset per contract.
    """

    def __init__(self):
        self.spot_bars: List[dict] = []  # [{datetime, spot}, ...]

        # Keyed by contract key e.g. "24000_CE"
        self.option_bars: Dict[str, List[dict]] = {}

        # Track current contract key per option type
        self._current_key: Dict[str, str] = {}  # {"CE": "24000_CE", ...}

        # Current expiry string (from API). When this changes, all
        # option buffers reset — same as backtest expiry_code rollover.
        self._current_expiry: Optional[str] = None

    # ------------------------------------------
    # SPOT
    # ------------------------------------------
    def add_spot(self, dt, price: float):
        """Append a spot price bar. Trims to MAX_BUFFER_BARS."""
        self.spot_bars.append({"datetime": dt, "spot": price})
        if len(self.spot_bars) > MAX_BUFFER_BARS:
            self.spot_bars = self.spot_bars[-MAX_BUFFER_BARS:]

    # ------------------------------------------
    # EXPIRY MANAGEMENT
    # ------------------------------------------
    def set_expiry(self, expiry: str):
        """
        Update the current expiry. If it changed, reset ALL option buffers.
        Mirrors backtest where a new expiry_code=1 means a new contract.
        """
        if self._current_expiry is not None and expiry != self._current_expiry:
            logger.info(
                f"Expiry changed: {self._current_expiry} -> {expiry}. "
                f"Resetting all option buffers."
            )
            self.option_bars.clear()
            self._current_key.clear()
        self._current_expiry = expiry

    # ------------------------------------------
    # OPTIONS
    # ------------------------------------------
    def fill_option(self, dt, strike: int, option_type: str, ltp: float,
                    high: float = None, low: float = None,
                    open_price: float = None):
        """
        Silently append a bar to a specific contract's buffer.

        Used during warmup to pre-fill multiple strikes without
        triggering ATM-shift logs or changing _current_key.

        Args:
            dt:          bar timestamp
            strike:      strike price
            option_type: "CE" or "PE"
            ltp:         close price
            high:        optional real high (defaults to ltp)
            low:         optional real low (defaults to ltp)
            open_price:  optional real open (defaults to ltp)
        """
        key = f"{int(strike)}_{option_type}"
        if key not in self.option_bars:
            self.option_bars[key] = []
        self.option_bars[key].append({
            "datetime": dt,
            "close": ltp,
            "high": high if high is not None else ltp,
            "low": low if low is not None else ltp,
            "open": open_price if open_price is not None else ltp,
        })
        if len(self.option_bars[key]) > MAX_BUFFER_BARS:
            self.option_bars[key] = self.option_bars[key][-MAX_BUFFER_BARS:]

    def add_option(self, dt, strike: int, option_type: str, ltp: float,
                   high: float = None, low: float = None,
                   open_price: float = None):
        """
        Append an option price bar (live use).

        Contract key = strike + option_type (like backtest grouping).
        If the ATM strike changed, we switch to that contract's buffer.
        Old bars are PRESERVED — so if ATM oscillates back, indicators
        still have history.

        Args:
            dt:          bar timestamp
            strike:      strike price
            option_type: "CE" or "PE"
            ltp:         close price (last traded price)
            high:        optional real high from candle aggregator
            low:         optional real low from candle aggregator
            open_price:  optional real open from candle aggregator
        """
        key = f"{int(strike)}_{option_type}"
        prev_key = self._current_key.get(option_type)

        # ATM shift — switch to this contract's buffer
        if key != prev_key:
            if prev_key is not None:
                old_bars = len(self.option_bars.get(prev_key, []))
                new_bars = len(self.option_bars.get(key, []))
                logger.info(
                    f"ATM shift for {option_type}: "
                    f"{prev_key} ({old_bars} bars) -> "
                    f"{key} ({new_bars} bars preserved)"
                )
            if key not in self.option_bars:
                self.option_bars[key] = []
            self._current_key[option_type] = key

        self.option_bars[key].append({
            "datetime": dt,
            "close": ltp,
            "high": high if high is not None else ltp,
            "low": low if low is not None else ltp,
            "open": open_price if open_price is not None else ltp,
        })

        # Trim
        if len(self.option_bars[key]) > MAX_BUFFER_BARS:
            self.option_bars[key] = self.option_bars[key][-MAX_BUFFER_BARS:]

    # ------------------------------------------
    # ACCESSORS
    # ------------------------------------------
    def get_spot_series(self) -> pd.Series:
        """Return spot prices as a pandas Series (for indicator calc)."""
        if not self.spot_bars:
            return pd.Series(dtype=float)
        return pd.Series([b["spot"] for b in self.spot_bars])

    def get_option_series(self, option_type: str) -> pd.Series:
        """Return option close prices for the current contract of this type."""
        key = self._current_key.get(option_type)
        if not key or key not in self.option_bars:
            return pd.Series(dtype=float)
        return pd.Series([b["close"] for b in self.option_bars[key]])

    def get_option_high_series(self, option_type: str) -> pd.Series:
        """Return option high prices for the current contract of this type."""
        key = self._current_key.get(option_type)
        if not key or key not in self.option_bars:
            return pd.Series(dtype=float)
        return pd.Series([b["high"] for b in self.option_bars[key]])

    def get_option_low_series(self, option_type: str) -> pd.Series:
        """Return option low prices for the current contract of this type."""
        key = self._current_key.get(option_type)
        if not key or key not in self.option_bars:
            return pd.Series(dtype=float)
        return pd.Series([b["low"] for b in self.option_bars[key]])

    def get_option_bar(self, option_type: str, offset: int = -1) -> Optional[dict]:
        """Get a specific option bar (default: latest)."""
        key = self._current_key.get(option_type)
        if not key or key not in self.option_bars:
            return None
        bars = self.option_bars[key]
        if not bars:
            return None
        try:
            return bars[offset]
        except IndexError:
            return None

    def get_current_key(self, option_type: str) -> Optional[str]:
        """Return current contract key for an option type."""
        return self._current_key.get(option_type)

    def bar_count(self, option_type: str) -> int:
        """Number of bars buffered for the current contract of this type."""
        key = self._current_key.get(option_type)
        if not key or key not in self.option_bars:
            return 0
        return len(self.option_bars[key])
