"""
Heikin-Ashi candle computation.

Matches TradingView PineScript formula:
  HA_Close = (O + H + L + C) / 4
  HA_Open  = first ? (O + C) / 2 : (prev_HA_Open + prev_HA_Close) / 2
  HA_High  = max(H, HA_Open, HA_Close)
  HA_Low   = min(L, HA_Open, HA_Close)
"""

import numpy as np
import pandas as pd


def compute_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Add ha_open, ha_close, ha_high, ha_low columns to a DataFrame with OHLC data.

    Args:
        df: DataFrame with columns: open, high, low, close

    Returns:
        Copy of df with four new HA columns added. Original columns preserved.
    """
    out = df.copy()
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(df)

    ha_close = (o + h + l + c) / 4.0

    ha_open = np.empty(n)
    ha_open[0] = (o[0] + c[0]) / 2.0
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0

    ha_high = np.maximum(h, np.maximum(ha_open, ha_close))
    ha_low = np.minimum(l, np.minimum(ha_open, ha_close))

    out["ha_open"] = ha_open
    out["ha_close"] = ha_close
    out["ha_high"] = ha_high
    out["ha_low"] = ha_low

    return out
