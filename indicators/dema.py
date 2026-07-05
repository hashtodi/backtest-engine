"""
DEMA (Double Exponential Moving Average) indicator.

Reduces lag compared to standard EMA by applying EMA twice and combining.
Formula: DEMA = 2 * EMA(close, period) - EMA(EMA(close, period), period)

Common periods: 20, 50, 100, 200.
"""

import pandas as pd
from indicators.base import Indicator


class DEMA(Indicator):
    """Double Exponential Moving Average."""

    def __init__(self, name: str, period: int = 200, **kwargs):
        super().__init__(name, period=period)
        self.period = period

    def calculate(self, close: pd.Series, volume: pd.Series = None) -> pd.Series:
        """
        Calculate DEMA.

        Warmup: approximately 2 * period bars before values fully stabilize.

        Args:
            close: close prices, sorted chronologically

        Returns:
            pd.Series of DEMA values
        """
        ema1 = close.ewm(span=self.period, adjust=False).mean()
        ema2 = ema1.ewm(span=self.period, adjust=False).mean()
        return 2 * ema1 - ema2
