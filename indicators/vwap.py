"""
VWAP (Volume Weighted Average Price) indicator.

Calculated as cumulative (price * volume) / cumulative volume.
Resets each day (intraday VWAP).

Note: Requires volume data. Returns NaN if volume is not provided.
"""

import pandas as pd
from indicators.base import Indicator


class VWAP(Indicator):
    """
    Intraday VWAP.

    Unlike other indicators, VWAP needs volume data.
    It resets at the start of each trading day.
    """

    def __init__(self, name: str, **kwargs):
        super().__init__(name)

    def calculate(self, close: pd.Series, volume: pd.Series = None) -> pd.Series:
        """
        Calculate VWAP.

        Note: This returns cumulative VWAP across the entire series.
        The caller (data_loader) should group by day before calling,
        so that VWAP resets daily.

        Args:
            close: option close prices, sorted chronologically
            volume: option volume (required for VWAP)

        Returns:
            pd.Series of VWAP values. NaN if volume not provided.
        """
        if volume is None:
            return pd.Series(float('nan'), index=close.index)

        # Cumulative price*volume / cumulative volume
        cum_pv = (close * volume).cumsum()
        cum_vol = volume.cumsum()

        # Avoid division by zero
        vwap = cum_pv / cum_vol.replace(0, float('nan'))
        return vwap
