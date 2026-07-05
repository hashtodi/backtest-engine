"""Tests for Gamma Blast backtest engine."""
import os
import tempfile
from datetime import date, datetime, time

import pandas as pd
import pytest

from engine.gamma_blast_backtest import (
    GammaBlastTrade,
    _default_loader,
    evaluate_entry_trigger,
    evaluate_exit,
    run_backtest,
    run_machine_for_day,
    should_arm,
    summarize_trades,
    trades_to_dataframe,
    write_trades_csv,
)


def make_trade(**overrides) -> GammaBlastTrade:
    defaults = dict(
        date="2026-02-26",
        instrument="SENSEX",
        expiry_date="2026-02-26",
        option_type="CE",
        strike=81000,
        spot_at_arm=80950.0,
        arm_time="11:00",
        arm_premium=18.0,
        spot_at_entry=81150.0,
        entry_time="12:31",
        entry_price=47.0,
        entry_trigger_close=45.0,
        spot_at_exit=81380.0,
        exit_time="13:10",
        exit_price=80.0,
        exit_reason="TP",
        pnl_points=33.0,
        pnl_inr=660.0,
        lot_size=20,
    )
    defaults.update(overrides)
    return GammaBlastTrade(**defaults)


class TestGammaBlastTradeDataclass:
    def test_all_fields_present(self):
        t = make_trade()
        assert t.date == "2026-02-26"
        assert t.instrument == "SENSEX"
        assert t.option_type == "CE"
        assert t.strike == 81000
        assert t.arm_premium == 18.0
        assert t.entry_price == 47.0
        assert t.exit_price == 80.0
        assert t.exit_reason == "TP"
        assert t.pnl_points == 33.0
        assert t.pnl_inr == 660.0

    def test_trades_to_dataframe_empty(self):
        df = trades_to_dataframe([])
        assert df.empty

    def test_trades_to_dataframe_roundtrip(self):
        trades = [make_trade(), make_trade(option_type="PE", exit_reason="SL")]
        df = trades_to_dataframe(trades)
        assert len(df) == 2
        assert set(df.columns) >= {
            "date", "instrument", "expiry_date", "option_type", "strike",
            "spot_at_arm", "arm_time", "arm_premium",
            "spot_at_entry", "entry_time", "entry_price", "entry_trigger_close",
            "spot_at_exit", "exit_time", "exit_price", "exit_reason",
            "pnl_points", "pnl_inr", "lot_size",
        }
        assert df.iloc[1]["option_type"] == "PE"


class TestShouldArm:
    """Arm condition: close < alert_price AND time in [arm_start, arm_deadline]."""

    def test_armed_when_close_below_alert(self):
        assert should_arm(atm_close=19.0, alert_price=20, bar_time=time(11, 0),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is True

    def test_not_armed_at_boundary_equal(self):
        assert should_arm(atm_close=20.0, alert_price=20, bar_time=time(11, 0),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is False

    def test_not_armed_above_alert(self):
        assert should_arm(atm_close=21.0, alert_price=20, bar_time=time(11, 0),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is False

    def test_not_armed_before_arm_start(self):
        assert should_arm(atm_close=10.0, alert_price=20, bar_time=time(9, 59),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is False

    def test_armed_at_arm_start_boundary(self):
        assert should_arm(atm_close=10.0, alert_price=20, bar_time=time(10, 0),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is True

    def test_armed_at_arm_deadline_boundary(self):
        assert should_arm(atm_close=10.0, alert_price=20, bar_time=time(15, 0),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is True

    def test_not_armed_after_arm_deadline(self):
        assert should_arm(atm_close=10.0, alert_price=20, bar_time=time(15, 1),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is False


class TestEvaluateEntryTrigger:
    """Given a trigger bar's data + next bar's open, decide entry action.

    Returns: ("enter", next_open) | ("skip", reason) | ("no_trigger", None)
    """

    def test_enter_when_close_above_entry_price(self):
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=45.0, bar_low=35.0,
            next_open=47.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=True,
        )
        assert result == ("enter", 47.0)

    def test_no_trigger_when_close_equal_entry_price(self):
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=40.0, bar_low=35.0,
            next_open=42.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=True,
        )
        assert result == ("no_trigger", None)

    def test_no_trigger_when_close_below_entry_price(self):
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=39.0, bar_low=35.0,
            next_open=41.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=True,
        )
        assert result == ("no_trigger", None)

    def test_gap_beyond_tp_skip(self):
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=45.0, bar_low=35.0,
            next_open=85.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=True,
        )
        assert result == ("skip", "gap_above_tp")

    def test_gap_below_sl_skip(self):
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=45.0, bar_low=35.0,
            next_open=10.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=True,
        )
        assert result == ("skip", "gap_below_sl")

    def test_same_bar_whip_green_enters(self):
        result = evaluate_entry_trigger(
            bar_open=18.0, bar_close=45.0, bar_low=12.0,
            next_open=47.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=False,
        )
        assert result == ("enter", 47.0)

    def test_same_bar_whip_red_skipped(self):
        result = evaluate_entry_trigger(
            bar_open=50.0, bar_close=45.0, bar_low=12.0,
            next_open=46.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=False,
        )
        assert result == ("no_trigger", None)

    def test_same_bar_whip_doji_skipped(self):
        result = evaluate_entry_trigger(
            bar_open=45.0, bar_close=45.0, bar_low=12.0,
            next_open=45.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=False,
        )
        assert result == ("no_trigger", None)

    def test_unarmed_no_whip_no_trigger(self):
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=45.0, bar_low=30.0,
            next_open=47.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=False,
        )
        assert result == ("no_trigger", None)


class TestEvaluateExit:
    """Given a bar's HLC + is_force_exit_bar, decide exit action.

    Returns: (exit_price, exit_reason) | None
    """

    def test_sl_hit(self):
        result = evaluate_exit(bar_high=55.0, bar_low=14.0, bar_close=30.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result == (15.0, "SL")

    def test_tp_hit(self):
        result = evaluate_exit(bar_high=82.0, bar_low=60.0, bar_close=75.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result == (80.0, "TP")

    def test_sl_wins_tie(self):
        result = evaluate_exit(bar_high=85.0, bar_low=12.0, bar_close=40.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result == (15.0, "SL")

    def test_no_exit_mid_range(self):
        result = evaluate_exit(bar_high=60.0, bar_low=35.0, bar_close=50.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result is None

    def test_boundary_exact_sl(self):
        result = evaluate_exit(bar_high=60.0, bar_low=15.0, bar_close=30.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result == (15.0, "SL")

    def test_boundary_exact_tp(self):
        result = evaluate_exit(bar_high=80.0, bar_low=30.0, bar_close=70.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result == (80.0, "TP")

    def test_force_exit_at_close(self):
        result = evaluate_exit(bar_high=60.0, bar_low=30.0, bar_close=50.0,
                               sl=15, tp=80, is_force_exit_bar=True)
        assert result == (50.0, "EOD")

    def test_sl_wins_over_eod(self):
        result = evaluate_exit(bar_high=60.0, bar_low=10.0, bar_close=50.0,
                               sl=15, tp=80, is_force_exit_bar=True)
        assert result == (15.0, "SL")

    def test_tp_wins_over_eod(self):
        result = evaluate_exit(bar_high=90.0, bar_low=40.0, bar_close=70.0,
                               sl=15, tp=80, is_force_exit_bar=True)
        assert result == (80.0, "TP")


# ---------------------------------------------------------------------------
# run_machine_for_day helpers and tests
# ---------------------------------------------------------------------------

def _make_day_df(rows, day=date(2026, 2, 26)):
    """Build a minute-level DataFrame from tuples (HH, MM, strike, moneyness,
    open, high, low, close, spot)."""
    recs = []
    for hh, mm, strike, mon, o, h, lo, c, sp in rows:
        recs.append({
            "datetime": datetime(day.year, day.month, day.day, hh, mm),
            "strike": strike,
            "moneyness": mon,
            "open": o, "high": h, "low": lo, "close": c,
            "spot": sp,
        })
    return pd.DataFrame(recs)


DEFAULT_PARAMS = dict(alert_price=20, entry_price=40, sl=15, tp=80)
DEFAULT_TIMING = dict(
    arm_start=time(10, 0),
    arm_deadline=time(15, 0),
    entry_deadline=time(15, 5),
    force_exit=time(15, 15),
)


class TestRunMachineForDay:
    def test_winning_tp_trade(self):
        day = date(2026, 2, 26)
        rows = [
            (10, 30, 81000, "ATM", 25, 30, 24, 28, 80950),
            (11, 0,  81000, "ATM", 22, 22, 17, 18, 80910),  # arm
            (12, 30, 81000, "ATM", 40, 46, 38, 45, 81200),  # trigger
            (12, 31, 81000, "ATM", 47, 58, 46, 55, 81215),  # enter @ 47, no exit
            (13, 10, 81000, "ATM", 70, 82, 68, 75, 81390),  # TP hit at 80
            (15, 15, 81000, "ATM", 90, 90, 88, 89, 81400),
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "TP"
        assert t.entry_price == 47
        assert t.exit_price == 80
        assert t.pnl_points == 33
        assert t.pnl_inr == 33 * 20 * 1
        assert t.strike == 81000
        assert t.option_type == "CE"

    def test_losing_sl_trade(self):
        day = date(2026, 2, 26)
        rows = [
            (10, 45, 81000, "ATM", 22, 22, 18, 19, 81000),
            (12, 0,  81000, "ATM", 40, 46, 39, 43, 80950),
            (12, 1,  81000, "ATM", 42, 45, 14, 20, 80900),  # enter @ 42, SL
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "SL"
        assert trades[0].exit_price == 15
        assert trades[0].pnl_points == -27

    def test_force_exit_eod(self):
        day = date(2026, 2, 26)
        rows = [
            (10, 30, 81000, "ATM", 22, 22, 18, 19, 81000),
            (11, 0,  81000, "ATM", 40, 46, 38, 45, 81050),
            (11, 1,  81000, "ATM", 46, 60, 45, 50, 81060),
            (12, 0,  81000, "ATM", 50, 55, 45, 52, 81060),
            (15, 15, 81000, "ATM", 35, 40, 30, 38, 81050),  # force exit @ close=38
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "EOD"
        assert trades[0].exit_price == 38

    def test_armed_but_deadline_expires(self):
        day = date(2026, 2, 26)
        rows = [
            (14, 30, 81000, "ATM", 22, 22, 18, 19, 81000),
            (14, 59, 81000, "ATM", 30, 35, 28, 32, 81020),
            (15, 5,  81000, "ATM", 35, 38, 33, 36, 81010),
            (15, 15, 81000, "ATM", 40, 42, 38, 41, 81005),
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert trades == []

    def test_reentry_after_sl(self):
        day = date(2026, 2, 26)
        rows = [
            (10, 30, 81000, "ATM", 22, 22, 18, 19, 81000),
            (11, 0,  81000, "ATM", 40, 46, 38, 45, 81050),
            (11, 1,  81000, "ATM", 46, 50, 14, 20, 81060),  # SL
            (11, 30, 81000, "ATM", 19, 19, 15, 17, 81040),  # re-arm
            (12, 0,  81000, "ATM", 38, 45, 35, 43, 81050),  # re-trigger
            (12, 1,  81000, "ATM", 44, 85, 42, 80, 81080),  # enter @ 44, TP
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 2
        assert trades[0].exit_reason == "SL"
        assert trades[1].exit_reason == "TP"

    def test_strike_locked_after_arm(self):
        day = date(2026, 2, 26)
        rows = [
            (11, 0,  81000, "ATM", 22, 22, 17, 18, 81000),
            (11, 0,  81100, "OTM", 15, 18, 13, 14, 81000),
            (11, 30, 81000, "ITM", 35, 40, 32, 38, 81120),
            (11, 30, 81100, "ATM", 20, 25, 18, 22, 81120),
            (12, 0,  81000, "ITM", 40, 46, 38, 45, 81150),
            (12, 0,  81100, "ATM", 24, 28, 22, 26, 81150),
            (12, 1,  81000, "ITM", 47, 85, 45, 82, 81180),
            (12, 1,  81100, "ATM", 27, 32, 25, 30, 81180),
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        assert trades[0].strike == 81000
        assert trades[0].entry_price == 47

    def test_arm_ignored_before_arm_start(self):
        day = date(2026, 2, 26)
        rows = [
            (9, 30, 81000, "ATM", 22, 22, 18, 19, 81000),
            (11, 0, 81000, "ATM", 25, 30, 24, 28, 81020),
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert trades == []

    def test_same_bar_green_whip_enters(self):
        day = date(2026, 2, 26)
        rows = [
            (11, 0,  81000, "ATM", 30, 48, 12, 45, 81000),  # green whip
            (11, 1,  81000, "ATM", 50, 58, 48, 55, 81020),  # entry @ 50
            (12, 0,  81000, "ATM", 60, 85, 55, 78, 81080),  # TP
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        assert trades[0].entry_price == 50
        assert trades[0].exit_price == 80
        assert trades[0].exit_reason == "TP"
        assert trades[0].arm_time == "11:00"
        assert trades[0].entry_time == "11:01"

    def test_same_bar_red_whip_skipped(self):
        day = date(2026, 2, 26)
        rows = [
            (11, 0,  81000, "ATM", 50, 55, 12, 45, 81000),  # red whip skipped
            (11, 10, 81000, "ATM", 20, 22, 17, 18, 81000),  # plain arm
            (12, 0,  81000, "ATM", 40, 46, 38, 45, 81060),  # trigger
            (12, 1,  81000, "ATM", 47, 82, 45, 78, 81080),  # TP
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        assert trades[0].arm_time == "11:10"
        assert trades[0].exit_reason == "TP"


def _stub_loader(df_by_instrument_and_day):
    def _load(instrument, day):
        return df_by_instrument_and_day.get((instrument, day), pd.DataFrame())
    return _load


class TestRunBacktest:
    def test_only_expiry_days_processed(self):
        feb26 = date(2026, 2, 26)
        feb27 = date(2026, 2, 27)
        rows = [
            (10, 30, 81000, "ATM", 22, 22, 18, 19, 81000),
            (11, 0,  81000, "ATM", 40, 46, 38, 45, 81050),
            (11, 1,  81000, "ATM", 47, 82, 45, 78, 81060),
        ]
        df_feb26_ce = _make_day_df(rows, day=feb26).assign(option_type="CE", expiry_code=1)
        df_feb27_ce = _make_day_df(rows, day=feb27).assign(option_type="CE", expiry_code=1)

        loader = _stub_loader({
            ("SENSEX", feb26): df_feb26_ce,
            ("SENSEX", feb27): df_feb27_ce,
        })
        config = {
            "instruments": ["SENSEX"],
            "params": {"SENSEX": DEFAULT_PARAMS},
            "timing": {k: v.strftime("%H:%M") for k, v in DEFAULT_TIMING.items()},
            "lot_size": 1,
            "backtest_start": "2026-02-26",
            "backtest_end":   "2026-02-27",
        }
        trades = run_backtest(config, loader=loader)
        assert len(trades) == 1
        assert trades[0].date == "2026-02-26"

    def test_instruments_filter_respected(self):
        feb26 = date(2026, 2, 26)
        rows = [
            (10, 30, 81000, "ATM", 22, 22, 18, 19, 81000),
            (11, 0,  81000, "ATM", 40, 46, 38, 45, 81050),
            (11, 1,  81000, "ATM", 47, 82, 45, 78, 81060),
        ]
        df_ce = _make_day_df(rows, day=feb26).assign(option_type="CE", expiry_code=1)
        loader = _stub_loader({("SENSEX", feb26): df_ce})
        config = {
            "instruments": ["NIFTY", "SENSEX"],
            "params": {
                "NIFTY": {"alert_price": None, "entry_price": None, "sl": None, "tp": None},
                "SENSEX": DEFAULT_PARAMS,
            },
            "timing": {k: v.strftime("%H:%M") for k, v in DEFAULT_TIMING.items()},
            "lot_size": 1,
            "backtest_start": "2026-02-26",
            "backtest_end":   "2026-02-26",
        }
        trades = run_backtest(config, loader=loader)
        assert all(t.instrument == "SENSEX" for t in trades)

    def test_ce_and_pe_both_run(self):
        feb26 = date(2026, 2, 26)
        ce_rows = [
            (10, 30, 81000, "ATM", 22, 22, 18, 19, 81000),
            (11, 0,  81000, "ATM", 40, 46, 38, 45, 81050),
            (11, 1,  81000, "ATM", 47, 82, 45, 78, 81060),
        ]
        pe_rows = [
            (10, 45, 81000, "ATM", 22, 22, 18, 19, 81000),
            (11, 30, 81000, "ATM", 40, 46, 38, 45, 80950),
            (11, 31, 81000, "ATM", 46, 50, 14, 20, 80930),  # SL
        ]
        df_ce = _make_day_df(ce_rows, day=feb26).assign(option_type="CE", expiry_code=1)
        df_pe = _make_day_df(pe_rows, day=feb26).assign(option_type="PE", expiry_code=1)
        df_day = pd.concat([df_ce, df_pe], ignore_index=True)
        loader = _stub_loader({("SENSEX", feb26): df_day})
        config = {
            "instruments": ["SENSEX"],
            "params": {"SENSEX": DEFAULT_PARAMS},
            "timing": {k: v.strftime("%H:%M") for k, v in DEFAULT_TIMING.items()},
            "lot_size": 1,
            "backtest_start": "2026-02-26",
            "backtest_end":   "2026-02-26",
        }
        trades = run_backtest(config, loader=loader)
        assert len(trades) == 2
        types = sorted([t.option_type for t in trades])
        assert types == ["CE", "PE"]
        reasons = sorted([t.exit_reason for t in trades])
        assert reasons == ["SL", "TP"]


class TestSummarize:
    def test_empty(self):
        s = summarize_trades([])
        assert s["total_trades"] == 0
        assert s["total_pnl_inr"] == 0

    def test_mixed_trades(self):
        trades = [
            make_trade(exit_reason="TP", pnl_points=33, pnl_inr=660),
            make_trade(exit_reason="SL", pnl_points=-27, pnl_inr=-540),
            make_trade(exit_reason="TP", pnl_points=40, pnl_inr=800, option_type="PE"),
        ]
        s = summarize_trades(trades)
        assert s["total_trades"] == 3
        assert s["wins"] == 2
        assert s["losses"] == 1
        assert round(s["win_rate"] * 100, 2) == 66.67
        assert s["total_pnl_points"] == 46
        assert s["total_pnl_inr"] == 920

    def test_per_instrument_breakdown(self):
        trades = [
            make_trade(instrument="SENSEX", pnl_inr=100),
            make_trade(instrument="SENSEX", pnl_inr=-200),
            make_trade(instrument="NIFTY", pnl_inr=500),
        ]
        s = summarize_trades(trades)
        assert s["by_instrument"]["SENSEX"]["trades"] == 2
        assert s["by_instrument"]["SENSEX"]["pnl_inr"] == -100
        assert s["by_instrument"]["NIFTY"]["trades"] == 1
        assert s["by_instrument"]["NIFTY"]["pnl_inr"] == 500


class TestWriteCsv:
    def test_write_roundtrip(self):
        trades = [make_trade(), make_trade(option_type="PE")]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trades.csv")
            write_trades_csv(trades, path)
            df = pd.read_csv(path)
            assert len(df) == 2
            assert list(df["option_type"]) == ["CE", "PE"]

    def test_write_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trades.csv")
            write_trades_csv([], path)
            df = pd.read_csv(path)
            assert df.empty


@pytest.mark.integration
class TestIntegrationRealData:
    """Smoke test against real SENSEX parquet. Skipped if data missing."""

    def test_one_sensex_expiry_day_runs(self):
        day = date(2026, 2, 26)
        try:
            df = _default_loader("SENSEX", day)
        except Exception as e:
            pytest.skip(f"SENSEX data not available: {e}")
        if df.empty:
            pytest.skip("No data for 2026-02-26")

        config = {
            "instruments": ["SENSEX"],
            "params": {"SENSEX": {"alert_price": 20, "entry_price": 40, "sl": 15, "tp": 80}},
            "timing": {"arm_start": "10:00", "arm_deadline": "15:00",
                       "entry_deadline": "15:05", "force_exit": "15:15"},
            "lot_size": 1,
            "backtest_start": "2026-02-26",
            "backtest_end":   "2026-02-26",
        }
        trades = run_backtest(config)
        assert isinstance(trades, list)
        for t in trades:
            assert t.instrument == "SENSEX"
            assert t.option_type in ("CE", "PE")
            assert t.date == "2026-02-26"
            assert t.exit_reason in ("SL", "TP", "EOD")
            assert t.entry_price > 0
            assert t.exit_price > 0
