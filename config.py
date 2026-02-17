"""
Global configuration.

Contains only global defaults shared across all strategies:
  - Data paths (parquet files)
  - Lot sizes per instrument
  - Live trading settings (API, Telegram)

Strategy-specific parameters (indicators, SL/TP, entry levels, etc.)
are defined in strategies/*.py files.
"""

import os

# ============================================
# DHAN API CREDENTIALS (live trading)
# ============================================
CLIENT_ID = os.getenv("CLIENT_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")


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

# Strike rounding per instrument (for live trading ATM calculation)
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
MAX_CONCURRENT_POSITIONS = 1

# Logging
LOG_FILE = "trading_bot.log"
LOG_LEVEL = "INFO"

# Telegram (optional)
ENABLE_TELEGRAM_NOTIFICATIONS = True
TELEGRAM_BOT_TOKEN = "your_telegram_bot_token_here"
TELEGRAM_CHAT_ID = "your_telegram_chat_id_here"
