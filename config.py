"""
Global configuration.

Contains only global defaults shared across all strategies:
  - Data paths (parquet files)
  - Lot sizes per instrument
  - Live trading settings (API, Telegram)
  - Risk management defaults

Strategy-specific parameters (indicators, SL/TP, entry levels, etc.)
are defined in saved_strategies/*.json files.
"""

import os

# Load .env / .env.local if present (python-dotenv)
try:
    from dotenv import load_dotenv
    # .env.local takes priority over .env
    load_dotenv(".env.local", override=True)
    load_dotenv(".env", override=False)
except ImportError:
    pass  # dotenv not installed — use raw env vars

# ============================================
# DHAN API CREDENTIALS
# ============================================
CLIENT_ID = os.getenv("CLIENT_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")

# ============================================
# TRADING MODE: "paper" (default) or "live"
# ============================================
TRADING_MODE = os.getenv("TRADING_MODE", "paper")

# ============================================
# RISK MANAGEMENT
# ============================================
# Max daily loss as % of capital before auto-halt (kill switch)
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "35"))

# Initial capital for P&L and risk calculations (INR)
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "200000"))

# ============================================
# LOT SIZES PER INSTRUMENT
# ============================================
# Number of units per lot for each instrument.
# Used for money P&L calculation: option_pnl * lot_size = money_pnl
LOT_SIZE = {
    'NIFTY': 75,
    'BANKNIFTY': 30,
    'SENSEX': 20,
    'RELIANCE': 250,
    'HDFCBANK': 550,
}

# ============================================
# DATA PATHS (parquet files with 1-min options OHLC)
# ============================================
DATA_PATH = {
    'NIFTY': 'data/options/nifty/NIFTY_OPTIONS_1m.parquet',
    'SENSEX': 'data/options/sensex/SENSEX_OPTIONS_1m.parquet',
}

# ============================================
# INSTRUMENTS LIST
# ============================================
INSTRUMENTS = ['NIFTY', 'BANKNIFTY', 'RELIANCE', 'HDFCBANK', 'SENSEX']

# Strike rounding per instrument (for ATM calculation)
STRIKE_ROUNDING = {
    'NIFTY': 50,
    'BANKNIFTY': 100,
    'SENSEX': 100,
    'RELIANCE': 5,
    'HDFCBANK': 10,
}

# ============================================
# LIVE TRADING SETTINGS
# ============================================
DATA_REFRESH_INTERVAL = 1    # seconds
ATM_UPDATE_INTERVAL = 300    # seconds
MAX_CONCURRENT_POSITIONS = 2  # 1 CE + 1 PE

# Logging
LOG_FILE = "trading_bot.log"
LOG_LEVEL = "INFO"

# Telegram (optional)
ENABLE_TELEGRAM_NOTIFICATIONS = os.getenv("ENABLE_TELEGRAM", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
