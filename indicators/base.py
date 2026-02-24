"""
Base Indicator class.

All indicators inherit from this.
The same calculate() method works for both price sources:
  - "option": input is option close prices, calculated per contract (resets on expiry)
  - "spot":   input is underlying/spot prices, calculated once (no expiry reset)
The price source is chosen in the strategy config, not here.
"""

import pandas as pd
from typing import Union, Dict


class Indicator:
    """
    Base class for all technical indicators.

    Subclasses must implement calculate().
    - Input: close prices (pd.Series), optionally volumes
    - Output: pd.Series (single value) or dict of pd.Series (multi-output like MACD)
    """

    def __init__(self, name: str, **params):
        """
        Args:
            name: unique name for this indicator instance (e.g., "rsi_14")
            **params: indicator-specific parameters (e.g., period=14)
        """
        self.name = name
        self.params = params

    def calculate(self, close: pd.Series, volume: pd.Series = None) -> Union[pd.Series, Dict[str, pd.Series]]:
        """
        Calculate the indicator values.

        Args:
            close: option close prices, sorted chronologically
            volume: option volume (needed for VWAP)

        Returns:
            pd.Series for single-output indicators (RSI, EMA, SMA)
            dict[str, pd.Series] for multi-output indicators (MACD, Bollinger)
        """
        raise NotImplementedError("Subclasses must implement calculate()")

    def __repr__(self):
        params_str = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.__class__.__name__}(name='{self.name}', {params_str})"
