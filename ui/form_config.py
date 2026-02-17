"""
Configuration constants for the strategy form.

Defines:
  - Indicator types and their parameters
  - Multi-output indicator sub-columns
  - Signal comparison types and what fields they need
  - Helper functions for auto-naming and column listing
"""

from typing import List


# ============================================
# INDICATOR DEFINITIONS
# ============================================

# Each indicator type and the parameters it accepts.
# Used to render the correct input fields per type.
INDICATOR_PARAMS = {
    "RSI": [
        {"key": "period", "label": "Period", "type": int, "default": 14, "min": 2, "max": 200},
    ],
    "EMA": [
        {"key": "period", "label": "Period", "type": int, "default": 20, "min": 2, "max": 200},
    ],
    "SMA": [
        {"key": "period", "label": "Period", "type": int, "default": 20, "min": 2, "max": 200},
    ],
    "MACD": [
        {"key": "fast", "label": "Fast", "type": int, "default": 12, "min": 2, "max": 100},
        {"key": "slow", "label": "Slow", "type": int, "default": 26, "min": 2, "max": 200},
        {"key": "signal", "label": "Signal", "type": int, "default": 9, "min": 2, "max": 100},
    ],
    "BOLLINGER": [
        {"key": "period", "label": "Period", "type": int, "default": 20, "min": 2, "max": 200},
        {"key": "std_dev", "label": "Std Dev", "type": float, "default": 2.0, "min": 0.5, "max": 5.0},
    ],
    "VWAP": [],
}

# Multi-output indicators produce name_subkey columns.
# Single-output indicators (not listed here) produce one column: just the name.
MULTI_OUTPUT = {
    "MACD": ["macd", "signal", "histogram"],
    "BOLLINGER": ["upper", "middle", "lower"],
}

# ============================================
# SIGNAL COMPARISON TYPES
# ============================================

COMPARE_TYPES = [
    "crosses_above",
    "crosses_below",
    "above",
    "below",
    "price_crosses_above",
    "price_crosses_below",
    "crosses_above_indicator",
    "crosses_below_indicator",
]

# Comparisons that require a threshold value (indicator vs number).
NEEDS_VALUE = {"crosses_above", "crosses_below", "above", "below"}

# Comparisons that require another indicator column name.
NEEDS_OTHER = {"crosses_above_indicator", "crosses_below_indicator"}


# ============================================
# HELPERS
# ============================================

def auto_name(ind: dict) -> str:
    """Generate a readable column name from an indicator config."""
    t = ind["type"]
    if t == "MACD":
        return f"macd_{ind.get('fast', 12)}_{ind.get('slow', 26)}_{ind.get('signal', 9)}"
    if t == "BOLLINGER":
        std = ind.get('std_dev', 2.0)
        # Format std_dev cleanly: 2.0 → "2", 2.5 → "2.5"
        std_str = f"{std:g}"
        return f"bb_{ind.get('period', 20)}_{std_str}"
    if t == "VWAP":
        return "vwap"
    # RSI, EMA, SMA: type_period
    return f"{t.lower()}_{ind.get('period', '')}"


def get_available_columns(indicators: list) -> List[str]:
    """
    Get all selectable column names from configured indicators.

    Single-output -> [name]
    Multi-output  -> [name_sub1, name_sub2, ...]
    """
    cols = []
    for ind in indicators:
        name = auto_name(ind)
        subs = MULTI_OUTPUT.get(ind["type"])
        if subs:
            cols.extend(f"{name}_{s}" for s in subs)
        else:
            cols.append(name)
    return cols
