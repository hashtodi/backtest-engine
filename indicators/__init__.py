"""
Indicator Registry.

Usage:
    from indicators import get_indicator
    rsi = get_indicator("RSI", name="rsi_14", period=14)
    values = rsi.calculate(close_prices)
"""

from indicators.base import Indicator
from indicators.rsi import RSI
from indicators.ema import EMA
from indicators.sma import SMA
from indicators.macd import MACD
from indicators.bollinger import BollingerBands
from indicators.vwap import VWAP

# Registry: maps indicator type string to class
_REGISTRY = {
    "RSI": RSI,
    "EMA": EMA,
    "SMA": SMA,
    "MACD": MACD,
    "BOLLINGER": BollingerBands,
    "VWAP": VWAP,
}


def get_indicator(indicator_type: str, name: str, **params) -> Indicator:
    """
    Factory function to create an indicator by type string.

    Args:
        indicator_type: one of "RSI", "EMA", "SMA", "MACD", "BOLLINGER", "VWAP"
        name: unique name for this instance (e.g., "rsi_14")
        **params: indicator-specific parameters (e.g., period=14)

    Returns:
        Indicator instance ready to call .calculate()

    Raises:
        ValueError: if indicator_type is not recognized
    """
    cls = _REGISTRY.get(indicator_type.upper())
    if cls is None:
        valid = ", ".join(_REGISTRY.keys())
        raise ValueError(f"Unknown indicator type '{indicator_type}'. Valid: {valid}")
    return cls(name=name, **params)


def list_indicators():
    """Return list of available indicator type strings."""
    return list(_REGISTRY.keys())
