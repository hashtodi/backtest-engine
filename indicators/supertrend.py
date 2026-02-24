"""
SuperTrend indicator.

A trend-following overlay that flips between support (bullish) and resistance (bearish).

How it works:
  1. Calculate ATR (Average True Range) over `atr_period` bars.
  2. Build upper/lower bands: hl2 ± factor * ATR.
  3. Bands ratchet: they only move in the direction of the trend.
  4. When price crosses a band, direction flips.

Parameters:
  - factor:     multiplier for ATR (default 4, range 1-20). Lower = more sensitive.
  - atr_period:  lookback for ATR (default 11).

Returns 2 series (multi-output):
  - value:     the SuperTrend line price level
  - direction: -1 = bullish (price above), +1 = bearish (price below)

Matches TradingView's ta.supertrend() when high/low data is provided:
  - Source = hl2 = (high + low) / 2
  - True Range = max(high - low, |high - close_prev|, |low - close_prev|)
  - ATR uses Wilder's smoothing

Falls back to close-only approximation when high/low are not available.
"""

import pandas as pd
import numpy as np
from typing import Dict
from indicators.base import Indicator


class SuperTrend(Indicator):
    """SuperTrend with configurable factor (sensitivity) and ATR period."""

    def __init__(self, name: str, factor: int = 4, atr_period: int = 11, **kwargs):
        super().__init__(name, factor=factor, atr_period=atr_period)
        self.factor = factor
        self.atr_period = atr_period

    def calculate(self, close: pd.Series, volume: pd.Series = None,
                  high: pd.Series = None, low: pd.Series = None) -> Dict[str, pd.Series]:
        """
        Calculate SuperTrend value and direction.

        When high/low are provided, uses proper OHLC True Range and hl2 source
        (matches TradingView exactly). Otherwise falls back to close-only.

        Args:
            close: close prices, sorted chronologically
            volume: (unused, kept for interface compatibility)
            high:  high prices (optional, enables TradingView-matching mode)
            low:   low prices (optional, enables TradingView-matching mode)

        Returns:
            dict with keys:
              "value"     - SuperTrend line (float)
              "direction" - trend direction (-1 = bullish, +1 = bearish)
        """
        closes = close.values.astype(float)
        n = len(closes)

        # Check if we have OHLC data
        has_ohlc = (high is not None and low is not None
                    and len(high) == n and len(low) == n)

        if has_ohlc:
            highs = high.values.astype(float)
            lows = low.values.astype(float)

        # Output arrays, default NaN
        st_value = np.full(n, np.nan)
        st_direction = np.full(n, np.nan)

        if n < self.atr_period + 1:
            return {
                "value": pd.Series(st_value, index=close.index),
                "direction": pd.Series(st_direction, index=close.index),
            }

        # --- Step 1: Calculate True Range ---
        tr = np.full(n, np.nan)

        if has_ohlc:
            # Full True Range: max(H-L, |H-C_prev|, |L-C_prev|)
            # Matches TradingView's ta.atr()
            for i in range(1, n):
                hl = highs[i] - lows[i]
                hc = abs(highs[i] - closes[i - 1])
                lc = abs(lows[i] - closes[i - 1])
                tr[i] = max(hl, hc, lc)
        else:
            # Close-only fallback: TR ≈ |close - close_prev|
            for i in range(1, n):
                tr[i] = abs(closes[i] - closes[i - 1])

        # --- Step 2: ATR using Wilder's smoothing ---
        atr = np.full(n, np.nan)
        atr_p = self.atr_period

        # Seed: SMA of first atr_period True Range values
        atr[atr_p] = np.mean(tr[1:atr_p + 1])

        # Wilder's recursive smoothing
        for i in range(atr_p + 1, n):
            atr[i] = (atr[i - 1] * (atr_p - 1) + tr[i]) / atr_p

        # --- Step 3: Source price for bands ---
        if has_ohlc:
            # hl2 = (high + low) / 2 — TradingView's default source
            src = (highs + lows) / 2.0
        else:
            # Fallback: use close as source
            src = closes

        # --- Step 4: Calculate SuperTrend ---
        upper_band = np.full(n, np.nan)
        lower_band = np.full(n, np.nan)

        start = atr_p  # first bar where ATR is available

        # Initial bands
        upper_band[start] = src[start] + self.factor * atr[start]
        lower_band[start] = src[start] - self.factor * atr[start]

        # Initial direction: bullish (-1)
        st_direction[start] = -1
        st_value[start] = lower_band[start]

        for i in range(start + 1, n):
            # Raw bands for this bar
            raw_upper = src[i] + self.factor * atr[i]
            raw_lower = src[i] - self.factor * atr[i]

            # Ratchet lower band up (only moves up in an uptrend).
            # Keep previous lower band if it was higher, unless price broke below it.
            if raw_lower > lower_band[i - 1] or closes[i - 1] < lower_band[i - 1]:
                lower_band[i] = raw_lower
            else:
                lower_band[i] = lower_band[i - 1]

            # Ratchet upper band down (only moves down in a downtrend).
            # Keep previous upper band if it was lower, unless price broke above it.
            if raw_upper < upper_band[i - 1] or closes[i - 1] > upper_band[i - 1]:
                upper_band[i] = raw_upper
            else:
                upper_band[i] = upper_band[i - 1]

            # Determine direction:
            #   Previous was bearish (upper band): flip bullish if close > upper
            #   Previous was bullish (lower band): flip bearish if close < lower
            prev_dir = st_direction[i - 1]

            if np.isnan(prev_dir):
                st_direction[i] = -1
            elif prev_dir == 1:
                # Was bearish (using upper band)
                st_direction[i] = -1 if closes[i] > upper_band[i] else 1
            else:
                # Was bullish (using lower band)
                st_direction[i] = 1 if closes[i] < lower_band[i] else -1

            # SuperTrend value = lower band when bullish, upper band when bearish
            st_value[i] = lower_band[i] if st_direction[i] == -1 else upper_band[i]

        return {
            "value": pd.Series(st_value, index=close.index),
            "direction": pd.Series(st_direction, index=close.index),
        }
