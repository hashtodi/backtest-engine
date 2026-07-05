"""Tests for the Volume-Breakout Re-entry engine.

Execution tests inject a fixed breakout `level` via `_daily_level`. The ENTRY fill is the
BASE for all SL/TP/lock/re-entry levels, and every trigger is evaluated on the 1-min CLOSE
(never high/low), filling at the exact level. One bar per "day".
"""

import datetime
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.volbreakout_reentry_backtest import VolBreakoutReentryEngine


def make_df(bars):
    """bars: (date 'YYYY-MM-DD', time 'HH:MM', open, high, low, close[, volume])."""
    rows = []
    for b in bars:
        d, t, o, h, l, c = b[:6]
        v = b[6] if len(b) > 6 else 1000
        rows.append({'date': datetime.date.fromisoformat(d), 'dt_str': f'{d} {t}',
                     'open': float(o), 'high': float(h), 'low': float(l), 'close': float(c),
                     'volume': float(v)})
    return pd.DataFrame(rows)


def run_exec(bars, earning, level, bdate='2026-01-01', **kw):
    eng = VolBreakoutReentryEngine({'X': earning}, **kw)
    df = make_df(bars)
    eng._load_stock = lambda s: df
    eng._daily_level = lambda d, R: (level, datetime.date.fromisoformat(bdate))
    return eng._run_stock('X', earning)


# ---------------------------------------------------------------- level detection

def test_level_detection_latest_breakout():
    bars = [
        ('2025-12-01', '09:15', 100, 120, 99, 119, 100),
        ('2025-12-02', '09:15', 100, 121, 99, 120, 120),
        ('2025-12-03', '09:15', 100, 122, 99, 121, 110),
        ('2025-12-04', '09:15', 100, 123, 99, 122, 100),
        ('2025-12-05', '09:15', 100, 135, 99, 134, 700),   # breakout, high=135
        ('2025-12-06', '09:15', 100, 124, 99, 123, 150),
    ]
    eng = VolBreakoutReentryEngine({}, vol_ma_period=3, vol_mult=5.0)
    level, bdate = eng._daily_level(make_df(bars), datetime.date(2025, 12, 10))
    assert level == 135.0 and str(bdate) == '2025-12-05'


def test_no_level_when_no_breakout():
    bars = [(f'2025-12-0{i}', '09:15', 100, 120, 99, 119, 100) for i in range(1, 7)]
    eng = VolBreakoutReentryEngine({}, vol_ma_period=3, vol_mult=5.0)
    level, _ = eng._daily_level(make_df(bars), datetime.date(2025, 12, 10))
    assert level is None


# ---------------------------------------------------------------- entry (base = fill)

def test_entry_open_below_level_then_tp():
    # open 98 < breakout-high 100 -> base = 98. TP = 98*1.08 = 105.84 (close-triggered).
    bars = [
        ('2026-01-02', '09:15', 98, 99, 97, 98),       # entry base 98
        ('2026-01-05', '09:15', 100, 110, 100, 106),   # close 106 >= 105.84 -> TP @ 105.84
    ]
    t = run_exec(bars, '2026-01-01', level=100.0)
    assert len(t) == 1
    tr = t[0]
    assert tr.leg == 1 and tr.level == 100.0 and tr.entry_price == 98.0
    assert tr.sl_init == 93.1 and tr.tp_price == 105.84
    assert tr.exit_reason == 'TP' and tr.exit_price == 105.84
    assert tr.qty == int(100000 // 98)


def test_entry_touch_close_when_open_above():
    # open 103 >= breakout-high 100 -> wait for a CLOSE <= 100, enter @ 100 (base = 100).
    bars = [
        ('2026-01-02', '09:15', 103, 105, 101, 104),   # close 104 > 100, no entry
        ('2026-01-05', '09:15', 102, 103, 98, 99),     # close 99 <= 100 -> entry @ 100
        ('2026-01-06', '09:15', 101, 109, 101, 108),   # close 108 >= TP 108 -> TP
    ]
    t = run_exec(bars, '2026-01-01', level=100.0)
    assert t[0].entry_price == 100.0
    assert t[0].entry_time.startswith('2026-01-05')
    assert t[0].exit_reason == 'TP' and t[0].exit_price == 108.0


def test_no_entry_when_open_above_and_close_never_reaches_level():
    bars = [
        ('2026-01-02', '09:15', 103, 105, 101, 104),
        ('2026-01-05', '09:15', 106, 110, 102, 108),
    ]
    t = run_exec(bars, '2026-01-01', level=100.0)
    assert t[0].exit_reason == 'NO_ENTRY'


# ---------------------------------------------------------------- exits (close-based)

def test_initial_sl_on_close():
    bars = [
        ('2026-01-02', '09:15', 98, 99, 97, 98),       # base 98, SL 93.1
        ('2026-01-05', '09:15', 95, 96, 92, 93),       # close 93 <= 93.1 -> SL @ 93.1
    ]
    t = run_exec(bars, '2026-01-01', level=100.0)
    assert t[0].exit_reason == 'SL'
    assert t[0].locked_sl == 93.1 and t[0].exit_price == 93.1
    assert t[0].pnl < 0


def test_profit_lock_ratchets_then_locked_exit():
    # base 100 (open 100 < breakout-high 120). close 106 ratchets lock to 102; close 102 exits.
    bars = [
        ('2026-01-02', '09:15', 100, 100, 100, 100),   # entry base 100
        ('2026-01-05', '09:15', 100, 107, 100, 106),   # close 106 -> lock 101 then 102
        ('2026-01-06', '09:15', 103, 104, 101, 102),   # close 102 <= 102 -> SL @ 102
    ]
    t = run_exec(bars, '2026-01-01', level=120.0)
    assert t[0].entry_price == 100.0
    assert t[0].exit_reason == 'SL'
    assert t[0].locked_sl == 102.0 and t[0].exit_price == 102.0


def test_lock_uses_close_not_high():
    # A bar whose HIGH is 107 but CLOSE is 101 only arms the 101 lock (close-based), not 103.
    bars = [
        ('2026-01-02', '09:15', 100, 100, 100, 100),   # base 100
        ('2026-01-05', '09:15', 100, 107, 100, 101),   # high 107 ignored; close 101 -> no lock (needs >=105)
        ('2026-01-06', '09:15', 100, 100, 94, 95),     # close 95 <= SL 95 -> SL @ 95 (no lock armed)
    ]
    t = run_exec(bars, '2026-01-01', level=120.0)
    assert t[0].exit_reason == 'SL'
    assert t[0].locked_sl == 95.0   # initial stop, lock never armed (close stayed < 105)


# ---------------------------------------------------------------- re-entry

def test_reentry_after_stop_close_below_sl_then_above_base():
    bars = [
        ('2026-01-02', '09:15', 98, 99, 97, 98),       # leg1 base 98
        ('2026-01-05', '09:15', 95, 96, 92, 93),       # close 93 <= 93.1 -> SL (leg1)
        ('2026-01-06', '09:15', 92, 93, 89, 90),       # close 90 < 93.1 -> armed
        ('2026-01-07', '09:15', 97, 100, 96, 99),      # close 99 > base 98 -> RE-ENTER @ 98
        ('2026-01-08', '09:15', 100, 110, 100, 106),   # close 106 >= 105.84 -> leg2 TP
    ]
    t = run_exec(bars, '2026-01-01', level=100.0)
    assert len(t) == 2
    assert t[0].leg == 1 and t[0].exit_reason == 'SL'
    assert t[1].leg == 2 and t[1].entry_price == 98.0
    assert t[1].entry_time.startswith('2026-01-07')
    assert t[1].exit_reason == 'TP'


def test_no_reentry_without_close_below_sl():
    bars = [
        ('2026-01-02', '09:15', 98, 99, 97, 98),       # base 98
        ('2026-01-05', '09:15', 95, 96, 92, 93),       # SL @ 93.1
        ('2026-01-06', '09:15', 95, 96, 95, 95),       # close 95, never < 93.1 (no arm)
        ('2026-01-07', '09:15', 99, 101, 99, 100),     # close 100 > 98 but not armed -> no re-entry
    ]
    t = run_exec(bars, '2026-01-01', level=100.0)
    assert len(t) == 1 and t[0].exit_reason == 'SL'


def test_no_reentry_after_tp():
    bars = [
        ('2026-01-02', '09:15', 98, 99, 97, 98),       # base 98
        ('2026-01-05', '09:15', 100, 110, 100, 106),   # TP
        ('2026-01-06', '09:15', 92, 93, 89, 90),       # close < 93.1
        ('2026-01-07', '09:15', 97, 100, 96, 99),      # close > 98 -> but NO re-entry (was TP)
    ]
    t = run_exec(bars, '2026-01-01', level=100.0)
    assert len(t) == 1 and t[0].exit_reason == 'TP'


def test_at_most_one_reentry():
    bars = [
        ('2026-01-02', '09:15', 98, 99, 97, 98),       # leg1 base 98
        ('2026-01-05', '09:15', 95, 96, 92, 93),       # leg1 SL
        ('2026-01-06', '09:15', 92, 93, 89, 90),       # arm
        ('2026-01-07', '09:15', 97, 100, 96, 99),      # re-enter leg2 @ 98
        ('2026-01-08', '09:15', 95, 96, 92, 93),       # leg2 SL
        ('2026-01-09', '09:15', 92, 93, 89, 90),       # arm again
        ('2026-01-12', '09:15', 97, 100, 96, 99),      # would re-enter, but max 1 -> ignored
    ]
    t = run_exec(bars, '2026-01-01', level=100.0)
    assert len(t) == 2 and [x.leg for x in t] == [1, 2]


# ---------------------------------------------------------------- misc

def test_time_exit_at_cutoff():
    bars = [
        ('2026-01-02', '09:15', 99, 100, 99, 100),     # base 99
        ('2026-01-05', '09:15', 100, 104, 99, 103),    # last bar within cutoff -> TIME
        ('2026-01-09', '09:15', 103, 105, 102, 104),   # beyond cutoff (capped)
    ]
    t = run_exec(bars, '2026-01-01', level=100.0, max_hold_days=5)
    assert t[0].exit_reason == 'TIME'
    assert t[0].exit_time.startswith('2026-01-05') and t[0].exit_price == 103.0


def test_no_level_stub():
    eng = VolBreakoutReentryEngine({'X': '2026-01-01'})
    eng._load_stock = lambda s: make_df([('2026-01-02', '09:15', 99, 100, 98, 99)])
    eng._daily_level = lambda d, R: (None, None)
    assert eng._run_stock('X', '2026-01-01')[0].exit_reason == 'NO_LEVEL'
