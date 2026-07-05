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
from datetime import date, datetime

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
    'NIFTY': 65,
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

# Spot / underlying 1-min OHLCV data (used by zone-based strategies)
SPOT_DATA_PATH = {
    'NIFTY': 'data/spot/nifty/NIFTY_1m.parquet',
    'SENSEX': 'data/spot/sensex/SENSEX_1m.parquet',
}

# Futures 1-min OHLCV data
FUTURES_DATA_PATH = {
    'NIFTY': 'data/futures/NIFTY_FUT_1m.parquet',
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

# ============================================
# NIFTY WEEKLY EXPIRY DATES (2020-2026)
# ============================================
# 2020-2024: derived from option/spot data and validated via code-1 ATM straddle
#   collapse (scripts/build_weekly_expiry_calendar.py). Holiday-shifted included.
# 2025-2026: NSE circulars.
# Thursday through 2025-08-28, Tuesday from 2025-09-02 (SEBI switch 2025-09-01).
NIFTY_WEEKLY_EXPIRY_DATES = sorted([
    # --- 2020-2024 (derived, data-validated) ---
    date(2020, 1, 2),
    date(2020, 1, 9),
    date(2020, 1, 16),
    date(2020, 1, 23),
    date(2020, 1, 30),
    date(2020, 2, 6),
    date(2020, 2, 13),
    date(2020, 2, 20),
    date(2020, 2, 27),
    date(2020, 3, 5),
    date(2020, 3, 12),
    date(2020, 3, 19),
    date(2020, 3, 26),
    date(2020, 4, 1),
    date(2020, 4, 9),
    date(2020, 4, 16),
    date(2020, 4, 23),
    date(2020, 4, 30),
    date(2020, 5, 7),
    date(2020, 5, 14),
    date(2020, 5, 21),
    date(2020, 5, 28),
    date(2020, 6, 4),
    date(2020, 6, 11),
    date(2020, 6, 18),
    date(2020, 6, 25),
    date(2020, 7, 2),
    date(2020, 7, 9),
    date(2020, 7, 16),
    date(2020, 7, 23),
    date(2020, 7, 30),
    date(2020, 8, 6),
    date(2020, 8, 13),
    date(2020, 8, 20),
    date(2020, 8, 27),
    date(2020, 9, 3),
    date(2020, 9, 10),
    date(2020, 9, 17),
    date(2020, 9, 24),
    date(2020, 10, 1),
    date(2020, 10, 8),
    date(2020, 10, 15),
    date(2020, 10, 22),
    date(2020, 10, 29),
    date(2020, 11, 5),
    date(2020, 11, 12),
    date(2020, 11, 19),
    date(2020, 11, 26),
    date(2020, 12, 3),
    date(2020, 12, 10),
    date(2020, 12, 17),
    date(2020, 12, 24),
    date(2020, 12, 31),
    date(2021, 1, 7),
    date(2021, 1, 14),
    date(2021, 1, 21),
    date(2021, 1, 28),
    date(2021, 2, 4),
    date(2021, 2, 11),
    date(2021, 2, 18),
    date(2021, 2, 25),
    date(2021, 3, 4),
    date(2021, 3, 10),
    date(2021, 3, 18),
    date(2021, 3, 25),
    date(2021, 4, 1),
    date(2021, 4, 8),
    date(2021, 4, 15),
    date(2021, 4, 22),
    date(2021, 4, 29),
    date(2021, 5, 6),
    date(2021, 5, 12),
    date(2021, 5, 20),
    date(2021, 5, 27),
    date(2021, 6, 3),
    date(2021, 6, 10),
    date(2021, 6, 17),
    date(2021, 6, 24),
    date(2021, 7, 1),
    date(2021, 7, 8),
    date(2021, 7, 15),
    date(2021, 7, 22),
    date(2021, 7, 29),
    date(2021, 8, 5),
    date(2021, 8, 12),
    date(2021, 8, 18),
    date(2021, 8, 26),
    date(2021, 9, 2),
    date(2021, 9, 9),
    date(2021, 9, 16),
    date(2021, 9, 23),
    date(2021, 9, 30),
    date(2021, 10, 7),
    date(2021, 10, 14),
    date(2021, 10, 21),
    date(2021, 10, 28),
    date(2021, 11, 3),
    date(2021, 11, 11),
    date(2021, 11, 18),
    date(2021, 11, 25),
    date(2021, 12, 2),
    date(2021, 12, 9),
    date(2021, 12, 16),
    date(2021, 12, 23),
    date(2021, 12, 30),
    date(2022, 1, 6),
    date(2022, 1, 13),
    date(2022, 1, 20),
    date(2022, 1, 27),
    date(2022, 2, 3),
    date(2022, 2, 10),
    date(2022, 2, 17),
    date(2022, 2, 24),
    date(2022, 3, 3),
    date(2022, 3, 10),
    date(2022, 3, 17),
    date(2022, 3, 24),
    date(2022, 3, 31),
    date(2022, 4, 7),
    date(2022, 4, 13),
    date(2022, 4, 21),
    date(2022, 4, 28),
    date(2022, 5, 5),
    date(2022, 5, 12),
    date(2022, 5, 19),
    date(2022, 5, 26),
    date(2022, 6, 2),
    date(2022, 6, 9),
    date(2022, 6, 16),
    date(2022, 6, 23),
    date(2022, 6, 30),
    date(2022, 7, 7),
    date(2022, 7, 14),
    date(2022, 7, 21),
    date(2022, 7, 28),
    date(2022, 8, 4),
    date(2022, 8, 11),
    date(2022, 8, 18),
    date(2022, 8, 25),
    date(2022, 9, 1),
    date(2022, 9, 8),
    date(2022, 9, 15),
    date(2022, 9, 22),
    date(2022, 9, 29),
    date(2022, 10, 6),
    date(2022, 10, 13),
    date(2022, 10, 20),
    date(2022, 10, 27),
    date(2022, 11, 3),
    date(2022, 11, 10),
    date(2022, 11, 17),
    date(2022, 11, 24),
    date(2022, 12, 1),
    date(2022, 12, 8),
    date(2022, 12, 15),
    date(2022, 12, 22),
    date(2022, 12, 29),
    date(2023, 1, 5),
    date(2023, 1, 12),
    date(2023, 1, 19),
    date(2023, 1, 25),
    date(2023, 2, 2),
    date(2023, 2, 9),
    date(2023, 2, 16),
    date(2023, 2, 23),
    date(2023, 3, 2),
    date(2023, 3, 9),
    date(2023, 3, 16),
    date(2023, 3, 23),
    date(2023, 3, 29),
    date(2023, 4, 6),
    date(2023, 4, 13),
    date(2023, 4, 20),
    date(2023, 4, 27),
    date(2023, 5, 4),
    date(2023, 5, 11),
    date(2023, 5, 18),
    date(2023, 5, 25),
    date(2023, 6, 1),
    date(2023, 6, 8),
    date(2023, 6, 15),
    date(2023, 6, 22),
    date(2023, 6, 28),
    date(2023, 7, 6),
    date(2023, 7, 13),
    date(2023, 7, 20),
    date(2023, 7, 27),
    date(2023, 8, 3),
    date(2023, 8, 10),
    date(2023, 8, 17),
    date(2023, 8, 24),
    date(2023, 8, 31),
    date(2023, 9, 7),
    date(2023, 9, 14),
    date(2023, 9, 21),
    date(2023, 9, 28),
    date(2023, 10, 5),
    date(2023, 10, 12),
    date(2023, 10, 19),
    date(2023, 10, 26),
    date(2023, 11, 2),
    date(2023, 11, 9),
    date(2023, 11, 16),
    date(2023, 11, 23),
    date(2023, 11, 30),
    date(2023, 12, 7),
    date(2023, 12, 14),
    date(2023, 12, 21),
    date(2023, 12, 28),
    date(2024, 1, 4),
    date(2024, 1, 11),
    date(2024, 1, 18),
    date(2024, 1, 25),
    date(2024, 2, 1),
    date(2024, 2, 8),
    date(2024, 2, 15),
    date(2024, 2, 22),
    date(2024, 2, 29),
    date(2024, 3, 7),
    date(2024, 3, 14),
    date(2024, 3, 21),
    date(2024, 3, 28),
    date(2024, 4, 4),
    date(2024, 4, 10),
    date(2024, 4, 18),
    date(2024, 4, 25),
    date(2024, 5, 2),
    date(2024, 5, 9),
    date(2024, 5, 16),
    date(2024, 5, 23),
    date(2024, 5, 30),
    date(2024, 6, 6),
    date(2024, 6, 13),
    date(2024, 6, 20),
    date(2024, 6, 27),
    date(2024, 7, 4),
    date(2024, 7, 11),
    date(2024, 7, 18),
    date(2024, 7, 25),
    date(2024, 8, 1),
    date(2024, 8, 8),
    date(2024, 8, 14),
    date(2024, 8, 22),
    date(2024, 8, 29),
    date(2024, 9, 5),
    date(2024, 9, 12),
    date(2024, 9, 19),
    date(2024, 9, 26),
    date(2024, 10, 3),
    date(2024, 10, 10),
    date(2024, 10, 17),
    date(2024, 10, 24),
    date(2024, 10, 31),
    date(2024, 11, 7),
    date(2024, 11, 14),
    date(2024, 11, 21),
    date(2024, 11, 28),
    date(2024, 12, 5),
    date(2024, 12, 12),
    date(2024, 12, 19),
    date(2024, 12, 26),
    # --- 2025 ---
    date(2025, 1, 2),
    date(2025, 1, 9),
    date(2025, 1, 16),
    date(2025, 1, 23),
    date(2025, 1, 30),
    date(2025, 2, 6),
    date(2025, 2, 13),
    date(2025, 2, 20),
    date(2025, 2, 27),
    date(2025, 3, 6),
    date(2025, 3, 13),
    date(2025, 3, 20),
    date(2025, 3, 27),
    date(2025, 4, 3),
    date(2025, 4, 9),   # shifted from 10-Apr (Shri Mahavir Jayanti)
    date(2025, 4, 17),
    date(2025, 4, 24),
    date(2025, 4, 30),  # shifted from 01-May (Maharashtra Day)
    date(2025, 5, 8),
    date(2025, 5, 15),
    date(2025, 5, 22),
    date(2025, 5, 29),
    date(2025, 6, 5),
    date(2025, 6, 12),
    date(2025, 6, 19),
    date(2025, 6, 26),
    date(2025, 7, 3),
    date(2025, 7, 10),
    date(2025, 7, 17),
    date(2025, 7, 24),
    date(2025, 7, 31),
    date(2025, 8, 7),
    date(2025, 8, 14),
    date(2025, 8, 21),
    date(2025, 8, 28),
    date(2025, 9, 2),
    date(2025, 9, 9),
    date(2025, 9, 16),
    date(2025, 9, 23),
    date(2025, 9, 30),
    date(2025, 10, 7),
    date(2025, 10, 14),
    date(2025, 10, 20), # shifted from 21-Oct (Diwali Laxmi Pujan)
    date(2025, 10, 28),
    date(2025, 11, 4),
    date(2025, 11, 11),
    date(2025, 11, 18),
    date(2025, 11, 25),
    date(2025, 12, 2),
    date(2025, 12, 9),
    date(2025, 12, 16),
    date(2025, 12, 23),
    date(2025, 12, 30),
    # --- 2026 ---
    date(2026, 1, 6),
    date(2026, 1, 13),
    date(2026, 1, 20),
    date(2026, 1, 27),
    date(2026, 2, 3),
    date(2026, 2, 10),
    date(2026, 2, 17),
    date(2026, 2, 24),
    date(2026, 3, 2),   # shifted from 03-Mar (Holi)
    date(2026, 3, 10),
    date(2026, 3, 17),
    date(2026, 3, 24),
    date(2026, 3, 30),  # shifted from 31-Mar (Shri Mahavir Jayanti)
    date(2026, 4, 7),
    date(2026, 4, 13),  # shifted from 14-Apr (Dr. B.R. Ambedkar Jayanti)
    date(2026, 4, 21),
    date(2026, 4, 28),
    date(2026, 5, 5),
    date(2026, 5, 12),
    date(2026, 5, 19),
    date(2026, 5, 26),
    date(2026, 6, 2),
    date(2026, 6, 9),
    date(2026, 6, 16),
    date(2026, 6, 23),
    date(2026, 6, 30),
    date(2026, 7, 7),
    date(2026, 7, 14),
    date(2026, 7, 21),
    date(2026, 7, 28),
    date(2026, 8, 4),
    date(2026, 8, 11),
    date(2026, 8, 18),
    date(2026, 8, 25),
    date(2026, 9, 1),
    date(2026, 9, 8),
    date(2026, 9, 15),
    date(2026, 9, 22),
    date(2026, 9, 29),
    date(2026, 10, 6),
    date(2026, 10, 13),
    date(2026, 10, 19), # shifted from 20-Oct (Dussehra)
    date(2026, 10, 27),
    date(2026, 11, 3),
    date(2026, 11, 9),  # shifted from 10-Nov (Diwali Balipratipada)
    date(2026, 11, 17),
    date(2026, 11, 23), # shifted from 24-Nov (Prakash Gurpurb / Guru Nanak Jayanti)
    date(2026, 12, 1),
    date(2026, 12, 8),
    date(2026, 12, 15),
    date(2026, 12, 22),
    date(2026, 12, 29),
])

# ============================================
# SENSEX WEEKLY EXPIRY DATES (2023-2026)
# ============================================
# Source: BSE circulars + SEBI expiry-day mandate. Regime history:
#   15-May-2023 – 31-Dec-2024:  Fridays (weekly options launched 15-May-2023)
#   7-Jan-2025 – 26-Aug-2025:   Tuesdays (BSE revision w.e.f. 1-Jan-2025; plus 3-Jan-2025 legacy Fri)
#   4-Sep-2025 onwards:         Thursdays (SEBI expiry-day mandate w.e.f. 1-Sep-2025)
# Holiday-shifted dates included (shifted backward to previous trading day).
SENSEX_WEEKLY_EXPIRY_DATES = sorted([
    # --- 2023 (Fridays, no holiday shifts in the post-launch window) ---
    date(2023, 5, 19), date(2023, 5, 26),
    date(2023, 6, 2),  date(2023, 6, 9),  date(2023, 6, 16), date(2023, 6, 23), date(2023, 6, 30),
    date(2023, 7, 7),  date(2023, 7, 14), date(2023, 7, 21), date(2023, 7, 28),
    date(2023, 8, 4),  date(2023, 8, 11), date(2023, 8, 18), date(2023, 8, 25),
    date(2023, 9, 1),  date(2023, 9, 8),  date(2023, 9, 15), date(2023, 9, 22), date(2023, 9, 29),
    date(2023, 10, 6), date(2023, 10, 13),date(2023, 10, 20),date(2023, 10, 27),
    date(2023, 11, 3), date(2023, 11, 10),date(2023, 11, 17),date(2023, 11, 24),
    date(2023, 12, 1), date(2023, 12, 8), date(2023, 12, 15),date(2023, 12, 22),date(2023, 12, 29),
    # --- 2024 (Fridays, 5 holiday-shifted to prior Thursday) ---
    date(2024, 1, 5),  date(2024, 1, 12), date(2024, 1, 19),
    date(2024, 1, 25),  # shifted from 26-Jan (Republic Day)
    date(2024, 2, 2),  date(2024, 2, 9),  date(2024, 2, 16), date(2024, 2, 23),
    date(2024, 3, 1),
    date(2024, 3, 7),   # shifted from 8-Mar (Maha Shivaratri)
    date(2024, 3, 15), date(2024, 3, 22),
    date(2024, 3, 28),  # shifted from 29-Mar (Good Friday)
    date(2024, 4, 5),  date(2024, 4, 12), date(2024, 4, 19), date(2024, 4, 26),
    date(2024, 5, 3),  date(2024, 5, 10), date(2024, 5, 17), date(2024, 5, 24), date(2024, 5, 31),
    date(2024, 6, 7),  date(2024, 6, 14), date(2024, 6, 21), date(2024, 6, 28),
    date(2024, 7, 5),  date(2024, 7, 12), date(2024, 7, 19), date(2024, 7, 26),
    date(2024, 8, 2),  date(2024, 8, 9),  date(2024, 8, 16), date(2024, 8, 23), date(2024, 8, 30),
    date(2024, 9, 6),  date(2024, 9, 13), date(2024, 9, 20), date(2024, 9, 27),
    date(2024, 10, 4), date(2024, 10, 11),date(2024, 10, 18),date(2024, 10, 25),
    date(2024, 10, 31), # shifted from 1-Nov (Diwali Laxmi Pujan — Muhurat only)
    date(2024, 11, 8),
    date(2024, 11, 14), # shifted from 15-Nov (Guru Nanak Jayanti)
    date(2024, 11, 22),date(2024, 11, 29),
    date(2024, 12, 6), date(2024, 12, 13),date(2024, 12, 20),date(2024, 12, 27),
    # --- 2025 ---
    date(2025, 1, 3),   # legacy Friday expiry (one-time carryover)
    # Tuesday expiries (Jan 7 – Aug 26)
    date(2025, 1, 7),
    date(2025, 1, 14),
    date(2025, 1, 21),
    date(2025, 1, 28),
    date(2025, 2, 4),
    date(2025, 2, 11),
    date(2025, 2, 18),
    date(2025, 2, 25),
    date(2025, 3, 4),
    date(2025, 3, 11),
    date(2025, 3, 18),
    date(2025, 3, 25),
    date(2025, 4, 1),
    date(2025, 4, 8),
    date(2025, 4, 15),
    date(2025, 4, 22),
    date(2025, 4, 29),
    date(2025, 5, 6),
    date(2025, 5, 13),
    date(2025, 5, 20),
    date(2025, 5, 27),
    date(2025, 6, 3),
    date(2025, 6, 10),
    date(2025, 6, 17),
    date(2025, 6, 24),
    date(2025, 7, 1),
    date(2025, 7, 8),
    date(2025, 7, 15),
    date(2025, 7, 22),
    date(2025, 7, 29),
    date(2025, 8, 5),
    date(2025, 8, 12),
    date(2025, 8, 19),
    date(2025, 8, 26),
    # Thursday expiries (Sept onwards)
    date(2025, 9, 4),
    date(2025, 9, 11),
    date(2025, 9, 18),
    date(2025, 9, 25),
    date(2025, 10, 1),  # shifted from 02-Oct (Gandhi Jayanti)
    date(2025, 10, 9),
    date(2025, 10, 16),
    date(2025, 10, 23),
    date(2025, 10, 30),
    date(2025, 11, 6),
    date(2025, 11, 13),
    date(2025, 11, 20),
    date(2025, 11, 27),
    date(2025, 12, 4),
    date(2025, 12, 11),
    date(2025, 12, 18),
    date(2025, 12, 24), # shifted from 25-Dec (Christmas)
    # --- 2026 ---
    date(2026, 1, 1),
    date(2026, 1, 8),
    date(2026, 1, 14),  # shifted from 15-Jan (Municipal Corp Elections Maharashtra)
    date(2026, 1, 22),
    date(2026, 1, 29),
    date(2026, 2, 5),
    date(2026, 2, 12),
    date(2026, 2, 19),
    date(2026, 2, 26),
    date(2026, 3, 5),
    date(2026, 3, 12),
    date(2026, 3, 19),
    date(2026, 3, 25),  # shifted from 26-Mar (Shri Ram Navami)
    date(2026, 4, 2),
    date(2026, 4, 9),
    date(2026, 4, 16),
    date(2026, 4, 23),
    date(2026, 4, 30),
    date(2026, 5, 7),
    date(2026, 5, 14),
    date(2026, 5, 21),
    date(2026, 5, 27),  # shifted from 28-May (Bakri Eid)
    date(2026, 6, 4),
    date(2026, 6, 11),
    date(2026, 6, 18),
    date(2026, 6, 25),
    date(2026, 7, 2),
    date(2026, 7, 9),
    date(2026, 7, 16),
    date(2026, 7, 23),
    date(2026, 7, 30),
    date(2026, 8, 6),
    date(2026, 8, 13),
    date(2026, 8, 20),
    date(2026, 8, 27),
    date(2026, 9, 3),
    date(2026, 9, 10),
    date(2026, 9, 17),
    date(2026, 9, 24),
    date(2026, 10, 1),
    date(2026, 10, 8),
    date(2026, 10, 15),
    date(2026, 10, 22),
    date(2026, 10, 29),
    date(2026, 11, 5),
    date(2026, 11, 12),
    date(2026, 11, 19),
    date(2026, 11, 26),
    date(2026, 12, 3),
    date(2026, 12, 10),
    date(2026, 12, 17),
    date(2026, 12, 24),
    date(2026, 12, 31),
])

# Pre-build a set for O(1) lookups
_EXPIRY_SET = set(NIFTY_WEEKLY_EXPIRY_DATES)


def get_nearest_weekly_expiry(d):
    """Return the nearest upcoming (or same-day) weekly expiry for a given date."""
    if isinstance(d, datetime):
        d = d.date()
    for exp in NIFTY_WEEKLY_EXPIRY_DATES:
        if exp >= d:
            return exp
    return None


def get_expiry_day_type(d):
    """Classify a trading date relative to its nearest weekly expiry.

    Returns:
        "expiry"   — d is the expiry day itself
        "expiry-1" — d is the trading day immediately before expiry
        "other"    — all other days
    """
    if isinstance(d, datetime):
        d = d.date()
    if d in _EXPIRY_SET:
        return "expiry"
    nearest = get_nearest_weekly_expiry(d)
    if nearest is None:
        return "other"
    # Check if d is the trading day right before expiry (1 calendar day for Tue→Mon,
    # but could be more if weekend/holiday falls between). We check: is there no
    # expiry date between d and nearest, and is d the previous trading day?
    # Simple approach: d is expiry-1 if nearest expiry is within 1-3 calendar days
    # and no other trading day sits between them. For robustness, just check
    # if the next calendar business day from d would be the expiry.
    diff = (nearest - d).days
    if diff == 1:
        return "expiry-1"
    # Handle weekends: Friday before a Monday (shifted) expiry → diff=3
    # d is expiry-1 only if d is the last trading day before expiry.
    # Monday before Tuesday expiry → diff=1 (covered above)
    # Friday before Monday expiry (shifted) → diff=3, weekday=4 (Friday)
    if diff == 3 and d.weekday() == 4:  # Friday before Monday expiry
        return "expiry-1"
    return "other"
