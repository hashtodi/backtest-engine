"""
EMA (Exponential Moving Average) indicator.

Gives more weight to recent prices than SMA.
Common periods: 9, 12, 20, 21, 26, 50, 200.
"""

import pandas as pd
from indicators.base import Indicator


class EMA(Indicator):
    """Exponential Moving Average."""

    def __init__(self, name: str, period: int = 20, **kwargs):
        super().__init__(name, period=period)
        self.period = period

    def calculate(self, close: pd.Series, volume: pd.Series = None) -> pd.Series:
        """
        Calculate EMA.

        First `period - 1` rows will be NaN.

        Args:
            close: option close prices, sorted chronologically

        Returns:
            pd.Series of EMA values
        """
        return close.ewm(span=self.period, adjust=False).mean()
