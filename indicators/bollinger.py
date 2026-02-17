"""
Bollinger Bands indicator.

A volatility band around a moving average.
Default: period=20, std_dev=2.

Returns 3 series:
  - upper: middle + (std_dev * rolling std)
  - middle: SMA of close
  - lower: middle - (std_dev * rolling std)
"""

import pandas as pd
from typing import Dict
from indicators.base import Indicator


class BollingerBands(Indicator):
    """Bollinger Bands with configurable period and std deviation multiplier."""

    def __init__(self, name: str, period: int = 20, std_dev: float = 2.0, **kwargs):
        super().__init__(name, period=period, std_dev=std_dev)
        self.period = period
        self.std_dev = std_dev

    def calculate(self, close: pd.Series, volume: pd.Series = None) -> Dict[str, pd.Series]:
        """
        Calculate upper, middle, and lower Bollinger Bands.

        Args:
            close: option close prices, sorted chronologically

        Returns:
            dict with keys "upper", "middle", "lower"
        """
        # Middle band = SMA
        middle = close.rolling(window=self.period).mean()

        # Standard deviation over the same window
        rolling_std = close.rolling(window=self.period).std()

        # Upper and lower bands
        upper = middle + (self.std_dev * rolling_std)
        lower = middle - (self.std_dev * rolling_std)

        return {
            "upper": upper,
            "middle": middle,
            "lower": lower,
        }
