# Project Conventions

## Architecture

This is a modular options backtesting system. The codebase is organized as:

- `config.py` - Global defaults only (data paths, lot sizes, live trading)
- `strategies/*.py` - Strategy definitions as STRATEGY dicts
- `indicators/*.py` - Indicator implementations (RSI, EMA, SMA, MACD, Bollinger, VWAP)
- `engine/*.py` - Core engine (backtest loop, data loader, signals, reporter, trade management)
- `run_backtest.py` - Entry point

## Strategy Format

Every strategy is a Python file in `strategies/` exporting a `STRATEGY` dict with these keys:

```python
STRATEGY = {
    "name": str,                    # Display name
    "description": str,             # One-line summary
    "indicators": [                 # List of indicator configs
        {"type": "RSI", "name": "rsi_14", "period": 14},
    ],
    "signal_conditions": [          # When all/any conditions fire -> signal
        {"indicator": "rsi_14", "compare": "crosses_above", "value": 70},
    ],
    "signal_logic": "AND",          # "AND" or "OR"
    "direction": "sell",            # "sell" or "buy"
    "entry_levels": [               # Staggered entry levels
        {"pct_above_base": 5, "capital_pct": 33.33},
    ],
    "stop_loss_pct": 20,            # SL % from avg entry
    "target_pct": 10,               # TP % from avg entry
    "trading_start": "09:30",       # IST
    "trading_end": "14:30",         # IST
    "instruments": ["NIFTY"],       # Which instruments to backtest
    "backtest_start": "2025-01-01",
    "backtest_end": "2025-12-31",
    "initial_capital": 200000,
}
```

## Adding a New Indicator

1. Create `indicators/<name>.py` inheriting from `indicators.base.Indicator`
2. Implement `calculate(close, volume)` returning `pd.Series` or `Dict[str, pd.Series]`
3. Register in `indicators/__init__.py` `_REGISTRY`

## Adding a New Strategy

1. Create `strategies/<name>.py` with a `STRATEGY` dict
2. Run: `python run_backtest.py --strategy <name>`

## Signal Comparison Types

Available in `engine/signals.py`:
- `crosses_above` / `crosses_below` - threshold crossover
- `above` / `below` - simple threshold
- `price_crosses_above` / `price_crosses_below` - price vs indicator
- `crosses_above_indicator` / `crosses_below_indicator` - indicator vs indicator

## Key Rules

- CE and PE are tracked independently (can have 1 CE + 1 PE active simultaneously)
- Both tracks reset at end of day
- SL/TP exits are exact fills (no slippage assumed)
- Data: nearest weekly expiry only (expiry_code=1, expiry_type=WEEK)
- ATM filter applied only during signal detection (not during position tracking)
- Indicators calculated per contract (strike + option_type + expiry_type + expiry_code)
