"""
WMA (Weighted Moving Average) indicator.

Linear-weighted moving average matching TradingView's `ta.wma`. Weights rise
linearly from 1 (oldest value in the window) to `period` (most recent value),
so recent prices dominate. Denominator is the triangular number
period*(period+1)/2.

  WMA = (1*p[-n+1] + 2*p[-n+2] + ... + n*p[-1]) / (1+2+...+n)

First `period - 1` rows are NaN (not enough data to fill the window).
"""

import numpy as np
import pandas as pd
from indicators.base import Indicator


class WMA(Indicator):
    """Linear weighted moving average (matches TradingView ta.wma)."""

    def __init__(self, name: str, period: int = 44, **kwargs):
        super().__init__(name, period=period)
        self.period = period

    def calculate(self, close: pd.Series, volume: pd.Series = None) -> pd.Series:
        """
        Calculate WMA with linearly increasing weights (newest = `period`).

        Args:
            close: price series, sorted chronologically (oldest first)

        Returns:
            pd.Series of WMA values aligned to `close.index`
        """
        n = self.period
        weights = np.arange(1, n + 1, dtype=float)  # oldest->1 ... newest->n
        wsum = weights.sum()

        def _wma(window: np.ndarray) -> float:
            # rolling passes the window oldest-first, so weights align directly.
            return float(np.dot(window, weights) / wsum)

        return close.rolling(window=n, min_periods=n).apply(_wma, raw=True)
