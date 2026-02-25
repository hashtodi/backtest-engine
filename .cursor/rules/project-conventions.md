# Project Conventions

## Architecture

Modular options trading system with backtesting, forward testing, and live trading.

### Packages

- `config.py` — Global defaults (data paths, lot sizes, risk settings). Reads `.env`.
- `datafeed/` — Dhan API integration (REST client, security map, option chain helpers)
- `ws_feed.py` — Dhan WebSocket real-time tick data feed
- `indicators/` — Indicator implementations (RSI, EMA, SMA, MACD, Bollinger, VWAP, SuperTrend)
- `engine/` — Backtest engine (backtest loop, data loader, signals, reporter, trade management)
- `forward/` — Forward test engine (live paper trading, price buffer, warmup, tick-level checks)
- `trading/` — Order execution (paper/live modes, risk manager, order tracker)
- `ui/` — Streamlit UI tabs (dashboard, trades, backtest runner, forward test)
- `saved_strategies/` — JSON strategy definitions
- `deploy/` — Docker and Lightsail deployment configs

### Entry Points

- `app.py` — Streamlit UI
- `forward_test_runner.py` — CLI forward test
- `run_backtest.py` — CLI backtest

## Strategy Format

Strategies are JSON files in `saved_strategies/`:

```json
{
    "name": "Display Name",
    "description": "One-line summary",
    "indicators": [
        {"type": "RSI", "name": "spot_rsi_14", "period": 14, "price_source": "spot"},
        {"type": "EMA", "name": "opt_ema_20", "period": 20, "price_source": "option"}
    ],
    "signal_conditions": [
        {"indicator": "spot_rsi_14", "compare": "crosses_above", "value": 70}
    ],
    "signal_logic": "AND",
    "direction": "sell",
    "entry": {
        "type": "staggered",
        "levels": [{"pct_from_base": 5, "capital_pct": 33.33}]
    },
    "stop_loss_pct": 20,
    "target_pct": 10,
    "trading_start": "09:30",
    "trading_end": "14:30",
    "instruments": ["NIFTY"],
    "backtest_start": "2025-01-01",
    "backtest_end": "2025-12-31",
    "initial_capital": 200000
}
```

## Adding a New Indicator

1. Create `indicators/<name>.py` inheriting from `indicators.base.Indicator`
2. Implement `calculate(close, volume)` returning `pd.Series` or `Dict[str, pd.Series]`
3. Register in `indicators/__init__.py` `_REGISTRY`

## Signal Comparison Types

Available in `engine/signals.py`:
- `crosses_above` / `crosses_below` — threshold crossover
- `above` / `below` — simple threshold
- `price_crosses_above` / `price_crosses_below` — price vs indicator
- `crosses_above_indicator` / `crosses_below_indicator` — indicator vs indicator

## Indicator Price Sources

Each indicator has a `price_source` field:
- `"spot"` — calculated on the underlying/spot price. One value per minute, shared across
  all contracts. Does NOT reset on new expiry. Naming: `spot_rsi_14`, `spot_ema_20`.
- `"option"` — calculated on option close price, per contract
  (strike + option_type + expiry_type + expiry_code). Resets on new expiry.
  Naming: `opt_rsi_14`, `opt_ema_20`.

Cross-comparison is allowed (e.g., `spot_ema_9 crosses_above_indicator opt_ema_20`).

Default for new indicators in UI: `"spot"`.
Backward compat: old strategies without `price_source` default to `"option"`.

## Trading Modes

- `paper` — Simulated trades, no real money (default)
- `live` — Real orders via Dhan API with safety rails:
  - Daily loss limit: 35% of capital (configurable)
  - Kill switch: halts all trading instantly
  - INTRADAY options only

## Key Rules

- CE and PE are tracked independently (can have 1 CE + 1 PE active simultaneously)
- Both tracks reset at end of day
- SL/TP exits are exact fills (no slippage assumed in backtest)
- Data: nearest weekly expiry only (expiry_code=1, expiry_type=WEEK)
- ATM filter applied only during signal detection (not during position tracking)
- Option indicators calculated per contract (strike + option_type + expiry_type + expiry_code)
- Spot indicators calculated on unique spot price timeline (no per-contract grouping)

## Deployment

Uses Docker + docker-compose. See `deploy/lightsail-setup.md` for AWS Lightsail guide.
Credentials are stored in `.env` (never committed to git).
