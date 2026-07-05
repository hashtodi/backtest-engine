"""Tests for the IV/HV iron-condor engine."""
from datetime import date, timedelta
from scripts.build_weekly_expiry_calendar import (
    derive_weekly_expiries, apply_known_fixes)


def _weekdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_thursday_regime_picks_thursdays():
    days = _weekdays(date(2021, 6, 1), date(2021, 6, 30))
    exp = derive_weekly_expiries(days)
    # June 2021 Thursdays
    assert date(2021, 6, 3) in exp
    assert date(2021, 6, 10) in exp
    assert date(2021, 6, 24) in exp
    assert all(e.weekday() == 3 for e in exp)  # all Thursdays


def test_tuesday_regime_after_switch():
    days = _weekdays(date(2026, 5, 1), date(2026, 5, 31))
    exp = derive_weekly_expiries(days)
    assert date(2026, 5, 12) in exp
    assert date(2026, 5, 19) in exp
    assert all(e.weekday() == 1 for e in exp)  # all Tuesdays


def test_holiday_rolls_back_to_previous_trading_day():
    days = _weekdays(date(2021, 6, 1), date(2021, 6, 30))
    days.remove(date(2021, 6, 10))  # simulate Thu holiday
    exp = derive_weekly_expiries(days)
    assert date(2021, 6, 9) in exp   # rolled to Wednesday
    assert date(2021, 6, 10) not in exp


def test_known_diwali_fixes_applied():
    # Diwali special-session shifts the weekday rule misses
    fixed = apply_known_fixes([date(2021, 11, 4), date(2025, 10, 21), date(2022, 1, 6)])
    assert date(2021, 11, 3) in fixed and date(2021, 11, 4) not in fixed
    assert date(2025, 10, 20) in fixed and date(2025, 10, 21) not in fixed
    assert date(2022, 1, 6) in fixed  # untouched dates preserved


def test_config_covers_backtest_period():
    import config
    wk = config.NIFTY_WEEKLY_EXPIRY_DATES
    assert min(wk) <= date(2020, 8, 10)
    assert len(wk) == len(set(wk))  # no duplicates
    assert config.get_nearest_weekly_expiry(date(2021, 6, 1)) == date(2021, 6, 3)
    assert config.get_nearest_weekly_expiry(date(2021, 6, 3)) == date(2021, 6, 3)  # same-day
    assert config.get_nearest_weekly_expiry(date(2026, 5, 11)) == date(2026, 5, 12)
    assert config.get_nearest_weekly_expiry(date(2021, 11, 1)) == date(2021, 11, 3)  # Diwali fix


# ---------------------------------------------------------------------------
# Task 4: engine skeleton (DayContext, parse_config)
# ---------------------------------------------------------------------------
from engine.iv_hv_iron_condor_backtest import parse_config, DayContext


def test_parse_config_defaults():
    ctx = parse_config({})
    assert ctx.iv_rv_ratio_min == 1.3
    assert ctx.tp_pct == 0.50 and ctx.sl_pct == 2.00
    assert ctx.sell_ce_delta == 0.20 and ctx.buy_pe_delta == -0.08
    assert ctx.lot_size == 65 and ctx.lots == 4


def test_parse_config_overrides():
    cfg = {"signal": {"iv_rv_ratio_min": 1.5},
           "exit": {"tp_pct": 0.6, "sl_pct": 1.0, "hard_exit_time": "15:15"},
           "structure": {"sell_ce_delta": 0.25}}
    ctx = parse_config(cfg)
    assert ctx.iv_rv_ratio_min == 1.5
    assert ctx.tp_pct == 0.6 and ctx.sl_pct == 1.0
    assert ctx.hard_exit_time == "15:15"
    assert ctx.sell_ce_delta == 0.25


# ---------------------------------------------------------------------------
# Task 5: Stage-1 signal finder
# ---------------------------------------------------------------------------
import pandas as pd
from engine.iv_hv_iron_condor_backtest import find_signals


def _atm_frame(times_ivs):
    # build ATM (offset 0) CE+PE rows for one day
    rows = []
    for t, ce_iv, pe_iv in times_ivs:
        dt = pd.Timestamp(f"2021-06-01 {t}")
        for ot, iv in [("CE", ce_iv), ("PE", pe_iv)]:
            rows.append({"_dt": dt, "_date": date(2021, 6, 1), "_time": t,
                         "strike_offset": 0, "option_type": ot, "iv": iv})
    return pd.DataFrame(rows)


def test_first_hit_per_day_and_ratio():
    df = _atm_frame([("09:45", 10, 10), ("09:46", 20, 20), ("09:47", 22, 22)])
    hv = {date(2021, 6, 1): 12.0}  # ratio at 09:46 = 20/12=1.67 (>1.3); 09:45=10/12=0.83
    sigs = find_signals(df, hv, DayContext())
    assert len(sigs) == 1
    assert sigs.iloc[0]["_time"] == "09:46"          # first minute crossing 1.3
    assert abs(sigs.iloc[0]["ratio"] - 20 / 12) < 1e-6
    assert sigs.iloc[0]["direction"] == "bearish"


def test_window_excludes_after_1130():
    df = _atm_frame([("11:31", 30, 30)])
    sigs = find_signals(df, {date(2021, 6, 1): 12.0}, DayContext())
    assert len(sigs) == 0


def test_missing_hv_no_signal():
    df = _atm_frame([("09:45", 30, 30)])
    sigs = find_signals(df, {}, DayContext())  # no HV for the date
    assert len(sigs) == 0


# ---------------------------------------------------------------------------
# Task 6: delta computation + leg selection (reproduces the spec §11 fixture)
# ---------------------------------------------------------------------------
from datetime import datetime
from engine.iv_hv_iron_condor_backtest import (
    load_options, minutes_to_expiry, add_delta, select_legs, net_credit_pts)

OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"


def test_fixture_leg_selection_2026_05_11():
    df = load_options(OPTIONS_PATH, "2026-05-11", "2026-05-11")
    bar = df[df["_time"] == "09:45"]
    spot = float(bar["spot"].iloc[0])
    T = minutes_to_expiry(datetime(2026, 5, 11, 9, 45), date(2026, 5, 12)) / 525600.0
    bar = add_delta(bar, spot, T, DayContext())
    legs = select_legs(bar, spot, T, DayContext())
    assert legs["sell_ce"].strike == 24150
    assert legs["buy_ce"].strike == 24300
    assert legs["sell_pe"].strike == 23650
    assert legs["buy_pe"].strike == 23450
    assert abs(net_credit_pts(legs) - 48.10) < 0.05


# ---------------------------------------------------------------------------
# Task 7: Stage-2 trade simulation
# ---------------------------------------------------------------------------
from engine.iv_hv_iron_condor_backtest import simulate_trade, LegFill


def _mk_leg(ot, strike, off, entry):
    return LegFill(ot, strike, off, 0.2, entry)


def _day_df_from_paths(paths):
    # paths: {(option_type, strike): {time: close}}
    rows = []
    for (ot, strike), series in paths.items():
        for t, close in series.items():
            rows.append({"_time": t, "option_type": ot, "strike": strike, "close": close})
    return pd.DataFrame(rows)


def test_tp_hit():
    legs = {"sell_ce": _mk_leg("CE", 100, 2, 20), "buy_ce": _mk_leg("CE", 200, 4, 5),
            "sell_pe": _mk_leg("PE", 100, -2, 20), "buy_pe": _mk_leg("PE", 50, -4, 5)}
    # credit = |(20+20)-(5+5)| = 30 ; tp = +15
    # at 09:47: pnl = (20-5)+(20-5)+(3-5)+(3-5) = 30-4 = 26 >= 15 -> TP
    paths = {("CE", 100): {"09:46": 20, "09:47": 5}, ("CE", 200): {"09:46": 5, "09:47": 3},
             ("PE", 100): {"09:46": 20, "09:47": 5}, ("PE", 50): {"09:46": 5, "09:47": 3}}
    df = _day_df_from_paths(paths)
    ctx = DayContext()
    r = simulate_trade(df, legs, 30.0, datetime(2021, 6, 1, 9, 45), ctx)
    assert r["exit_reason"] == "TP"
    assert r["exit_time"] == "09:47"
    assert r["pnl_inr"] == r["pnl_pts"] * 65 * 4


def test_time_exit_when_flat():
    legs = {"sell_ce": _mk_leg("CE", 100, 2, 20), "buy_ce": _mk_leg("CE", 200, 4, 5),
            "sell_pe": _mk_leg("PE", 100, -2, 20), "buy_pe": _mk_leg("PE", 50, -4, 5)}
    # prices unchanged all day -> pnl ~0 -> TIME exit at 15:10
    times = ["09:46", "12:00", "15:10"]
    paths = {("CE", 100): {t: 20 for t in times}, ("CE", 200): {t: 5 for t in times},
             ("PE", 100): {t: 20 for t in times}, ("PE", 50): {t: 5 for t in times}}
    df = _day_df_from_paths(paths)
    r = simulate_trade(df, legs, 30.0, datetime(2021, 6, 1, 9, 45), DayContext())
    assert r["exit_reason"] == "TIME"
    assert r["exit_time"] == "15:10"
    assert abs(r["pnl_pts"]) < 1e-6


def test_sl_hit():
    legs = {"sell_ce": _mk_leg("CE", 100, 2, 20), "buy_ce": _mk_leg("CE", 200, 4, 5),
            "sell_pe": _mk_leg("PE", 100, -2, 20), "buy_pe": _mk_leg("PE", 50, -4, 5)}
    # credit 30, sl = -60. Big adverse move: short CE explodes to 120.
    # pnl = (20-120)+(80-5)+(20-20)+(5-5) = -100+75 = -25 -> not yet; push further
    paths = {("CE", 100): {"09:46": 120}, ("CE", 200): {"09:46": 5},
             ("PE", 100): {"09:46": 20}, ("PE", 50): {"09:46": 5}}
    # pnl = (20-120)+(5-5)+(20-20)+(5-5) = -100 <= -60 -> SL
    df = _day_df_from_paths(paths)
    r = simulate_trade(df, legs, 30.0, datetime(2021, 6, 1, 9, 45), DayContext())
    assert r["exit_reason"] == "SL"
    assert r["exit_time"] == "09:46"


# ---------------------------------------------------------------------------
# Task 8: sanity flag + reporter
# ---------------------------------------------------------------------------
from engine.iv_hv_iron_condor_backtest import sanity_flag, summarize_metrics, is_formable


def test_is_formable_rejects_collapsed_wing():
    ok = {"sell_ce": _mk_leg("CE", 24150, 6, 37.95), "buy_ce": _mk_leg("CE", 24300, 9, 17.80),
          "sell_pe": _mk_leg("PE", 23650, -4, 41.35), "buy_pe": _mk_leg("PE", 23450, -8, 13.40)}
    assert is_formable(ok) is True
    # CE wing collapsed onto short (same strike) -> not formable
    bad = dict(ok, buy_ce=_mk_leg("CE", 24150, 6, 37.95))
    assert is_formable(bad) is False
    # PE wing collapsed
    bad2 = dict(ok, buy_pe=_mk_leg("PE", 23650, -4, 41.35))
    assert is_formable(bad2) is False


def test_sanity_flag_bounds_by_width():
    legs = {"sell_ce": _mk_leg("CE", 24150, 6, 37.95), "buy_ce": _mk_leg("CE", 24300, 9, 17.80),
            "sell_pe": _mk_leg("PE", 23650, -4, 41.35), "buy_pe": _mk_leg("PE", 23450, -8, 13.40)}
    # CE width 150, PE width 200 -> max 200
    assert sanity_flag(legs, 500.0) is True     # impossible -> flagged
    assert sanity_flag(legs, 24.0) is False      # normal TP-sized move


def test_summarize_metrics_basic():
    trades = [{"pnl_inr": 100.0, "exit_reason": "TP"},
              {"pnl_inr": -50.0, "exit_reason": "SL"},
              {"pnl_inr": 0.0, "exit_reason": "TIME"}]
    s = summarize_metrics(trades)
    assert s["total_trades"] == 3
    assert s["wins"] == 1 and s["losses"] == 1
    assert abs(s["total_pnl_inr"] - 50.0) < 1e-6
    assert s["exit_reason_counts"] == {"TP": 1, "SL": 1, "TIME": 1}


# ---------------------------------------------------------------------------
# run() orchestrator + skip stats (integration, uses real data for one month)
# ---------------------------------------------------------------------------
import json
from engine.iv_hv_iron_condor_backtest import run


def test_run_orchestrator_may_2026():
    with open("saved_strategies/iv_hv_iron_condor.json") as f:
        cfg = dict(json.load(f))
    cfg["backtest_start"] = "2026-05-01"
    cfg["backtest_end"] = "2026-05-22"
    result = run(cfg)
    # May 2026: 4 formable trades executed; 2 days (05-13, 05-20) are un-formable.
    # Of the 4, 2026-05-18 has a leg leave the +/-10 window (fill_fallback) -> excluded
    # from the headline, leaving 3 reliable trades.
    s = result["summary"]
    assert s["all_trades"] == 4
    assert s["total_trades"] == 3               # reliable (headline)
    assert s["excluded_fallback_trades"] == 1   # 2026-05-18
    assert result["stats"]["skipped_unformable"] == 2
    assert result["stats"]["signals"] >= s["all_trades"]
    assert len(result["trades_df"]) == 4        # CSV keeps all executed trades
    # no degenerate (collapsed-wing) trades survive
    df = result["trades_df"]
    assert not ((df.sell_ce_strike == df.buy_ce_strike) |
                (df.sell_pe_strike == df.buy_pe_strike)).any()
    assert bool(df[df.date == "2026-05-18"]["fill_fallback"].iloc[0]) is True
