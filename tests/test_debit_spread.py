"""Tests for debit_spread backtest engine."""
from dataclasses import asdict
from datetime import date

import pandas as pd
import pytest

from engine.debit_spread_backtest import (
    LegSpec,
    DebitSpreadTrade,
    trades_to_dataframe,
    compute_entry_date,
    resolve_atm_strike,
    fetch_legs_at,
    LEG_KEY_ORDER,
    DEFAULT_LEG_SPECS,
    LegFill,
    compute_entry_economics,
    LOT_SIZE_NIFTY,
    compute_mtm_inr,
    scan_for_tp_exit,
    build_bar_stream,
    run_one_week,
    WeekContext,
    run_backtest,
    parse_config,
    build_equity_curve,
    compute_sharpe,
    compute_sortino,
    max_consecutive_losses,
    summarize_metrics,
    write_trades_csv,
    write_equity_csv,
    print_summary,
    run,
)
from datetime import timedelta
import math


def _row(ts, opt_type, strike_offset, close):
    return {
        "datetime": ts, "underlying": "NIFTY",
        "option_type": opt_type, "expiry_code": 1, "expiry_type": "WEEK",
        "strike": 24500 + strike_offset * 50, "atm_strike": 24500.0,
        "strike_offset": strike_offset, "moneyness": "OTM",
        "spot": 24500.0, "open": close, "high": close,
        "low": close, "close": close, "volume": 1, "oi": 1, "iv": 15.0,
    }


def _legs_with_prices(prices: dict):
    """Build a {leg_key: LegFill} dict with the supplied entry prices."""
    legs = {}
    for k, spec in DEFAULT_LEG_SPECS.items():
        legs[k] = LegFill(
            option_type=spec.option_type, side=spec.side, lots=spec.lots,
            strike_offset=spec.strike_offset,
            strike=24500 + spec.strike_offset * 50,
            entry_price=prices[k],
        )
    return legs


def _make_leg_row(strike, offset, opt_type, open_=10.0, close=11.0,
                  high=12.0, low=9.0, expiry_code=1, moneyness="OTM",
                  expiry_type="WEEK",
                  ts="2025-06-13T11:00:00+05:30"):
    return {
        "datetime": ts, "underlying": "NIFTY",
        "option_type": opt_type, "expiry_code": expiry_code,
        "expiry_type": expiry_type,
        "strike": strike, "atm_strike": 24500.0,
        "strike_offset": offset, "moneyness": moneyness,
        "spot": 24512.0, "open": open_, "high": high, "low": low,
        "close": close, "volume": 1000, "oi": 5000, "iv": 15.0,
    }


class TestDataclasses:
    def test_legspec_holds_offset_and_lots(self):
        spec = LegSpec(option_type="CE", side="BUY", lots=1, strike_offset=-1)
        assert spec.option_type == "CE"
        assert spec.side == "BUY"
        assert spec.lots == 1
        assert spec.strike_offset == -1

    def test_trade_to_dataframe_empty(self):
        df = trades_to_dataframe([])
        assert df.empty

    def test_trade_to_dataframe_one_row(self):
        trade = DebitSpreadTrade(
            expiry_date="2025-06-17",
            entry_date="2025-06-13",
            entry_time="11:00",
            atm_strike=24500.0,
            spot_at_entry=24512.5,
            net_debit_pts=80.0,
            net_debit_inr=5200.0,
            tp_target_inr=7800.0,
            exit_time="15:25",
            exit_reason="EXPIRY",
            pnl_pts=15.0,
            pnl_inr=975.0,
            return_pct=0.00325,
            running_equity_inr=300975.0,
            skip_reason=None,
            legs={},
        )
        df = trades_to_dataframe([trade])
        assert len(df) == 1
        assert df.iloc[0]["exit_reason"] == "EXPIRY"


class TestComputeEntryDate:
    """T−2 trading days off the (possibly shifted) expiry date."""

    def test_regular_tuesday_expiry(self):
        # Tue 2025-06-17 → entry Fri 2025-06-13
        trading_days = sorted({
            date(2025, 6, 9), date(2025, 6, 10), date(2025, 6, 11),
            date(2025, 6, 12), date(2025, 6, 13),
            date(2025, 6, 16), date(2025, 6, 17),
        })
        assert compute_entry_date(date(2025, 6, 17), trading_days, 2) == date(2025, 6, 13)

    def test_holiday_in_between(self):
        # Expiry Tue 2025-08-19, holiday Mon 2025-08-18 → entry skips Mon → Thu 2025-08-14
        trading_days = sorted({
            date(2025, 8, 13), date(2025, 8, 14), date(2025, 8, 15),
            date(2025, 8, 19),  # 2025-08-18 missing (holiday)
        })
        assert compute_entry_date(date(2025, 8, 19), trading_days, 2) == date(2025, 8, 14)

    def test_shifted_monday_expiry(self):
        # Expiry shifted to Mon 2026-10-19 (Tuesday Dussehra) → entry Thu 2026-10-15
        trading_days = sorted({
            date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15),
            date(2026, 10, 16), date(2026, 10, 19),
        })
        assert compute_entry_date(date(2026, 10, 19), trading_days, 2) == date(2026, 10, 15)

    def test_expiry_not_in_trading_days_raises(self):
        trading_days = [date(2025, 6, 13), date(2025, 6, 16)]
        with pytest.raises(ValueError):
            compute_entry_date(date(2025, 6, 17), trading_days, 2)


class TestResolveAtmStrike:
    def test_picks_moneyness_atm_row(self):
        df = pd.DataFrame([
            {"strike": 24500.0, "moneyness": "ATM", "spot": 24512.0,
             "option_type": "CE", "expiry_code": 1},
            {"strike": 24450.0, "moneyness": "ITM", "spot": 24512.0,
             "option_type": "CE", "expiry_code": 1},
        ])
        atm, spot = resolve_atm_strike(df)
        assert atm == 24500.0
        assert spot == 24512.0

    def test_multiple_atm_picks_closest_to_spot(self):
        df = pd.DataFrame([
            {"strike": 24450.0, "moneyness": "ATM", "spot": 24512.0,
             "option_type": "CE", "expiry_code": 1},
            {"strike": 24500.0, "moneyness": "ATM", "spot": 24512.0,
             "option_type": "CE", "expiry_code": 1},
        ])
        atm, _ = resolve_atm_strike(df)
        assert atm == 24500.0  # |24500-24512|=12 vs |24450-24512|=62

    def test_no_atm_returns_none(self):
        df = pd.DataFrame([
            {"strike": 24450.0, "moneyness": "ITM", "spot": 24512.0,
             "option_type": "CE", "expiry_code": 1},
        ])
        atm, spot = resolve_atm_strike(df)
        assert atm is None
        assert spot is None


class TestFetchLegsAt:
    def test_all_six_legs_present(self):
        rows = []
        for off, mny, op in [(-1, "ITM", 120.0), (4, "OTM", 35.0), (5, "OTM", 22.0)]:
            rows.append(_make_leg_row(24500 + off*50, off, "CE",
                                      open_=op, moneyness=mny))
        for off, mny, op in [(1, "ITM", 115.0), (-4, "OTM", 32.0), (-5, "OTM", 20.0)]:
            rows.append(_make_leg_row(24500 + off*50, off, "PE",
                                      open_=op, moneyness=mny))
        df = pd.DataFrame(rows)

        legs, missing = fetch_legs_at(df, DEFAULT_LEG_SPECS)
        assert missing == []
        assert set(legs.keys()) == set(LEG_KEY_ORDER)
        assert legs["ce_itm"].entry_price == 120.0
        assert legs["ce_short"].entry_price == 35.0
        assert legs["ce_far"].entry_price == 22.0
        assert legs["pe_itm"].entry_price == 115.0
        assert legs["pe_short"].entry_price == 32.0
        assert legs["pe_far"].entry_price == 20.0

    def test_missing_one_leg_reported(self):
        rows = []
        for off, mny, op in [(-1, "ITM", 120.0), (4, "OTM", 35.0), (5, "OTM", 22.0)]:
            rows.append(_make_leg_row(24500 + off*50, off, "CE", open_=op, moneyness=mny))
        for off, mny, op in [(1, "ITM", 115.0), (-4, "OTM", 32.0)]:
            rows.append(_make_leg_row(24500 + off*50, off, "PE", open_=op, moneyness=mny))
        df = pd.DataFrame(rows)

        legs, missing = fetch_legs_at(df, DEFAULT_LEG_SPECS)
        assert missing == ["pe_far"]

    def test_empty_slice_all_missing(self):
        df = pd.DataFrame()
        legs, missing = fetch_legs_at(df, DEFAULT_LEG_SPECS)
        assert set(missing) == set(LEG_KEY_ORDER)


class TestComputeEntryEconomics:
    def test_typical_debit_case(self):
        prices = {
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        }
        # entry_cost = -(1*120) + (3*35) + -(2*22) + -(1*115) + (3*32) + -(2*20)
        #            = -120 + 105 - 44 - 115 + 96 - 40 = -118
        # net_debit_pts = 118 ; net_debit_inr = 118 * 65 = 7670
        legs = _legs_with_prices(prices)
        net_pts, net_inr, tp = compute_entry_economics(legs, tp_multiple=1.5)
        assert net_pts == pytest.approx(118.0)
        assert net_inr == pytest.approx(118.0 * LOT_SIZE_NIFTY)
        assert tp == pytest.approx(118.0 * LOT_SIZE_NIFTY * 1.5)

    def test_credit_case_clamps_tp_to_zero(self):
        prices = {
            "ce_itm": 10.0, "ce_short": 50.0, "ce_far": 5.0,
            "pe_itm": 10.0, "pe_short": 50.0, "pe_far": 5.0,
        }
        legs = _legs_with_prices(prices)
        net_pts, net_inr, tp = compute_entry_economics(legs, tp_multiple=1.5)
        assert net_pts < 0
        assert net_inr < 0
        assert tp == 0.0


class TestComputeMtmInr:
    def test_mtm_zero_at_entry_prices(self):
        legs = _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, 1.5)
        prices_now = {k: l.entry_price for k, l in legs.items()}
        mtm = compute_mtm_inr(legs, prices_now, net_debit_pts=net_pts)
        assert mtm == pytest.approx(0.0)

    def test_mtm_rises_when_long_legs_appreciate(self):
        legs = _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, 1.5)
        prices_now = {
            "ce_itm": 200.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        }
        mtm = compute_mtm_inr(legs, prices_now, net_debit_pts=net_pts)
        # +1 lot * (+80 pts) = +80 pts ; * 65 = 5200
        assert mtm == pytest.approx(80.0 * LOT_SIZE_NIFTY)


class TestScanForTpExit:
    def _bars(self, n_bars, fn_prices):
        bars = []
        for i in range(n_bars):
            ts = pd.Timestamp("2025-06-13T11:01:00+05:30") + pd.Timedelta(minutes=i)
            bars.append({"datetime": ts, "prices": fn_prices(i)})
        return bars

    def test_tp_fires_when_mtm_crosses_target(self):
        legs = _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })
        net_pts, _, tp = compute_entry_economics(legs, 1.5)

        def prices_at(i):
            base = {k: l.entry_price for k, l in legs.items()}
            if i >= 1:
                base["ce_itm"] += 500.0
            return base

        bars = self._bars(n_bars=3, fn_prices=prices_at)
        result = scan_for_tp_exit(legs, bars, net_debit_pts=net_pts, tp_target_inr=tp)
        assert result is not None
        exit_ts, exit_prices, mtm_at_exit = result
        assert exit_ts == bars[1]["datetime"]
        assert mtm_at_exit >= tp

    def test_tp_never_fires_returns_none(self):
        legs = _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })
        net_pts, _, tp = compute_entry_economics(legs, 1.5)

        def flat_prices(i):
            return {k: l.entry_price for k, l in legs.items()}
        bars = self._bars(n_bars=10, fn_prices=flat_prices)
        result = scan_for_tp_exit(legs, bars, net_debit_pts=net_pts, tp_target_inr=tp)
        assert result is None

    def test_credit_case_exits_at_first_positive_mtm(self):
        legs = _legs_with_prices({
            "ce_itm": 10.0, "ce_short": 50.0, "ce_far": 5.0,
            "pe_itm": 10.0, "pe_short": 50.0, "pe_far": 5.0,
        })
        net_pts, _, tp = compute_entry_economics(legs, 1.5)
        assert tp == 0.0

        def prices_at(i):
            base = {k: l.entry_price for k, l in legs.items()}
            if i >= 1:
                base["pe_short"] -= 1.0  # short price down → SELL leg gains
            return base

        bars = self._bars(n_bars=3, fn_prices=prices_at)
        result = scan_for_tp_exit(legs, bars, net_debit_pts=net_pts, tp_target_inr=tp)
        assert result is not None
        exit_ts, _, mtm = result
        assert exit_ts == bars[1]["datetime"]
        assert mtm > 0


class TestBuildBarStream:
    def _legs(self):
        return _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })

    def test_two_clean_bars(self):
        legs = self._legs()
        rows = []
        for i, ts in enumerate([
            "2025-06-13T11:01:00+05:30", "2025-06-13T11:02:00+05:30"]):
            for k, leg in legs.items():
                rows.append(_row(ts, leg.option_type, leg.strike_offset, 100 + i))
        df = pd.DataFrame(rows)
        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        assert len(bars) == 2
        assert all(set(b["prices"]) == set(LEG_KEY_ORDER) for b in bars)
        assert bars[0]["prices"]["ce_itm"] == 100
        assert bars[1]["prices"]["ce_itm"] == 101

    def test_short_gap_carry_forwards(self):
        legs = self._legs()
        rows = []
        for k, leg in legs.items():
            rows.append(_row("2025-06-13T11:01:00+05:30", leg.option_type,
                             leg.strike_offset, 100))
        for k, leg in legs.items():
            if k == "ce_itm":
                continue
            rows.append(_row("2025-06-13T11:02:00+05:30", leg.option_type,
                             leg.strike_offset, 101))
        for k, leg in legs.items():
            rows.append(_row("2025-06-13T11:03:00+05:30", leg.option_type,
                             leg.strike_offset, 102))
        df = pd.DataFrame(rows)
        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        assert len(bars) == 3
        # 11:02 ce_itm carried forward from 11:01 (=100); other legs at 101
        assert bars[1]["prices"]["ce_itm"] == 100
        assert bars[1]["prices"]["ce_short"] == 101

    def test_long_gap_emits_force_exit_marker(self):
        legs = self._legs()
        # 11:01 full; ce_itm absent for 31+ minutes → force_exit at 11:32
        rows = []
        for k, leg in legs.items():
            rows.append(_row("2025-06-13T11:01:00+05:30", leg.option_type,
                             leg.strike_offset, 100))
        for m in range(2, 33):  # 11:02..11:32 inclusive (31 minutes)
            ts = f"2025-06-13T11:{m:02d}:00+05:30"
            for k, leg in legs.items():
                if k == "ce_itm":
                    continue
                rows.append(_row(ts, leg.option_type, leg.strike_offset, 101))
        df = pd.DataFrame(rows)

        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        force_bars = [b for b in bars if b.get("force_exit")]
        assert len(force_bars) == 1
        fb = force_bars[0]
        # Force exit yields the LAST FULLY-OBSERVED bar (11:01) with all 6 legs
        assert set(fb["prices"]) == set(LEG_KEY_ORDER)
        assert fb["prices"]["ce_itm"] == 100
        assert bars[-1] is fb


def _build_synthetic_week_df():
    """Build a tiny synthetic option dataset for a single week.

    Friday 2025-06-13 (entry day) and Tuesday 2025-06-17 (expiry day).
    11:00 entry. Premiums chosen so net is a debit and TP doesn't fire on
    a flat market → exit at 15:25 expiry close.
    """
    rows = []
    legs_at_entry = [
        # Anchor ATM row (offset 0) — needed for resolve_atm_strike
        ("CE",  0, "ATM",  90.0),
        ("CE", -1, "ITM", 120.0),
        ("CE",  4, "OTM",  35.0),
        ("CE",  5, "OTM",  22.0),
        ("PE",  1, "ITM", 115.0),
        ("PE", -4, "OTM",  32.0),
        ("PE", -5, "OTM",  20.0),
    ]
    # Sprinkle bars from 11:00 Fri through 15:25 Tue.
    for day in ["2025-06-13", "2025-06-16", "2025-06-17"]:
        for h in range(11, 16):
            for m in range(0, 60, 15):
                if day == "2025-06-17" and (h > 15 or (h == 15 and m > 25)):
                    continue
                ts = f"{day}T{h:02d}:{m:02d}:00+05:30"
                for opt, off, mny, op in legs_at_entry:
                    rows.append({
                        "datetime": ts, "underlying": "NIFTY",
                        "option_type": opt, "expiry_code": 1, "expiry_type": "WEEK",
                        "strike": 24500.0 + off * 50, "atm_strike": 24500.0,
                        "strike_offset": off, "moneyness": mny,
                        "spot": 24512.0, "open": op, "high": op,
                        "low": op, "close": op, "volume": 1, "oi": 1, "iv": 15.0,
                    })
    return pd.DataFrame(rows)


class TestRunOneWeek:
    def test_flat_market_exits_at_expiry_with_zero_pnl(self):
        df = _build_synthetic_week_df()
        ctx = WeekContext(
            expiry_date=date(2025, 6, 17),
            entry_date=date(2025, 6, 13),
            entry_time_str="11:00",
            expiry_squareoff_time_str="15:25",
            tp_multiple=1.5,
            data_gap_force_exit_minutes=30,
            leg_specs=DEFAULT_LEG_SPECS,
        )
        trade = run_one_week(df, ctx)
        assert trade is not None
        assert trade.skip_reason is None
        assert trade.exit_reason == "EXPIRY"
        assert trade.pnl_inr == pytest.approx(0.0, abs=1e-6)

    def test_skip_when_entry_bar_missing(self):
        df = _build_synthetic_week_df()
        df = df[df["datetime"] != "2025-06-13T11:00:00+05:30"]
        ctx = WeekContext(
            expiry_date=date(2025, 6, 17),
            entry_date=date(2025, 6, 13),
            entry_time_str="11:00",
            expiry_squareoff_time_str="15:25",
            tp_multiple=1.5,
            data_gap_force_exit_minutes=30,
            leg_specs=DEFAULT_LEG_SPECS,
        )
        trade = run_one_week(df, ctx)
        assert trade is not None
        assert trade.skip_reason is not None
        assert "no_entry_bar" in trade.skip_reason or "missing_strike" in trade.skip_reason


def _make_synthetic_multi_week_df():
    """Build a 3-week synthetic dataset.  Friday entry, Tuesday expiry."""
    weeks = [
        (date(2025, 6, 17), date(2025, 6, 13)),
        (date(2025, 6, 24), date(2025, 6, 20)),
        (date(2025, 7, 1),  date(2025, 6, 27)),
    ]
    legs_at_entry = [
        ("CE",  0, "ATM",  90.0),
        ("CE", -1, "ITM", 120.0),
        ("CE",  4, "OTM",  35.0),
        ("CE",  5, "OTM",  22.0),
        ("PE",  1, "ITM", 115.0),
        ("PE", -4, "OTM",  32.0),
        ("PE", -5, "OTM",  20.0),
    ]
    rows = []
    for expiry_d, entry_d in weeks:
        # Trading days: entry_d (Fri), the Monday between, expiry_d (Tue).
        monday = expiry_d - timedelta(days=1)
        timestamps = [
            f"{entry_d.isoformat()}T11:00:00+05:30",
            f"{monday.isoformat()}T11:00:00+05:30",
            f"{expiry_d.isoformat()}T15:25:00+05:30",
        ]
        for ts in timestamps:
            for opt, off, mny, op in legs_at_entry:
                rows.append({
                    "datetime": ts, "underlying": "NIFTY",
                    "option_type": opt, "expiry_code": 1, "expiry_type": "WEEK",
                    "strike": 24500.0 + off * 50, "atm_strike": 24500.0,
                    "strike_offset": off, "moneyness": mny,
                    "spot": 24512.0, "open": op, "high": op,
                    "low": op, "close": op, "volume": 1, "oi": 1, "iv": 15.0,
                })
    return pd.DataFrame(rows), [w[0] for w in weeks]


class TestRunBacktest:
    def test_three_weeks_synthetic(self):
        df, expiries = _make_synthetic_multi_week_df()
        config = {
            "name": "debit_spread", "strategy_type": "debit_spread",
            "instruments": ["NIFTY"],
            "entry": {"days_before_expiry": 2, "entry_time": "11:00"},
            "structure": {
                "ce_legs": [
                    {"side": "BUY", "lots": 1, "strike_offset": -1},
                    {"side": "SELL", "lots": 3, "strike_offset": 4},
                    {"side": "BUY", "lots": 2, "strike_offset": 5},
                ],
                "pe_legs": [
                    {"side": "BUY", "lots": 1, "strike_offset": 1},
                    {"side": "SELL", "lots": 3, "strike_offset": -4},
                    {"side": "BUY", "lots": 2, "strike_offset": -5},
                ],
            },
            "exit": {
                "tp_multiple_of_max_loss": 1.5,
                "expiry_squareoff_time": "15:25",
                "data_gap_force_exit_minutes": 30,
            },
            "sizing": {"sets_per_trade": 1, "reference_capital": 300000},
            "metrics": {"risk_free_rate": 0.06, "annualization_factor": 52},
            "backtest_start": "2025-06-01",
            "backtest_end":   "2025-07-31",
        }
        result = run_backtest(df, config, expiry_dates=expiries)
        assert "trades" in result
        assert len(result["trades"]) == 3
        assert all(t.skip_reason is None for t in result["trades"])
        assert all(t.pnl_inr == pytest.approx(0.0) for t in result["trades"])
        # running_equity_inr is set on every trade
        assert all(not _is_nan(t.running_equity_inr) for t in result["trades"])


def _is_nan(x):
    try:
        return x != x
    except Exception:
        return False


class TestBuildEquityCurve:
    def test_three_trades_step_function(self):
        trades = [
            DebitSpreadTrade(
                expiry_date="2025-06-17", entry_date="2025-06-13",
                entry_time="11:00", atm_strike=24500.0, spot_at_entry=24500.0,
                net_debit_pts=80.0, net_debit_inr=5200.0, tp_target_inr=7800.0,
                exit_time="15:25", exit_reason="EXPIRY",
                pnl_pts=10.0, pnl_inr=650.0, return_pct=0.00216,
                running_equity_inr=300650.0, skip_reason=None, legs={},
            ),
            DebitSpreadTrade(
                expiry_date="2025-06-24", entry_date="2025-06-20",
                entry_time="11:00", atm_strike=24500.0, spot_at_entry=24500.0,
                net_debit_pts=80.0, net_debit_inr=5200.0, tp_target_inr=7800.0,
                exit_time="15:25", exit_reason="EXPIRY",
                pnl_pts=-20.0, pnl_inr=-1300.0, return_pct=-0.00433,
                running_equity_inr=299350.0, skip_reason=None, legs={},
            ),
            DebitSpreadTrade(
                expiry_date="2025-07-01", entry_date="2025-06-27",
                entry_time="11:00", atm_strike=24500.0, spot_at_entry=24500.0,
                net_debit_pts=80.0, net_debit_inr=5200.0, tp_target_inr=7800.0,
                exit_time="15:25", exit_reason="EXPIRY",
                pnl_pts=30.0, pnl_inr=1950.0, return_pct=0.0065,
                running_equity_inr=301300.0, skip_reason=None, legs={},
            ),
        ]
        curve = build_equity_curve(trades, starting_capital=300_000.0)
        assert curve.iloc[0]["equity_inr"] == 300650.0
        assert curve.iloc[1]["equity_inr"] == 299350.0
        assert curve.iloc[2]["equity_inr"] == 301300.0
        assert curve.iloc[1]["drawdown_inr"] == pytest.approx(300650.0 - 299350.0)
        assert curve.iloc[2]["drawdown_inr"] == pytest.approx(0.0)

    def test_skipped_trades_carry_equity_flat(self):
        trades = [
            DebitSpreadTrade(
                expiry_date="2025-06-17", entry_date="2025-06-13",
                entry_time="11:00", atm_strike=float("nan"),
                spot_at_entry=float("nan"),
                net_debit_pts=float("nan"), net_debit_inr=float("nan"),
                tp_target_inr=float("nan"),
                exit_time="", exit_reason="",
                pnl_pts=0.0, pnl_inr=0.0, return_pct=0.0,
                running_equity_inr=300000.0,
                skip_reason="missing_strike: ce_far",
                legs={},
            ),
        ]
        curve = build_equity_curve(trades, starting_capital=300_000.0)
        assert curve.iloc[0]["equity_inr"] == 300000.0
        assert curve.iloc[0]["drawdown_inr"] == 0.0


class TestSharpeSortino:
    def test_sharpe_positive_returns(self):
        returns = [0.01, 0.005, 0.015, 0.01, 0.005]
        s = compute_sharpe(returns, risk_free_rate=0.06, periods_per_year=52)
        # mean = 0.009 ; sd ≈ 0.004183 (sample stdev)
        # weekly_rfr = 0.06/52
        expected = (0.009 - 0.06/52) / 0.0041833 * math.sqrt(52)
        assert s == pytest.approx(expected, rel=0.01)

    def test_sharpe_zero_stdev_returns_nan(self):
        returns = [0.01, 0.01, 0.01]
        s = compute_sharpe(returns, risk_free_rate=0.06, periods_per_year=52)
        assert math.isnan(s)

    def test_sortino_only_downside(self):
        returns = [0.02, -0.01, 0.03, -0.02, 0.01]
        sortino = compute_sortino(returns, risk_free_rate=0.06, periods_per_year=52)
        assert not math.isnan(sortino)


class TestMaxConsecutiveLosses:
    def test_no_losses(self):
        assert max_consecutive_losses([100.0, 50.0, 200.0]) == 0

    def test_basic_streak(self):
        assert max_consecutive_losses([-1.0, -2.0, 1.0, -1.0]) == 2

    def test_skipped_pnl_zero_breaks_streak(self):
        assert max_consecutive_losses([-1.0, 0.0, -1.0, -1.0]) == 2


class TestWriters:
    def test_trades_csv_has_per_leg_columns(self, tmp_path):
        legs = _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })
        for leg in legs.values():
            leg.exit_price = leg.entry_price
        trade = DebitSpreadTrade(
            expiry_date="2025-06-17", entry_date="2025-06-13",
            entry_time="11:00", atm_strike=24500.0, spot_at_entry=24512.0,
            net_debit_pts=118.0, net_debit_inr=7670.0, tp_target_inr=11505.0,
            exit_time="15:25", exit_reason="EXPIRY",
            pnl_pts=0.0, pnl_inr=0.0, return_pct=0.0,
            running_equity_inr=300000.0, skip_reason=None, legs=legs,
        )
        path = tmp_path / "trades.csv"
        write_trades_csv([trade], path)
        df = pd.read_csv(path)
        for k in LEG_KEY_ORDER:
            assert f"{k}_strike" in df.columns
            assert f"{k}_entry" in df.columns
            assert f"{k}_exit" in df.columns
        assert df.iloc[0]["exit_reason"] == "EXPIRY"

    def test_equity_csv_columns(self, tmp_path):
        trades = [
            DebitSpreadTrade(
                expiry_date="2025-06-17", entry_date="2025-06-13",
                entry_time="11:00", atm_strike=24500.0, spot_at_entry=24500.0,
                net_debit_pts=80.0, net_debit_inr=5200.0, tp_target_inr=7800.0,
                exit_time="15:25", exit_reason="EXPIRY",
                pnl_pts=10.0, pnl_inr=650.0, return_pct=0.00216,
                running_equity_inr=300650.0, skip_reason=None, legs={},
            ),
        ]
        path = tmp_path / "equity.csv"
        write_equity_csv(trades, starting_capital=300000.0, path=path)
        df = pd.read_csv(path)
        assert {"date", "equity_inr", "drawdown_inr", "drawdown_pct", "in_trade"}.issubset(df.columns)


class TestPrintSummary:
    def test_summary_prints_required_lines(self, capsys):
        summary = {
            "total_weeks_processed": 10, "trades_placed": 9,
            "trades_skipped": 1, "wins": 6, "losses": 3,
            "win_rate": 6/9, "loss_rate": 3/9, "pct_profitable_weeks": 6/9,
            "mean_pnl_inr": 500.0, "median_pnl_inr": 200.0,
            "total_pnl_inr": 4500.0, "total_return_pct": 0.015,
            "max_drawdown_inr": 1500.0, "max_drawdown_pct": 0.005,
            "max_consecutive_losses": 2,
            "sharpe": 1.7, "sortino": 2.4,
            "best_trade_inr": 2000.0, "worst_trade_inr": -800.0,
            "exit_reason_counts": {"TP": 4, "EXPIRY": 5},
            "skip_reason_counts": {"missing_strike: ce_far": 1},
        }
        print_summary(summary)
        out = capsys.readouterr().out
        assert "Total weeks processed: 10" in out
        assert "Trades placed: 9" in out
        assert "Sharpe" in out
        assert "Sortino" in out
        assert "Max drawdown" in out
        assert "TP" in out and "EXPIRY" in out


class TestEntrypoint:
    @pytest.mark.slow
    def test_smoke_run_with_real_data(self, tmp_path):
        cfg = {
            "name": "debit_spread", "strategy_type": "debit_spread",
            "instruments": ["NIFTY"],
            "entry": {"days_before_expiry": 2, "entry_time": "11:00"},
            "structure": {
                "ce_legs": [
                    {"side": "BUY", "lots": 1, "strike_offset": -1},
                    {"side": "SELL", "lots": 3, "strike_offset": 4},
                    {"side": "BUY", "lots": 2, "strike_offset": 5},
                ],
                "pe_legs": [
                    {"side": "BUY", "lots": 1, "strike_offset": 1},
                    {"side": "SELL", "lots": 3, "strike_offset": -4},
                    {"side": "BUY", "lots": 2, "strike_offset": -5},
                ],
            },
            "exit": {
                "tp_multiple_of_max_loss": 1.5,
                "expiry_squareoff_time": "15:25",
                "data_gap_force_exit_minutes": 30,
            },
            "sizing": {"sets_per_trade": 1, "reference_capital": 300000},
            "metrics": {"risk_free_rate": 0.06, "annualization_factor": 52},
            "backtest_start": "2025-06-01",
            "backtest_end":   "2025-06-30",
        }
        result = run(
            cfg,
            options_path="data/options/nifty/NIFTY_OPTIONS_1m.parquet",
            output_dir=str(tmp_path),
        )
        assert "trades" in result
        assert "summary" in result
        assert any(p.suffix == ".csv" and "trades" in p.name for p in tmp_path.iterdir())
        assert any(p.suffix == ".csv" and "equity" in p.name for p in tmp_path.iterdir())


class TestRealDataIntegration:
    @pytest.mark.slow
    def test_one_known_week(self):
        """Run a single known-good expiry week end-to-end against real parquet.

        Doesn't pin exact P&L (varies with parquet updates), but asserts the
        trade is non-skipped, has all 6 legs filled, and exit_reason is one of
        {TP, EXPIRY, data_gap_force_exit}.
        """
        df = pd.read_parquet("data/options/nifty/NIFTY_OPTIONS_1m.parquet")
        df = df[df["underlying"] == "NIFTY"]
        # Normalize tz-aware datetime to ISO 8601 strings (matches what run() does)
        if pd.api.types.is_datetime64_any_dtype(df["datetime"]):
            df = df.assign(
                datetime=df["datetime"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
            )
            df["datetime"] = df["datetime"].str.replace(
                r"([+\-]\d{2})(\d{2})$", r"\1:\2", regex=True
            )
        df = df[(df["datetime"] >= "2025-06-09T00:00:00+05:30") &
                (df["datetime"] <= "2025-06-18T23:59:59+05:30")]

        expiry = date(2025, 6, 17)  # Tuesday
        trading_days = sorted(
            {pd.to_datetime(ts).date() for ts in df["datetime"].unique()}
        )
        ctx = WeekContext(
            expiry_date=expiry,
            entry_date=compute_entry_date(expiry, trading_days, 2),
            entry_time_str="11:00",
            expiry_squareoff_time_str="15:25",
            tp_multiple=1.5,
            data_gap_force_exit_minutes=30,
            leg_specs=DEFAULT_LEG_SPECS,
        )
        trade = run_one_week(df, ctx)
        assert trade.skip_reason is None, f"week skipped: {trade.skip_reason}"
        assert trade.exit_reason in {"TP", "EXPIRY", "data_gap_force_exit"}
        assert len(trade.legs) == 6
        for leg in trade.legs.values():
            assert leg.exit_price is not None
