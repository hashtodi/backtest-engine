"""Tests for the SENSEX dual short-premium backtest."""
from datetime import date as ddate, time as dtime, datetime as dt

import numpy as np
import pandas as pd

from engine.expiry_calendar import (
    get_weekly_expiry, days_to_weekly_expiry, trading_days_to_weekly_expiry,
)
from engine.sensex_dual_short_backtest import (
    pick_row, select_locked_strikes, compute_range, detect_breakouts,
    simulate_leg, load_spot, process_day, SensexDualShortBacktest, summarize,
    classify_day_buckets, bucket_summary,
)
from config import SPOT_DATA_PATH

IST = "Asia/Kolkata"
SPOT_PATH = SPOT_DATA_PATH["SENSEX"]


# ---------------------------------------------------------------------------
# Task 1: weekly DTE
# ---------------------------------------------------------------------------
def test_weekly_dte_friday_regime_2023():
    assert get_weekly_expiry("SENSEX", ddate(2023, 6, 2)) == ddate(2023, 6, 2)
    assert days_to_weekly_expiry("SENSEX", ddate(2023, 6, 2)) == 0        # expiry day
    assert days_to_weekly_expiry("SENSEX", ddate(2023, 6, 1)) == 1        # Thursday before
    assert days_to_weekly_expiry("SENSEX", ddate(2023, 5, 30)) == 3       # Tuesday before


def test_weekly_dte_thursday_regime_2025():
    assert days_to_weekly_expiry("SENSEX", ddate(2025, 9, 4)) == 0
    assert days_to_weekly_expiry("SENSEX", ddate(2025, 9, 1)) == 3        # Monday before
    assert days_to_weekly_expiry("SENSEX", ddate(2025, 9, 5)) == 6       # day after -> skipped


def test_trading_days_to_weekly_expiry_tuesday_regime():
    # SENSEX 2025-01-28 is a Tuesday weekly expiry; real sessions that week are
    # Wed 22, Thu 23, Fri 24, Mon 27, Tue 28 (weekend 25-26 not sessions).
    sess = [ddate(2025, 1, 22), ddate(2025, 1, 23), ddate(2025, 1, 24),
            ddate(2025, 1, 27), ddate(2025, 1, 28)]
    f = lambda d: trading_days_to_weekly_expiry("SENSEX", d, sess)
    assert f(ddate(2025, 1, 28)) == 0    # expiry day
    assert f(ddate(2025, 1, 27)) == 1    # Mon
    assert f(ddate(2025, 1, 24)) == 2    # Fri  (calendar would be 4)
    assert f(ddate(2025, 1, 23)) == 3    # Thu  (calendar would be 5)
    assert f(ddate(2025, 1, 22)) == 4    # Wed -> skipped under 0-3
    # calendar counting inflates the pre-weekend sessions:
    assert days_to_weekly_expiry("SENSEX", ddate(2025, 1, 24)) == 4


def test_trading_dte_excludes_a_holiday_in_the_runup():
    # If a mid-week session is a holiday (absent from `sessions`), the days before
    # it move one DTE closer. Tue-28 expiry, but Thu-23 is a holiday (dropped):
    sess = [ddate(2025, 1, 22), ddate(2025, 1, 24), ddate(2025, 1, 27), ddate(2025, 1, 28)]
    f = lambda d: trading_days_to_weekly_expiry("SENSEX", d, sess)
    assert f(ddate(2025, 1, 24)) == 2    # Fri: {Mon27, Tue28}
    assert f(ddate(2025, 1, 22)) == 3    # Wed: {Fri24, Mon27, Tue28} (Thu holiday not counted)


def test_weekly_expiry_none_when_past_end():
    assert get_weekly_expiry("SENSEX", ddate(2099, 1, 1)) is None
    assert days_to_weekly_expiry("SENSEX", ddate(2099, 1, 1)) is None


# ---------------------------------------------------------------------------
# Task 2: strike selection
# ---------------------------------------------------------------------------
def _slice_0945():
    rows = []
    for off in (-6, 0, 6):
        strike = 78300 + off * 100
        for ot, close in (("CE", 100 + off), ("PE", 100 - off)):
            rows.append({"option_type": ot, "strike_offset": off,
                         "strike": float(strike), "close": float(close)})
    return pd.DataFrame(rows)


def test_pick_row_hit_and_miss():
    s = _slice_0945()
    assert pick_row(s, "CE", 6)["strike"] == 78900.0
    assert pick_row(s, "PE", -6)["strike"] == 77700.0
    assert pick_row(s, "CE", 3) is None


def test_select_locked_strikes_maps_offsets():
    picks = select_locked_strikes(_slice_0945())
    assert picks["p1_ce"]["strike"] == 78900.0
    assert picks["p1_pe"]["strike"] == 77700.0
    assert picks["p2_ce"]["strike"] == 78300.0
    assert picks["p2_pe"]["strike"] == 78300.0


def test_select_locked_strikes_missing_offset_is_none():
    s = _slice_0945()
    s = s[s["strike_offset"] != 6]
    picks = select_locked_strikes(s)
    assert picks["p1_ce"] is None
    assert picks["p1_pe"]["strike"] == 77700.0


# ---------------------------------------------------------------------------
# Task 3: range
# ---------------------------------------------------------------------------
def _spot_day(rows):
    return pd.DataFrame([
        {"time_only": dtime(hh, mm), "high": float(h), "low": float(l)}
        for (hh, mm, h, l) in rows
    ])


def test_compute_range_inclusive_window():
    sd = _spot_day([
        (9, 44, 999, 1),
        (9, 45, 100, 90),
        (10, 30, 120, 80),
        (11, 45, 110, 70),
        (11, 46, 500, 5),
    ])
    hi, lo = compute_range(sd)
    assert hi == 120.0 and lo == 70.0


def test_compute_range_empty_window_is_nan():
    sd = _spot_day([(12, 0, 100, 90)])
    hi, lo = compute_range(sd)
    assert np.isnan(hi) and np.isnan(lo)


# ---------------------------------------------------------------------------
# Task 4: breakout
# ---------------------------------------------------------------------------
def _spot_day_bt(rows):
    out = []
    for (hh, mm, h, l) in rows:
        out.append({"datetime": f"{hh:02d}:{mm:02d}", "time_only": dtime(hh, mm),
                    "high": float(h), "low": float(l)})
    return pd.DataFrame(out)


def test_breakout_high_then_low_whipsaw():
    sd = _spot_day_bt([
        (11, 45, 105, 95),
        (12, 0, 106, 96),
        (12, 30, 111, 96),
        (14, 10, 104, 89),
    ])
    put_dt, call_dt = detect_breakouts(sd, range_high=110, range_low=90)
    assert put_dt == "12:30"
    assert call_dt == "14:10"


def test_breakout_engulfing_bar_fires_both():
    sd = _spot_day_bt([(12, 0, 111, 89)])
    put_dt, call_dt = detect_breakouts(sd, range_high=110, range_low=90)
    assert put_dt == "12:00" and call_dt == "12:00"


def test_breakout_none_when_inside_range():
    sd = _spot_day_bt([(12, 0, 109, 91), (13, 0, 108, 92)])
    assert detect_breakouts(sd, 110, 90) == (None, None)


def test_breakout_ignores_1145_and_after_1528():
    sd = _spot_day_bt([(11, 45, 200, 1), (15, 29, 200, 1)])
    assert detect_breakouts(sd, 110, 90) == (None, None)


# ---------------------------------------------------------------------------
# Task 5: leg state machine
# ---------------------------------------------------------------------------
def test_leg_target_hit_no_reentry():
    highs = [100, 110, 110, 110]
    lows = [100, 90, 50, 9]
    closes = [100, 100, 100, 100]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=3)
    assert len(trips) == 1
    assert trips[0]["exit_reason"] == "TARGET"
    assert trips[0]["exit_price"] == 10.0
    assert trips[0]["pnl_points"] == 90.0


def test_leg_sl_then_reentry_then_target():
    highs = [100, 130, 120, 120, 120]
    lows = [100, 120, 118, 100, 9]
    closes = [100, 100, 100, 100, 100]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=4)
    assert [t["entry_kind"] for t in trips] == ["INITIAL", "REENTRY"]
    assert trips[0]["exit_reason"] == "SL" and trips[0]["exit_price"] == 125.0
    assert trips[0]["pnl_points"] == -25.0
    assert trips[1]["entry_price"] == 100.0
    assert trips[1]["exit_reason"] == "TARGET" and trips[1]["pnl_points"] == 90.0


def test_leg_sl_then_reentry_then_sl_no_third():
    highs = [100, 130, 120, 130, 130]
    lows = [100, 120, 100, 120, 120]
    closes = [100, 100, 100, 100, 100]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=4)
    assert len(trips) == 2
    assert trips[0]["exit_reason"] == "SL"
    assert trips[1]["exit_reason"] == "SL"
    assert sum(t["pnl_points"] for t in trips) == -50.0


def test_leg_same_bar_sl_and_target_sl_wins():
    highs = [100, 130]
    lows = [100, 9]
    closes = [100, 100]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=1)
    assert trips[0]["exit_reason"] == "SL"


def test_leg_eod_when_nothing_hits():
    highs = [100, 110, 110]
    lows = [100, 90, 90]
    closes = [100, 100, 77]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=2)
    assert len(trips) == 1
    assert trips[0]["exit_reason"] == "EOD"
    assert trips[0]["exit_price"] == 77.0
    assert trips[0]["pnl_points"] == 23.0


def test_leg_sl_but_never_returns_to_cost_no_reentry():
    highs = [100, 130, 128, 129]
    lows = [100, 126, 127, 128]
    closes = [100, 127, 127, 128]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=3)
    assert len(trips) == 1 and trips[0]["exit_reason"] == "SL"


# ---------------------------------------------------------------------------
# Task 6: spot loader
# ---------------------------------------------------------------------------
def test_load_spot_has_helper_cols_and_ist_time():
    sp = load_spot(SPOT_PATH, "2025-09-04", "2025-09-04")
    assert {"date", "time_only", "high", "low", "close"}.issubset(sp.columns)
    assert sp["date"].nunique() == 1 and sp["date"].iloc[0] == ddate(2025, 9, 4)
    assert (sp["time_only"] == dtime(9, 45)).any()
    assert sp["time_only"].min() >= dtime(9, 0)


# ---------------------------------------------------------------------------
# Task 7: day processor
# ---------------------------------------------------------------------------
def _mk_opt_row(t, ot, off, strike, o, h, l, c):
    return {"datetime": pd.Timestamp(t, tz=IST), "time_only": t.time(),
            "date": t.date(), "option_type": ot, "strike_offset": off,
            "strike": float(strike), "open": o, "high": h, "low": l, "close": c,
            "atm_strike": 78300.0}


def _mk_spot_row(t, o, h, l, c):
    return {"datetime": pd.Timestamp(t, tz=IST), "time_only": t.time(),
            "date": t.date(), "open": o, "high": h, "low": l, "close": c}


def test_process_day_part1_target_and_skip_ledger():
    d = ddate(2025, 9, 4)
    opts, spot = [], []
    times = [dt(2025, 9, 4, 9, 45), dt(2025, 9, 4, 9, 46), dt(2025, 9, 4, 15, 28)]
    pe_prices = {times[0]: (100, 100, 100, 100),
                 times[1]: (100, 100, 9, 50),
                 times[2]: (50, 50, 50, 50)}
    for t in times:
        o, h, l, c = pe_prices[t]
        opts.append(_mk_opt_row(t, "PE", -6, 77700, o, h, l, c))
        spot.append(_mk_spot_row(t, 78300, 78300, 78300, 78300))
    day_opts = pd.DataFrame(opts)
    spot_day = pd.DataFrame(spot)

    trades, skips = process_day(day_opts, spot_day, d, dte=0)

    pe_trades = [t for t in trades if t["part"] == "P1" and t["side"] == "PE"]
    assert len(pe_trades) == 1
    assert pe_trades[0]["exit_reason"] == "TARGET"
    assert pe_trades[0]["pnl_inr"] == 1800.0
    assert skips.get("P1_CE_UNAVAILABLE_0945") == 1
    assert skips.get("P2_ATM_NOT_RECORDABLE_0945") == 1


# ---------------------------------------------------------------------------
# Task 8: engine run loop
# ---------------------------------------------------------------------------
def test_engine_runs_small_window_and_gates_dte():
    eng = SensexDualShortBacktest("2025-09-01", "2025-09-05")
    eng.run()
    assert isinstance(eng.trades, list)
    if eng.trades:
        t = eng.trades[0]
        assert {"date", "part", "side", "strike", "entry_kind", "entry_cost",
                "sl_level", "target_level", "exit_reason", "pnl_inr"}.issubset(t)
    assert all(str(t["date"]) != "2025-09-05" for t in eng.trades)
    assert eng.non_trading_days >= 1


# ---------------------------------------------------------------------------
# Task 9: summary
# ---------------------------------------------------------------------------
def test_summarize_aggregates_by_leg():
    trades = [
        {"leg_id": "A", "part": "P1", "entry_kind": "INITIAL", "exit_reason": "SL",
         "pnl_points": -25, "pnl_inr": -500.0},
        {"leg_id": "A", "part": "P1", "entry_kind": "REENTRY", "exit_reason": "TARGET",
         "pnl_points": 90, "pnl_inr": 1800.0},
        {"leg_id": "B", "part": "P2", "entry_kind": "INITIAL", "exit_reason": "SL",
         "pnl_points": -25, "pnl_inr": -500.0},
    ]
    s = summarize(trades, {"P1_CE_UNAVAILABLE_0945": 2}, non_trading_days=10)
    assert s["total_pnl_inr"] == 800.0
    assert s["p1_pnl_inr"] == 1300.0 and s["p2_pnl_inr"] == -500.0
    assert s["n_legs"] == 2 and s["n_round_trips"] == 3 and s["n_reentries"] == 1
    assert s["win_rate"] == 0.5
    assert s["exit_reason_counts"]["SL"] == 2
    assert s["skips"]["P1_CE_UNAVAILABLE_0945"] == 2
    assert s["non_trading_days"] == 10
    # no blind legs here -> all P&L is fully observed
    assert s["blind_pnl_inr"] == 0.0
    assert s["observed_pnl_inr"] == s["total_pnl_inr"]
    assert s["n_blind_legs"] == 0


def test_classify_and_bucket_summary():
    IST = "Asia/Kolkata"
    # day_opts: 09:45..09:47, offsets -2..2 (window half-width = 2), ATM dips at 09:46.
    atm_by_min = {dtime(9, 45): 78000.0, dtime(9, 46): 77800.0, dtime(9, 47): 78000.0}
    rows = [{"time_only": tt, "atm_strike": atm, "strike_offset": off}
            for tt, atm in atm_by_min.items() for off in (-2, -1, 0, 1, 2)]
    day_opts = pd.DataFrame(rows)

    def ts(h, m):
        return pd.Timestamp(f"2025-09-04 {h:02d}:{m:02d}:00", tz=IST)

    trades = [
        # near-ATM strike, always in window, TARGET -> clean
        {"leg_id": "C", "strike": 78000.0, "entry_time": ts(9, 45), "exit_time": ts(9, 47),
         "exit_reason": "TARGET", "pnl_inr": 1000.0},
        # strike 78200: offset 4 at 09:46 (>2) then back, EOD -> recovered
        {"leg_id": "R", "strike": 78200.0, "entry_time": ts(9, 45), "exit_time": ts(9, 47),
         "exit_reason": "EOD", "pnl_inr": 500.0},
        # blind exit -> blind
        {"leg_id": "B", "strike": 78100.0, "entry_time": ts(9, 45), "exit_time": ts(9, 46),
         "exit_reason": "EOD_LAST_AVAILABLE", "pnl_inr": 800.0},
    ]
    classify_day_buckets(day_opts, trades, step=100)
    assert {t["leg_id"]: t["bucket"] for t in trades} == {"C": "clean", "R": "recovered", "B": "blind"}

    bs = bucket_summary(trades)
    assert bs["clean"]["n_legs"] == 1 and bs["recovered"]["n_legs"] == 1 and bs["blind"]["n_legs"] == 1
    assert bs["clean"]["total_pnl_inr"] == 1000.0
    assert bs["clean"]["win_rate"] == 1.0
    assert bs["clean"]["hold_median_min"] == 2.0     # 09:45 -> 09:47


def test_summarize_blind_split():
    trades = [
        {"leg_id": "A", "part": "P1", "entry_kind": "INITIAL", "exit_reason": "TARGET",
         "pnl_points": 90, "pnl_inr": 1800.0},
        {"leg_id": "B", "part": "P2", "entry_kind": "INITIAL",
         "exit_reason": "EOD_LAST_AVAILABLE", "pnl_points": 30, "pnl_inr": 600.0},
    ]
    s = summarize(trades, {}, non_trading_days=0)
    assert s["total_pnl_inr"] == 2400.0
    assert s["blind_pnl_inr"] == 600.0
    assert s["observed_pnl_inr"] == 1800.0
    assert abs(s["blind_pnl_pct"] - 0.25) < 1e-9
    assert s["n_blind_round_trips"] == 1
    assert s["n_blind_legs"] == 1


# ---------------------------------------------------------------------------
# NIFTY support (instrument-aware engine + config-sourced weekly calendar)
# ---------------------------------------------------------------------------
def test_weekly_dte_nifty_from_config():
    # NIFTY weekly dates must come from config (complete), not the 2025+-only JSON.
    assert days_to_weekly_expiry("NIFTY", ddate(2025, 9, 2)) == 0   # Tuesday weekly expiry
    assert days_to_weekly_expiry("NIFTY", ddate(2025, 9, 1)) == 1   # Monday before
    # A 2023 lookup resolves within 2023 (the JSON would jump to 2025-01-02).
    assert get_weekly_expiry("NIFTY", ddate(2023, 6, 1)).year == 2023


def test_engine_runs_nifty_small_window():
    # 2025-05-22 is a NIFTY Thursday weekly expiry; Mon..Thu = DTE 3..0.
    eng = SensexDualShortBacktest("2025-05-19", "2025-05-22", instrument="NIFTY")
    eng.run()
    assert eng.instrument == "NIFTY"
    assert eng.lot == 65                       # NIFTY lot size
    assert len(eng.trades) > 0
    t = eng.trades[0]
    assert abs(t["pnl_inr"] - t["pnl_points"] * 65) < 1e-6
