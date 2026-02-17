"""
RSI (Relative Strength Index) indicator.

Measures momentum by comparing average gains vs average losses.
Range: 0 to 100. Above 70 = overbought, below 30 = oversold.

Uses Wilder's smoothing (exponential moving average) to match
TradingView and most charting platforms.

Wilder's formula:
  1. Seed: first Avg Gain/Loss = SMA of first `period` changes
  2. Then:  Avg Gain = (prev_Avg_Gain * (period-1) + current_gain) / period
            Avg Loss = (prev_Avg_Loss * (period-1) + current_loss) / period
  3. RS  = Avg Gain / Avg Loss
  4. RSI = 100 - (100 / (1 + RS))
"""

import pandas as pd
import numpy as np
from indicators.base import Indicator


class RSI(Indicator):
    """RSI using Wilder's smoothing (matches TradingView)."""

    def __init__(self, name: str, period: int = 14, **kwargs):
        super().__init__(name, period=period)
        self.period = period

    def calculate(self, close: pd.Series, volume: pd.Series = None) -> pd.Series:
        """
        Calculate RSI with Wilder's smoothing.

        First `period` rows will be NaN (not enough data to seed).

        Args:
            close: option close prices, sorted chronologically

        Returns:
            pd.Series of RSI values (0-100)
        """
        p = self.period
        closes = close.values
        n = len(closes)

        # Output array, default NaN
        rsi = np.full(n, np.nan)

        if n < p + 1:
            # Not enough data to even seed the first average
            return pd.Series(rsi, index=close.index)

        # Price changes (first element is NaN since no previous bar)
        delta = np.diff(closes, prepend=np.nan)
        gains = np.maximum(delta, 0)
        losses = np.maximum(-delta, 0)

        # Step 1: Seed with SMA of first `period` changes
        # (changes at indices 1..period, since index 0 has no diff)
        avg_gain = np.mean(gains[1:p + 1])
        avg_loss = np.mean(losses[1:p + 1])

        # RSI at the seed bar
        if avg_loss == 0:
            rsi[p] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[p] = 100 - (100 / (1 + rs))

        # Step 2: Wilder's recursive smoothing for remaining bars
        for i in range(p + 1, n):
            avg_gain = (avg_gain * (p - 1) + gains[i]) / p
            avg_loss = (avg_loss * (p - 1) + losses[i]) / p

            if avg_loss == 0:
                rsi[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i] = 100 - (100 / (1 + rs))

        return pd.Series(rsi, index=close.index)
