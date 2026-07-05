"""
ADX (Average Directional Index) indicator - Wilder.

A direction-agnostic trend-STRENGTH oscillator (0-100). Low ADX (< 20-25)
means a ranging / choppy market; high ADX (> 25) means a strong trend.

Uses Wilder's RMA smoothing to match TradingView's ta.adx (ta.dmi), seeded
the same way as this package's RSI (SMA of the first `period` values, then the
recursive Wilder update).

Definition (when high/low are provided):
  TR    = max(high - low, |high - close_prev|, |low - close_prev|)
  up    = high - high_prev ;  down = low_prev - low
  +DM   = up   if up > down and up > 0 else 0
  -DM   = down if down > up and down > 0 else 0
  +DI   = 100 * RMA(+DM) / RMA(TR)
  -DI   = 100 * RMA(-DM) / RMA(TR)
  DX    = 100 * |+DI - -DI| / (+DI + -DI)      (sum == 0 -> DX = 0)
  ADX   = RMA(DX)

ADX is undefined without high/low, so the close-only fallback returns NaN.
"""

import numpy as np
import pandas as pd

from indicators.base import Indicator


def _rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's RMA (SMA-seeded recursive smoothing); matches TradingView ta.rma.

    Leading NaNs are skipped so the seed uses the first `period` valid values.
    """
    arr = series.to_numpy(dtype=float)
    n = len(arr)
    out = np.full(n, np.nan)
    valid = ~np.isnan(arr)
    if valid.sum() < period:
        return pd.Series(out, index=series.index)

    first = int(np.argmax(valid))            # first non-NaN index
    seed_end = first + period
    if seed_end > n:
        return pd.Series(out, index=series.index)

    prev = float(np.nanmean(arr[first:seed_end]))
    out[seed_end - 1] = prev
    for i in range(seed_end, n):
        x = arr[i]
        if np.isnan(x):
            x = prev                          # carry forward across gaps
        prev = prev + (x - prev) / period
        out[i] = prev
    return pd.Series(out, index=series.index)


class ADX(Indicator):
    """Average Directional Index using Wilder's smoothing (matches TradingView)."""

    def __init__(self, name: str, period: int = 14, **kwargs):
        super().__init__(name, period=period)
        self.period = period

    def calculate(self, close: pd.Series, volume: pd.Series = None,
                  high: pd.Series = None, low: pd.Series = None) -> pd.Series:
        """Calculate ADX. Returns NaN everywhere if high/low are unavailable."""
        n = len(close)
        if high is None or low is None or len(high) != n or len(low) != n:
            return pd.Series(np.nan, index=close.index)

        h = high.astype(float)
        l = low.astype(float)
        c = close.astype(float)
        prev_c = c.shift(1)

        tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()],
                       axis=1).max(axis=1)
        up = h.diff()
        down = -l.diff()
        plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0),
                            index=close.index)
        minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0),
                             index=close.index)

        atr = _rma(tr, self.period)
        plus_di = 100.0 * _rma(plus_dm, self.period) / atr
        minus_di = 100.0 * _rma(minus_dm, self.period) / atr

        di_sum = plus_di + minus_di
        dx = 100.0 * (plus_di - minus_di).abs() / di_sum.where(di_sum != 0, 1.0)
        return _rma(dx, self.period)
