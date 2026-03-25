"""
RSI MA (Moving Average of RSI) indicator.

Calculates RSI first (Wilder's smoothing), then applies SMA to smooth it.
Useful for detecting RSI trend direction and RSI crossovers.

Params:
  - rsi_period: period for RSI calculation (default 14)
  - ma_period: SMA period applied to RSI values (default 14)
"""

import pandas as pd
from indicators.base import Indicator
from indicators.rsi import RSI


class RSI_MA(Indicator):
    """Simple Moving Average of RSI values."""

    def __init__(self, name: str, rsi_period: int = 14, ma_period: int = 14, **kwargs):
        super().__init__(name, rsi_period=rsi_period, ma_period=ma_period)
        self.rsi_period = rsi_period
        self.ma_period = ma_period

    def calculate(self, close: pd.Series, volume: pd.Series = None) -> pd.Series:
        """
        Calculate SMA of RSI.

        Steps:
          1. Compute RSI (Wilder's) on close prices
          2. Apply SMA with ma_period on the RSI output

        Warmup: first (rsi_period + ma_period - 1) bars will be NaN.

        Args:
            close: price series (typically option close)

        Returns:
            pd.Series of RSI MA values
        """
        # Reuse the existing RSI indicator for step 1
        rsi_ind = RSI(name='_internal_rsi', period=self.rsi_period)
        rsi_values = rsi_ind.calculate(close)

        # Step 2: SMA of RSI
        return rsi_values.rolling(self.ma_period, min_periods=self.ma_period).mean()
