"""
Expiry calendar lookup for NIFTY and SENSEX options.

Loads pre-computed expiry dates from data/expiry_calendar.json.
Provides helpers to find the next monthly expiry, compute days-to-expiry,
and pick the right expiry_code for a trading day.

Usage:
    from engine.expiry_calendar import get_monthly_expiry, get_expiry_code

    # Next monthly expiry from a date
    expiry = get_monthly_expiry("nifty", date(2026, 2, 10))
    # -> date(2026, 2, 24)

    # Which expiry_code to use (>= 15 days rule)
    code = get_expiry_code("nifty", date(2026, 2, 10))
    # -> 1  (Feb 24 is 14 days away, < 15 -> code 2? No: 24-10=14 -> code=2)
"""

import bisect
import json
import logging
import os
from datetime import date, timedelta
from typing import Optional, List

import pandas as pd

logger = logging.getLogger(__name__)

# Path to the JSON calendar file (relative to project root)
_CALENDAR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "expiry_calendar.json"
)

# Cached calendar data (loaded once)
_calendar = None


def _load_calendar() -> dict:
    """Load and cache the expiry calendar from JSON."""
    global _calendar
    if _calendar is not None:
        return _calendar

    with open(_CALENDAR_PATH) as f:
        _calendar = json.load(f)

    logger.info(f"Loaded expiry calendar from {_CALENDAR_PATH}")
    return _calendar


def _to_date(d) -> date:
    """Convert various date types to datetime.date."""
    if isinstance(d, date) and not isinstance(d, pd.Timestamp):
        return d
    return pd.Timestamp(d).date()


def get_holidays() -> set:
    """Return all trading holidays as a set of date objects."""
    cal = _load_calendar()
    holidays = set()
    for year_holidays in cal["holidays"].values():
        for h in year_holidays:
            holidays.add(date.fromisoformat(h))
    return holidays


def get_monthly_expiries(instrument: str) -> List[date]:
    """
    Return all monthly expiry dates for an instrument.

    Args:
        instrument: "nifty" or "sensex" (case-insensitive)

    Returns:
        Sorted list of monthly expiry dates.
    """
    cal = _load_calendar()
    key = instrument.lower()
    return [date.fromisoformat(d) for d in cal[key]["monthly"]]


def get_weekly_expiries(instrument: str) -> List[date]:
    """Return all weekly expiry dates for an instrument."""
    cal = _load_calendar()
    key = instrument.lower()
    return [date.fromisoformat(d) for d in cal[key]["weekly"]]


def get_monthly_expiry(instrument: str, trading_date) -> Optional[date]:
    """
    Find the next monthly expiry on or after a given trading date.

    Args:
        instrument: "nifty" or "sensex"
        trading_date: the current trading day

    Returns:
        The nearest monthly expiry date >= trading_date, or None if not found.
    """
    trading_date = _to_date(trading_date)
    expiries = get_monthly_expiries(instrument)
    for exp in expiries:
        if exp >= trading_date:
            return exp
    return None


def get_next_monthly_expiry(instrument: str, trading_date) -> Optional[date]:
    """
    Find the second monthly expiry on or after a given trading date.
    (The month after the nearest one.)

    Args:
        instrument: "nifty" or "sensex"
        trading_date: the current trading day

    Returns:
        The second-nearest monthly expiry date, or None if not found.
    """
    trading_date = _to_date(trading_date)
    expiries = get_monthly_expiries(instrument)
    found_first = False
    for exp in expiries:
        if exp >= trading_date:
            if found_first:
                return exp
            found_first = True
    return None


def days_to_expiry(instrument: str, trading_date) -> Optional[int]:
    """
    Calendar days from trading_date to the nearest monthly expiry.

    Returns None if no expiry is found.
    """
    trading_date = _to_date(trading_date)
    exp = get_monthly_expiry(instrument, trading_date)
    if exp is None:
        return None
    return (exp - trading_date).days


def get_expiry_code(instrument: str, trading_date) -> int:
    """
    Pick the right monthly expiry_code based on the >= 15 days rule.

    Rule: use nearest monthly expiry (code=1) if >= 15 calendar days
    remain. Otherwise use next month (code=2).

    If the current date is ON the expiry date itself, days_left=0 < 15,
    so code=2. If the expiry has passed (day after), the nearest expiry
    is next month — re-evaluate from there.

    Args:
        instrument: "nifty" or "sensex"
        trading_date: date object for the trading day

    Returns:
        1 (nearest monthly) or 2 (next month's monthly)
    """
    trading_date = _to_date(trading_date)
    dte = days_to_expiry(instrument, trading_date)
    if dte is None:
        return 1
    return 1 if dte >= 15 else 2


def _weekly_dates(instrument: str) -> List[date]:
    """SENSEX and NIFTY weekly dates come from config (complete, holiday-shifted
    lists back to launch); other instruments come from the JSON calendar."""
    inst = instrument.lower()
    if inst == "sensex":
        from config import SENSEX_WEEKLY_EXPIRY_DATES  # lazy import; avoids load-order issues
        return sorted(SENSEX_WEEKLY_EXPIRY_DATES)
    if inst == "nifty":
        from config import NIFTY_WEEKLY_EXPIRY_DATES
        return sorted(NIFTY_WEEKLY_EXPIRY_DATES)
    return get_weekly_expiries(instrument)


def get_weekly_expiry(instrument: str, trading_date) -> Optional[date]:
    """Nearest weekly expiry on or after trading_date, or None."""
    trading_date = _to_date(trading_date)
    for exp in _weekly_dates(instrument):
        if exp >= trading_date:
            return exp
    return None


def days_to_weekly_expiry(instrument: str, trading_date) -> Optional[int]:
    """Calendar days from trading_date to the nearest weekly expiry, or None."""
    trading_date = _to_date(trading_date)
    exp = get_weekly_expiry(instrument, trading_date)
    if exp is None:
        return None
    return (exp - trading_date).days


def trading_days_to_weekly_expiry(instrument: str, trading_date, sessions) -> Optional[int]:
    """Trading SESSIONS from trading_date to the nearest weekly expiry, or None.

    Counts the ACTUAL sessions in the half-open interval (trading_date, expiry]
    using `sessions` — a SORTED sequence of real session dates (the dates present
    in the options data). Weekends and holidays are excluded automatically because
    they simply aren't sessions, so this needs no separate holiday list and stays
    exactly consistent with the data. DTE 0 = the expiry day itself, 1 = the
    session before it, etc.
    """
    trading_date = _to_date(trading_date)
    exp = get_weekly_expiry(instrument, trading_date)
    if exp is None:
        return None
    lo = bisect.bisect_right(sessions, trading_date)   # first session > trading_date
    hi = bisect.bisect_right(sessions, exp)            # first session > expiry
    return hi - lo


def pick_expiry_code(instrument: str, day_data: pd.DataFrame, trading_date) -> Optional[int]:
    """
    Determine the monthly expiry_code for a trading day.

    Uses get_expiry_code() to pick the correct code based on the
    >= 15 days rule. Returns None if the data doesn't have ATM rows
    for that code — no fallback to a different expiry.

    Args:
        instrument: "nifty" or "sensex"
        day_data: DataFrame for a single trading day (all codes)
        trading_date: date object

    Returns:
        The correct expiry_code (int), or None if data is missing.
    """
    preferred = get_expiry_code(instrument, trading_date)

    # Only use the preferred code if it has ATM data. No fallback.
    preferred_atm = day_data[
        (day_data['expiry_code'] == preferred) &
        (day_data['moneyness'] == 'ATM')
    ]
    if len(preferred_atm) > 0:
        return preferred

    return None
