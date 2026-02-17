"""
RSI 70 Sell Strategy.

Sell ATM options when RSI(14) crosses above 70.
Staggered entry at +5%, +10%, +15% from base price.
SL = 20% above avg entry, TP = 10% below avg entry.
Hours: 09:30 - 14:30 IST. Nearest weekly expiry only.

This is the original strategy that was hardcoded in backtest_engine.py,
now expressed as a configurable dictionary.
"""

STRATEGY = {
    # ---- Identity ----
    "name": "RSI 70 Sell",
    "description": (
        "Sell ATM option when RSI(14) crosses above 70. "
        "Staggered entry at +5/10/15% from base. "
        "SL 20%, TP 10%. Nearest weekly expiry."
    ),

    # ---- Indicators ----
    # List of indicators to calculate on each contract.
    # Each indicator gets its own column in the data.
    "indicators": [
        {
            "type": "RSI",       # matches indicators/__init__.py registry
            "name": "rsi_14",    # column name in DataFrame
            "period": 14,
        },
    ],

    # ---- Signal Conditions ----
    # When ALL conditions fire (AND logic), a signal is generated.
    # The signal starts "observation" of the ATM contract.
    "signal_conditions": [
        {
            "indicator": "rsi_14",       # column name from indicators
            "compare": "crosses_above",  # comparison type (see engine/signals.py)
            "value": 70,                 # threshold
        },
    ],
    "signal_logic": "AND",  # "AND" = all conditions must be true, "OR" = any

    # ---- Trade Direction ----
    # "sell" = we sell the option (profit when price drops)
    # "buy" = we buy the option (profit when price rises)
    "direction": "sell",

    # ---- Staggered Entry Levels ----
    # After signal fires, enter in parts as price moves in our direction.
    # For sell: price must RISE by pct% (we sell higher = better price).
    # For buy: price must DROP by pct% (we buy lower = better price).
    "entry_levels": [
        {"pct_above_base": 5,  "capital_pct": 33.33},   # Part 1: +5% from base
        {"pct_above_base": 10, "capital_pct": 33.33},   # Part 2: +10% from base
        {"pct_above_base": 15, "capital_pct": 33.34},   # Part 3: +15% from base
    ],

    # ---- Stop Loss / Target Profit ----
    # Based on weighted average entry price.
    # SL: for sell, exit if price rises 20% above avg entry.
    # TP: for sell, exit if price drops 10% below avg entry.
    # Exits are assumed to fill at EXACT SL/TP level (no slippage).
    "stop_loss_pct": 20,
    "target_pct": 10,

    # ---- Trading Hours (IST) ----
    "trading_start": "09:30",  # Signal detection starts
    "trading_end": "14:30",    # Force exit all positions (EOD)

    # ---- Instruments ----
    # Which instruments to backtest. Each runs independently.
    "instruments": ["NIFTY", "SENSEX"],

    # ---- Max Trades Per Day ----
    # Total combined CE + PE trades allowed per day.
    # None = unlimited. Current behavior: 1 CE + 1 PE at a time.
    "max_trades_per_day": None,

    # ---- Backtest Date Range ----
    "backtest_start": "2025-01-01",
    "backtest_end": "2025-12-31",

    # ---- Capital ----
    "initial_capital": 200000,  # Rs 2,00,000 per instrument
}
