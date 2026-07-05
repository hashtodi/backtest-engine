"""Derive true NIFTY weekly expiry dates from the option/spot data.

Rule: Thursday before 2025-09-01, Tuesday on/after (SEBI switch). If the target
weekday is a market holiday (not a trading day in the data), roll to the previous
trading day. Validate each against the option file (code-1 ATM straddle collapse).

Usage:
    python scripts/build_weekly_expiry_calendar.py            # print date(...) lines + report
"""
import sys
from datetime import date, timedelta

import pandas as pd

SWITCH = date(2025, 9, 1)  # Thursday(3) before, Tuesday(1) on/after
SPOT_PATH = "data/spot/nifty/NIFTY_1m.parquet"
OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"

# Exchange moved expiry EARLIER than the weekday rule predicts. These are Diwali
# special/Muhurat days: the nominal expiry weekday still had a (partial) session, so
# the "roll back only if not a trading day" rule keeps it — but NSE settled the weekly
# a day earlier. Verified against the option file (ATM straddle collapse). {wrong: right}
KNOWN_FIXES = {
    date(2021, 11, 4): date(2021, 11, 3),   # Diwali Laxmi Pujan (Thu) -> expiry Wed
    date(2025, 10, 21): date(2025, 10, 20),  # Diwali (Tue) -> expiry Mon
}


def trading_days_from_parquet(path: str = SPOT_PATH) -> list:
    df = pd.read_parquet(path, columns=["datetime"])
    d = pd.to_datetime(df["datetime"].str.slice(0, 10), format="%Y-%m-%d")
    return sorted(set(d.dt.date))


def derive_weekly_expiries(trading_days: list, switch: date = SWITCH) -> list:
    trading = set(trading_days)
    first, last = trading_days[0], trading_days[-1]
    out = set()
    d = first
    while d <= last:
        target_wd = 3 if d < switch else 1  # Thu / Tue
        if d.weekday() == target_wd:
            e = d
            while e >= first and e not in trading:
                e -= timedelta(days=1)
            if e in trading:
                out.add(e)
        d += timedelta(days=1)
    return sorted(out)


def apply_known_fixes(expiries: list) -> list:
    """Replace weekday-rule dates the exchange shifted earlier (see KNOWN_FIXES)."""
    s = set(expiries)
    for wrong, right in KNOWN_FIXES.items():
        if wrong in s:
            s.discard(wrong)
            s.add(right)
    return sorted(s)


def validate_expiry(options_path: str, expiry: date) -> dict:
    """Confirm code-1 ATM straddle collapses on `expiry` vs the next trading day."""
    lo = pd.Timestamp(expiry) - pd.Timedelta(days=1)
    hi = pd.Timestamp(expiry) + pd.Timedelta(days=5)
    df = pd.read_parquet(options_path,
        columns=["datetime", "option_type", "expiry_type", "expiry_code",
                 "strike_offset", "close"],
        filters=[("expiry_type", "==", "WEEK"), ("expiry_code", "==", 1),
                 ("strike_offset", "==", 0)])
    dt = pd.to_datetime(df["datetime"].str.slice(0, 19))
    df = df.assign(_dt=dt, _date=dt.dt.date)
    df = df[(df["_date"] >= lo.date()) & (df["_date"] <= hi.date())]
    straddle = {}
    for dd, g in df.groupby("_date"):
        last = g[g["_dt"] == g["_dt"].max()]
        straddle[dd] = float(last["close"].sum())
    days = sorted(straddle)
    if expiry not in straddle:
        return {"expiry": expiry, "ok": False, "reason": "no data"}
    nxt = [x for x in days if x > expiry]
    exp_val = straddle[expiry]
    nxt_val = straddle[nxt[0]] if nxt else None
    ok = nxt_val is None or (exp_val > 0 and nxt_val / exp_val > 1.8)
    return {"expiry": expiry, "straddle": round(exp_val, 1),
            "next_straddle": round(nxt_val, 1) if nxt_val else None, "ok": ok}


def main():
    tdays = trading_days_from_parquet()
    exp = apply_known_fixes(derive_weekly_expiries(tdays))
    pre = [e for e in exp if e.year < 2025]        # missing years to add to config
    print(f"# derived {len(exp)} weekly expiries {exp[0]} .. {exp[-1]}", file=sys.stderr)
    print(f"# {len(pre)} pre-2025 dates to insert into config.NIFTY_WEEKLY_EXPIRY_DATES", file=sys.stderr)
    # validate a spread-out sample against the option file
    sample = pre[::40] + [e for e in exp if e.year >= 2025][:2]
    for s in sample:
        print("#", validate_expiry(OPTIONS_PATH, s), file=sys.stderr)
    # emit the date(...) lines for the pre-2025 block
    for e in pre:
        print(f"    date({e.year}, {e.month}, {e.day}),")


if __name__ == "__main__":
    main()
