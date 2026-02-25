"""
Signal condition evaluation.

Evaluates whether signal conditions are met for a given data row.
Supports multiple comparison types and AND/OR logic.

Comparison types:
  - crosses_above: indicator crosses above a fixed value (e.g., RSI crosses 70)
  - crosses_below: indicator crosses below a fixed value (e.g., RSI crosses 30)
  - above: indicator is above a value (no crossover needed)
  - below: indicator is below a value
  - price_above: price is above indicator line (no crossover needed)
  - price_below: price is below indicator line (no crossover needed)
  - price_crosses_above: price crosses above indicator line
  - price_crosses_below: price crosses below indicator line
  For price_* types, condition["price_field"] selects which price
  (close, high, low, open) to compare. Defaults to "close".
  - crosses_above_indicator: one indicator crosses above another
  - crosses_below_indicator: one indicator crosses below another
"""

import pandas as pd
from typing import List, Dict, Tuple


def check_condition(row, condition: Dict) -> Tuple[bool, str]:
    """
    Check a single signal condition against one data row.

    Args:
        row: pandas Series (one minute of data) with indicator columns
        condition: dict from strategy config, e.g.:
            {"indicator": "rsi_14", "compare": "crosses_above", "value": 70}

    Returns:
        (is_met: bool, description: str)
    """
    compare = condition['compare']
    ind_name = condition['indicator']

    # Get current and previous values for this indicator
    curr = row.get(ind_name)
    prev = row.get(f"{ind_name}_prev")

    # --- Price vs indicator (no crossover) ---
    # These only need curr, not prev. Check them before the blanket NaN guard
    # so they work on the first bar after warmup (where prev may still be NaN).
    # price_field lets the user choose close/high/low/open (default: close).

    if compare == 'price_above':
        pf = condition.get('price_field', 'close')
        price = row.get(pf)
        if pd.isna(price) or pd.isna(curr):
            return False, f"{pf} or {ind_name}=NaN"
        met = price > curr
        desc = f"{pf} above {ind_name} ({price:.2f} > {curr:.2f})" if met else ""
        return met, desc

    elif compare == 'price_below':
        pf = condition.get('price_field', 'close')
        price = row.get(pf)
        if pd.isna(price) or pd.isna(curr):
            return False, f"{pf} or {ind_name}=NaN"
        met = price < curr
        desc = f"{pf} below {ind_name} ({price:.2f} < {curr:.2f})" if met else ""
        return met, desc

    # Skip if indicator values are NaN (warmup period).
    # Remaining comparisons all need both curr and prev.
    if pd.isna(curr) or pd.isna(prev):
        return False, f"{ind_name}=NaN (warmup)"

    # --- Threshold-based comparisons ---

    if compare == 'crosses_above':
        # Indicator crosses above a fixed value
        value = condition['value']
        met = prev <= value and curr > value
        desc = f"{ind_name} crossed above {value} ({prev:.2f} -> {curr:.2f})" if met else ""
        return met, desc

    elif compare == 'crosses_below':
        # Indicator crosses below a fixed value
        value = condition['value']
        met = prev >= value and curr < value
        desc = f"{ind_name} crossed below {value} ({prev:.2f} -> {curr:.2f})" if met else ""
        return met, desc

    elif compare == 'above':
        # Indicator is above a value (no crossover needed)
        value = condition['value']
        met = curr > value
        desc = f"{ind_name}={curr:.2f} > {value}" if met else ""
        return met, desc

    elif compare == 'below':
        # Indicator is below a value
        value = condition['value']
        met = curr < value
        desc = f"{ind_name}={curr:.2f} < {value}" if met else ""
        return met, desc

    # --- Price vs indicator crossover comparisons ---

    elif compare == 'price_crosses_above':
        # Price crosses above the indicator line.
        # Uses price_field (close/high/low/open) and its _prev counterpart.
        pf = condition.get('price_field', 'close')
        price = row.get(pf)
        price_prev = row.get(f'{pf}_prev', price)
        if pd.isna(price):
            return False, f"{pf}=NaN"
        met = price_prev <= prev and price > curr
        desc = f"{pf} crossed above {ind_name} ({price:.2f} > {curr:.2f})" if met else ""
        return met, desc

    elif compare == 'price_crosses_below':
        # Price crosses below the indicator line.
        pf = condition.get('price_field', 'close')
        price = row.get(pf)
        price_prev = row.get(f'{pf}_prev', price)
        if pd.isna(price):
            return False, f"{pf}=NaN"
        met = price_prev >= prev and price < curr
        desc = f"{pf} crossed below {ind_name} ({price:.2f} < {curr:.2f})" if met else ""
        return met, desc

    # --- Indicator vs indicator comparisons ---

    elif compare == 'crosses_above_indicator':
        # One indicator crosses above another
        other_name = condition['other']
        other_curr = row.get(other_name)
        other_prev = row.get(f"{other_name}_prev")
        if pd.isna(other_curr) or pd.isna(other_prev):
            return False, f"{other_name}=NaN (warmup)"
        met = prev <= other_prev and curr > other_curr
        desc = (f"{ind_name} crossed above {other_name} "
                f"({curr:.2f} > {other_curr:.2f})") if met else ""
        return met, desc

    elif compare == 'crosses_below_indicator':
        # One indicator crosses below another
        other_name = condition['other']
        other_curr = row.get(other_name)
        other_prev = row.get(f"{other_name}_prev")
        if pd.isna(other_curr) or pd.isna(other_prev):
            return False, f"{other_name}=NaN (warmup)"
        met = prev >= other_prev and curr < other_curr
        desc = (f"{ind_name} crossed below {other_name} "
                f"({curr:.2f} < {other_curr:.2f})") if met else ""
        return met, desc

    else:
        raise ValueError(f"Unknown comparison type: '{compare}'")


def check_signal(row, conditions: List[Dict], logic: str = "AND") -> Tuple[bool, str]:
    """
    Check all signal conditions for one row.

    Args:
        row: pandas Series (one minute of ATM option data)
        conditions: list of condition dicts from strategy config
        logic: "AND" (all must fire) or "OR" (any fires)

    Returns:
        (signal_fired: bool, description: str with reasons)
    """
    if not conditions:
        return False, ""

    results = []
    descriptions = []

    for cond in conditions:
        met, desc = check_condition(row, cond)
        results.append(met)
        if met and desc:
            descriptions.append(desc)

    if logic == "AND":
        fired = all(results)
    elif logic == "OR":
        fired = any(results)
    else:
        raise ValueError(f"Unknown signal logic: '{logic}'. Use 'AND' or 'OR'.")

    reason = " & ".join(descriptions) if fired else ""
    return fired, reason
