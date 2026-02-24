# RSI Options Trading Strategy

Modular options trading system with backtesting, forward testing (paper trading), and live trading via Dhan API.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy env template and add your Dhan credentials
cp .env.example .env

# Run Streamlit UI
streamlit run app.py

# Run backtest via CLI
python run_backtest.py --strategy rsi_ema_cross

# Run forward test via CLI
python forward_test_runner.py --strategy rsi_ema_cross --instrument NIFTY
```

## Project Structure

```
├── app.py                        # Streamlit UI entry point
├── config.py                     # Global config (reads .env)
├── forward_test_runner.py        # CLI forward test entry point
├── run_backtest.py               # CLI backtest entry point
├── .env.example                  # Environment variable template
├── Dockerfile                    # Docker image for deployment
├── docker-compose.yml            # Multi-service deployment
│
├── datafeed/                     # Dhan API integration
│   ├── __init__.py               # DhanDataFeed facade
│   ├── rest_client.py            # REST API wrapper + rate limiting
│   ├── option_chain.py           # Option chain, expiry, ATM helpers
│   └── security_map.py           # Security ID mapping
│
├── ws_feed.py                    # Dhan WebSocket real-time tick feed
│
├── indicators/                   # Technical indicators
│   ├── base.py                   # Indicator base class
│   ├── rsi.py, ema.py, sma.py   # Single-output indicators
│   ├── macd.py, bollinger.py     # Multi-output indicators
│   ├── vwap.py, supertrend.py    # Specialized indicators
│   └── __init__.py               # Registry + factory
│
├── engine/                       # Backtest engine
│   ├── backtest.py               # Main backtest loop
│   ├── data_loader.py            # Data loading + indicator calc
│   ├── signals.py                # Signal condition evaluation
│   ├── trade.py                  # Trade lifecycle management
│   ├── reporter.py               # Reports (CSV, logs, summary)
│   └── detailed_logger.py        # Minute-by-minute detailed log
│
├── forward/                      # Forward test (paper trading)
│   ├── engine.py                 # Main loop + orchestration
│   ├── price_buffer.py           # Rolling price buffer
│   ├── warmup.py                 # Multi-day historical warmup
│   ├── tick_checker.py           # Tick-level SL/TP/entry checks
│   ├── helpers.py                # Event builders, timestamp utils
│   └── paper_trader.py           # Paper trade logger
│
├── trading/                      # Live trading module
│   ├── order_executor.py         # Paper/live order placement
│   ├── risk_manager.py           # Daily loss limit, kill switch
│   └── order_tracker.py          # Order status polling
│
├── ui/                           # Streamlit UI tabs
│   ├── dashboard.py              # Performance dashboard
│   ├── trades.py                 # Trade explorer
│   ├── backtest_runner.py        # Run backtest tab
│   ├── forward_test.py           # Forward test tab
│   ├── strategy_form.py          # Strategy configuration form
│   ├── strategy_store.py         # Load/save strategies
│   └── form_config.py            # Form defaults
│
├── saved_strategies/             # JSON strategy definitions
├── deploy/                       # Deployment configs
│   ├── lightsail-setup.md        # AWS Lightsail guide
│   └── nginx.conf                # Nginx reverse proxy config
│
├── telegram_notifier.py          # Telegram alerts (optional)
└── data/                         # Historical parquet data
```

## Strategies

Strategies are JSON files in `saved_strategies/`. Create and manage them via the Streamlit UI or edit JSON directly.

```bash
# List available strategies
python run_backtest.py --list
```

## Available Indicators

| Indicator | Type | Output | Params |
|-----------|------|--------|--------|
| RSI | `RSI` | Single series | `period` (default 14) |
| EMA | `EMA` | Single series | `period` (default 20) |
| SMA | `SMA` | Single series | `period` (default 20) |
| MACD | `MACD` | `macd`, `signal`, `histogram` | `fast`, `slow`, `signal_period` |
| Bollinger | `BOLLINGER` | `upper`, `middle`, `lower` | `period`, `std_dev` |
| VWAP | `VWAP` | Single series | (resets daily) |
| SuperTrend | `SUPERTREND` | `trend`, `direction` | `period`, `multiplier` |

Each indicator can use `"price_source": "spot"` or `"price_source": "option"`.

## Trading Modes

| Mode | Description |
|------|-------------|
| **Paper** | Simulated trades, no real money (default) |
| **Live** | Real orders via Dhan API with safety rails |

Safety features for live trading:
- Daily loss limit: 35% of capital (configurable)
- Kill switch: halts all trading instantly
- INTRADAY options only

## Deployment

Deploy on AWS Lightsail ($5/month) using Docker:

```bash
docker-compose up -d
```

See `deploy/lightsail-setup.md` for the full guide.

## Disclaimer

For educational purposes only. Options trading involves substantial risk of loss.
