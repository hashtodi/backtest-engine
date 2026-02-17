# Options Backtesting Framework

Modular, configurable options backtesting system. Define strategies as config dicts, plug in any indicator, and run backtests.

## Quick Start

```bash
# Activate virtualenv
source venv/bin/activate

# Run default strategy (RSI 70 Sell)
python run_backtest.py

# Run a specific strategy
python run_backtest.py --strategy rsi_70_sell
```

## Project Structure

```
├── run_backtest.py               # Entry point
├── config.py                     # Global defaults (data paths, lot sizes)
│
├── strategies/                   # Strategy definitions
│   └── rsi_70_sell.py            # RSI 70 sell strategy config
│
├── indicators/                   # Indicator implementations
│   ├── base.py                   # Indicator base class
│   ├── __init__.py               # Registry + factory
│   ├── rsi.py                    # Relative Strength Index
│   ├── ema.py                    # Exponential Moving Average
│   ├── sma.py                    # Simple Moving Average
│   ├── macd.py                   # MACD (multi-output)
│   ├── bollinger.py              # Bollinger Bands (multi-output)
│   └── vwap.py                   # Volume Weighted Average Price
│
├── engine/                       # Core backtest engine
│   ├── backtest.py               # Main backtest loop
│   ├── data_loader.py            # Data loading + indicator calculation
│   ├── signals.py                # Signal condition evaluation
│   ├── trade.py                  # Trade + PositionPart classes
│   ├── reporter.py               # Reports (CSV, trade log, summary)
│   └── detailed_logger.py        # Minute-by-minute detailed log
│
├── backtest_engine.py            # (Legacy) original monolithic engine
│
├── dhan_datafeed.py              # Dhan API integration (live trading)
├── trading_bot_runner.py         # Live trading runner (future)
├── telegram_notifier.py          # Telegram alerts (future)
│
└── data/options/                 # Historical parquet data
    ├── nifty/NIFTY_OPTIONS_1m.parquet
    └── sensex/SENSEX_OPTIONS_1m.parquet
```

## Strategy Format

Each strategy is a Python file in `strategies/` exporting a `STRATEGY` dict:

```python
STRATEGY = {
    "name": "RSI 70 Sell",
    "indicators": [
        {"type": "RSI", "name": "rsi_14", "period": 14},
    ],
    "signal_conditions": [
        {"indicator": "rsi_14", "compare": "crosses_above", "value": 70},
    ],
    "signal_logic": "AND",
    "direction": "sell",
    "entry_levels": [
        {"pct_above_base": 5,  "capital_pct": 33.33},
        {"pct_above_base": 10, "capital_pct": 33.33},
        {"pct_above_base": 15, "capital_pct": 33.34},
    ],
    "stop_loss_pct": 20,
    "target_pct": 10,
    "trading_start": "09:30",
    "trading_end": "14:30",
    "instruments": ["NIFTY", "SENSEX"],
    "backtest_start": "2025-01-01",
    "backtest_end": "2025-12-31",
    "initial_capital": 200000,
}
```

## Available Indicators

| Indicator | Type | Output | Params |
|-----------|------|--------|--------|
| RSI | `RSI` | Single series | `period` (default 14) |
| EMA | `EMA` | Single series | `period` (default 20) |
| SMA | `SMA` | Single series | `period` (default 20) |
| MACD | `MACD` | `macd`, `signal`, `histogram` | `fast`, `slow`, `signal_period` |
| Bollinger | `BOLLINGER` | `upper`, `middle`, `lower` | `period`, `std_dev` |
| VWAP | `VWAP` | Single series | (resets daily, requires volume) |

## Signal Comparison Types

| Compare | Description |
|---------|-------------|
| `crosses_above` | Indicator crosses above a fixed value |
| `crosses_below` | Indicator crosses below a fixed value |
| `above` | Indicator is above a value |
| `below` | Indicator is below a value |
| `price_crosses_above` | Close price crosses above indicator |
| `price_crosses_below` | Close price crosses below indicator |
| `crosses_above_indicator` | One indicator crosses above another |
| `crosses_below_indicator` | One indicator crosses below another |

## Outputs

After running a backtest:

- `backtest_results_NIFTY.csv` / `backtest_results_SENSEX.csv` - Trade-level CSV
- `backtest_trades.log` - Detailed trade log
- `backtest_summary.md` - Performance summary
- `backtest_detailed_NIFTY.log` / `backtest_detailed_SENSEX.log` - Minute-by-minute log

## Data Schema

Parquet files contain 1-minute options data:

| Column | Description |
|--------|-------------|
| `ts` | Epoch seconds |
| `datetime` | ISO 8601 with IST offset |
| `underlying` | NIFTY or SENSEX |
| `option_type` | CE or PE |
| `expiry_type` | WEEK or MONTH |
| `expiry_code` | 1=nearest, 2=next, 3=far |
| `atm_strike` | ATM strike (spot rounded to nearest step) |
| `strike_offset` | Offset from ATM: 0, +1, -1, ... |
| `moneyness` | ITM, ATM, or OTM |
| `strike` | Actual strike price |
| `spot` | Underlying spot price |
| `open/high/low/close` | Option OHLC |
| `volume` | Volume traded |
| `oi` | Open interest |
| `iv` | Implied volatility |

## Adding a New Strategy

1. Create `strategies/my_strategy.py` with a `STRATEGY` dict
2. Run: `python run_backtest.py --strategy my_strategy`

## Adding a New Indicator

1. Create `indicators/my_indicator.py` inheriting from `indicators.base.Indicator`
2. Implement `calculate(close, volume)` -> `pd.Series` or `Dict[str, pd.Series]`
3. Register in `indicators/__init__.py` `_REGISTRY`

## Live Trading (Future)

```bash
# Set credentials in .env.local
python trading_bot_runner.py
```

See `TELEGRAM_SETUP.md` for Telegram notification setup.

## Disclaimer

For educational purposes only. Options trading involves substantial risk.
