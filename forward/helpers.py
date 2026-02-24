"""
Shared helpers for the forward test module.

Event builders and timestamp utilities used by engine, warmup,
tick_checker, etc.
"""

import pytz
from datetime import datetime

IST = pytz.timezone('Asia/Kolkata')


def make_event(event_type: str, message: str, option_type: str = "",
               **extra) -> dict:
    """Build a standardised event dict for logging and UI."""
    return {
        "time": datetime.now(IST),
        "type": event_type,       # signal | entry | exit | info | error
        "option_type": option_type,
        "message": message,
        **extra,
    }


def safe_timestamp(ts) -> datetime:
    """
    Convert a pandas Timestamp (or similar) to a Python datetime safely.

    Avoids the 'Discarding nonzero nanoseconds' warning by flooring to
    microsecond precision before conversion.
    """
    if hasattr(ts, 'floor'):
        # pandas Timestamp — floor nanoseconds before converting
        ts = ts.floor('us').to_pydatetime()
    elif hasattr(ts, 'to_pydatetime'):
        ts = ts.to_pydatetime()
    # Ensure timezone-aware (IST)
    if hasattr(ts, 'tzinfo') and ts.tzinfo is None:
        ts = IST.localize(ts)
    return ts
