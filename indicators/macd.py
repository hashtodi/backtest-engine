"""
MACD (Moving Average Convergence Divergence) indicator.

Shows relationship between two EMAs.
Default: fast=12, slow=26, signal=9.

Returns 3 series:
  - macd: fast EMA - slow EMA
  - signal: EMA of the MACD line
  - histogram: macd - signal
"""

import pandas as pd
from typing import Dict
from indicators.base import Indicator


class MACD(Indicator):
    """MACD with configurable fast, slow, and signal periods."""

    def __init__(self, name: str, fast: int = 12, slow: int = 26, signal: int = 9, **kwargs):
        super().__init__(name, fast=fast, slow=slow, signal=signal)
        self.fast = fast
        self.slow = slow
        self.signal_period = signal

    def calculate(self, close: pd.Series, volume: pd.Series = None) -> Dict[str, pd.Series]:
        """
        Calculate MACD, signal line, and histogram.

        Args:
            close: option close prices, sorted chronologically

        Returns:
            dict with keys "macd", "signal", "histogram"
        """
        # Fast and slow EMAs
        ema_fast = close.ewm(span=self.fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.slow, adjust=False).mean()

        # MACD line = fast EMA - slow EMA
        macd_line = ema_fast - ema_slow

        # Signal line = EMA of MACD
        signal_line = macd_line.ewm(span=self.signal_period, adjust=False).mean()

        # Histogram = MACD - Signal
        histogram = macd_line - signal_line

        return {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": histogram,
        }
