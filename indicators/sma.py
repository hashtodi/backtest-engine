"""
SMA (Simple Moving Average) indicator.

Equal weight to all prices in the lookback window.
Common periods: 10, 20, 50, 100, 200.
"""

import pandas as pd
from indicators.base import Indicator


class SMA(Indicator):
    """Simple Moving Average."""

    def __init__(self, name: str, period: int = 20, **kwargs):
        super().__init__(name, period=period)
        self.period = period

    def calculate(self, close: pd.Series, volume: pd.Series = None) -> pd.Series:
        """
        Calculate SMA.

        First `period - 1` rows will be NaN.

        Args:
            close: option close prices, sorted chronologically

        Returns:
            pd.Series of SMA values
        """
        return close.rolling(window=self.period).mean()
