"""Tests for zero_credit backtest engine."""
from datetime import date

import pandas as pd
import pytest

from engine.zero_credit_backtest import (
    LegSpec,
    LegFill,
    ZeroCreditTrade,
    LOT_SIZE_NIFTY,
)


class TestDataclasses:
    def test_legspec_holds_premium_target(self):
        spec = LegSpec(option_type="CE", side="BUY", lots=1, premium_target_inr=100.0)
        assert spec.option_type == "CE"
        assert spec.side == "BUY"
        assert spec.lots == 1
        assert spec.premium_target_inr == 100.0

    def test_legfill_records_strike_and_prices(self):
        fill = LegFill(
            option_type="CE", side="BUY", lots=1,
            premium_target_inr=100.0,
            strike=24500.0,
            entry_price=98.5,
            exit_price=120.0,
        )
        assert fill.strike == 24500.0
        assert fill.entry_price == 98.5
        assert fill.exit_price == 120.0

    def test_trade_constructs(self):
        trade = ZeroCreditTrade(
            date="2026-04-08", entry_time="09:20",
            atm_strike=24500.0, spot_at_entry=24512.5,
            net_debit_pts=0.5, net_debit_inr=32.5, tp_target_inr=1000.0,
            exit_time="11:30", exit_reason="TP",
            pnl_pts=15.5, pnl_inr=1007.5, return_pct=0.0050375,
            running_equity_inr=201007.5,
            skip_reason=None, legs={},
        )
        assert trade.exit_reason == "TP"
        assert trade.skip_reason is None

    def test_lot_size_constant(self):
        assert LOT_SIZE_NIFTY == 65


from engine.zero_credit_backtest import (
    pick_strike_by_premium,
    PickResult,
)


def _option_row(strike, opt_type, open_, atm_strike=24500.0):
    """Mock one parquet row at the entry minute."""
    return {
        "datetime": "2026-04-08T09:20:00+05:30",
        "underlying": "NIFTY",
        "option_type": opt_type,
        "expiry_type": "WEEK",
        "expiry_code": 1,
        "atm_strike": atm_strike,
        "strike_offset": int(round((strike - atm_strike) / 50)),
        "moneyness": "ATM" if strike == atm_strike else ("OTM" if (
            (opt_type == "CE" and strike > atm_strike)
            or (opt_type == "PE" and strike < atm_strike)
        ) else "ITM"),
        "strike": strike,
        "spot": atm_strike,
        "open": open_,
        "high": open_,
        "low": open_,
        "close": open_,
        "volume": 1, "oi": 1, "iv": 15.0,
    }


class TestPickStrikeByPremium:
    def _slice(self, rows):
        return pd.DataFrame(rows)

    def test_picks_closest_premium_target_100(self):
        rows = [
            _option_row(24400, "CE", 140.0),
            _option_row(24450, "CE", 110.0),
            _option_row(24500, "CE",  95.0),  # delta=5
            _option_row(24550, "CE",  80.0),  # delta=20
            _option_row(24600, "CE",  40.0),
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="CE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert isinstance(result, PickResult)
        assert result.strike == 24500.0
        assert result.entry_price == 95.0
        assert result.skipped is False

    def test_tiebreak_closer_to_atm(self):
        # Two strikes equidistant in premium (delta=10 each). Pick closer to ATM.
        rows = [
            _option_row(24550, "CE", 90.0, atm_strike=24500.0),   # |strike|=50
            _option_row(24400, "CE", 110.0, atm_strike=24500.0),  # |strike|=100
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="CE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.strike == 24550.0  # closer to ATM (50 vs 100)

    def test_tiebreak_lower_strike_for_ce_when_strike_distance_also_ties(self):
        # Both delta-premium=10 AND |strike|=50. CE -> pick lower strike.
        rows = [
            _option_row(24450, "CE", 110.0, atm_strike=24500.0),
            _option_row(24550, "CE",  90.0, atm_strike=24500.0),
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="CE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.strike == 24450.0

    def test_tiebreak_higher_strike_for_pe_when_strike_distance_also_ties(self):
        rows = [
            _option_row(24450, "PE",  90.0, atm_strike=24500.0),
            _option_row(24550, "PE", 110.0, atm_strike=24500.0),
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="PE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.strike == 24550.0

    def test_skip_when_no_strike_within_tolerance(self):
        rows = [
            _option_row(24400, "CE", 200.0),
            _option_row(24500, "CE", 130.0),  # delta=30, > tolerance=20
            _option_row(24600, "CE", 40.0),   # delta=60
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="CE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.skipped is True
        assert result.skip_reason == "no_strike_within_tolerance"

    def test_filters_to_specified_option_type(self):
        rows = [
            _option_row(24500, "CE", 95.0),
            _option_row(24500, "PE", 99.0),  # would beat the CE on PE-side query
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="PE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.strike == 24500.0
        assert result.option_type == "PE"
        assert result.entry_price == 99.0

    def test_empty_slice_skips(self):
        result = pick_strike_by_premium(
            pd.DataFrame(), option_type="CE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.skipped is True
        assert result.skip_reason == "no_strike_within_tolerance"


from engine.zero_credit_backtest import resolve_atm_strike


class TestResolveAtmStrike:
    def test_single_atm_row(self):
        rows = [
            _option_row(24400, "CE", 140.0, atm_strike=24500.0),
            _option_row(24500, "CE",  95.0, atm_strike=24500.0),  # ATM-tagged
        ]
        df = pd.DataFrame(rows)
        df.loc[df["strike"] == 24500, "moneyness"] = "ATM"
        atm, spot = resolve_atm_strike(df)
        assert atm == 24500.0
        assert spot == 24500.0

    def test_no_atm_returns_none(self):
        rows = [_option_row(24500, "CE", 95.0, atm_strike=24500.0)]
        df = pd.DataFrame(rows)
        df["moneyness"] = "OTM"
        atm, spot = resolve_atm_strike(df)
        assert atm is None
        assert spot is None

    def test_multiple_atm_picks_closest_to_spot(self):
        rows = [
            _option_row(24450, "CE", 110.0, atm_strike=24500.0),
            _option_row(24550, "CE",  90.0, atm_strike=24500.0),
        ]
        df = pd.DataFrame(rows)
        df["moneyness"] = "ATM"
        df["spot"] = 24512.0
        # |24450 - 24512| = 62, |24550 - 24512| = 38 -> 24550 wins.
        atm, spot = resolve_atm_strike(df)
        assert atm == 24550.0
        assert spot == 24512.0

    def test_empty_returns_none(self):
        atm, spot = resolve_atm_strike(pd.DataFrame())
        assert atm is None
        assert spot is None


from engine.zero_credit_backtest import (
    compute_entry_economics,
    compute_mtm_inr,
    scan_for_tp_exit,
)


def _legs_with_prices(prices: dict):
    """Build {leg_key: LegFill} dict with the supplied entry prices and the
    canonical 1/1/2/2 lots, Rs100/Rs100/Rs50/Rs50 targets."""
    specs = {
        "ce_long":  ("CE", "BUY",  1, 100.0),
        "pe_long":  ("PE", "BUY",  1, 100.0),
        "ce_short": ("CE", "SELL", 2,  50.0),
        "pe_short": ("PE", "SELL", 2,  50.0),
    }
    legs = {}
    strike_map = {
        "ce_long": 24500.0, "pe_long": 24500.0,
        "ce_short": 24600.0, "pe_short": 24400.0,
    }
    for k, (ot, side, lots, target) in specs.items():
        legs[k] = LegFill(
            option_type=ot, side=side, lots=lots,
            premium_target_inr=target,
            strike=strike_map[k],
            entry_price=prices[k],
        )
    return legs


class TestComputeEntryEconomics:
    def test_perfect_zero_credit(self):
        # 1*100 + 1*100 - 2*50 - 2*50 = 0
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, net_inr, _ = compute_entry_economics(
            legs, tp_target_inr_fixed=1000.0,
        )
        assert net_pts == pytest.approx(0.0)
        assert net_inr == pytest.approx(0.0)

    def test_small_debit(self):
        legs = _legs_with_prices({
            "ce_long": 105.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        # signed = +1*105 + 1*100 - 2*50 - 2*50 = 5 pts
        net_pts, net_inr, tp = compute_entry_economics(
            legs, tp_target_inr_fixed=1000.0,
        )
        assert net_pts == pytest.approx(5.0)
        assert net_inr == pytest.approx(5.0 * 65)
        assert tp == 1000.0

    def test_tp_target_is_fixed_rupee_value(self):
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        _, _, tp = compute_entry_economics(legs, tp_target_inr_fixed=2500.0)
        assert tp == 2500.0


class TestComputeMtmInr:
    def test_zero_when_prices_equal_entry(self):
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, tp_target_inr_fixed=1000.0)
        prices = {"ce_long": 100.0, "pe_long": 100.0,
                  "ce_short": 50.0, "pe_short": 50.0}
        mtm = compute_mtm_inr(legs, prices, net_pts)
        assert mtm == pytest.approx(0.0)

    def test_positive_mtm_when_shorts_decay(self):
        # Both shorts decay 10 pts each.
        # signed_now = +1*100 + +1*100 + -2*40 + -2*40 = 200 - 160 = 40
        # net_debit  = 0
        # mtm_pts = 40 -> mtm_inr = 40 * 65 = 2600
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, tp_target_inr_fixed=1000.0)
        prices = {"ce_long": 100.0, "pe_long": 100.0,
                  "ce_short": 40.0, "pe_short": 40.0}
        mtm = compute_mtm_inr(legs, prices, net_pts)
        assert mtm == pytest.approx(40.0 * 65)


class TestScanForTpExit:
    def test_returns_first_bar_at_or_above_tp(self):
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, tp_target_inr_fixed=1000.0)
        bars = [
            {"datetime": pd.Timestamp("2026-04-08T09:21:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 47, "pe_short": 47}},
            # signed_now = 200 - 2*47 - 2*47 = 12 -> mtm=780 < TP
            {"datetime": pd.Timestamp("2026-04-08T09:22:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 42, "pe_short": 42}},
            # signed_now = 32 -> mtm=2080 >= TP -> trigger
        ]
        result = scan_for_tp_exit(legs, bars, net_pts, tp_target_inr=1000.0)
        assert result is not None
        ts, prices, mtm = result
        assert ts == pd.Timestamp("2026-04-08T09:22:00+05:30")
        assert mtm == pytest.approx(32.0 * 65)

    def test_returns_none_if_tp_never_hits(self):
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, tp_target_inr_fixed=1000.0)
        bars = [
            {"datetime": pd.Timestamp("2026-04-08T09:21:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 47, "pe_short": 47}},
        ]
        result = scan_for_tp_exit(legs, bars, net_pts, tp_target_inr=1000.0)
        assert result is None

    def test_skips_bars_with_missing_legs(self):
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, tp_target_inr_fixed=1000.0)
        bars = [
            {"datetime": pd.Timestamp("2026-04-08T09:21:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100}},  # missing shorts
            {"datetime": pd.Timestamp("2026-04-08T09:22:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 42, "pe_short": 42}},
        ]
        result = scan_for_tp_exit(legs, bars, net_pts, tp_target_inr=1000.0)
        assert result is not None
        ts, _, _ = result
        assert ts == pd.Timestamp("2026-04-08T09:22:00+05:30")


from engine.zero_credit_backtest import scan_for_exit_trigger


class TestScanForExitTrigger:
    def _legs_and_net(self):
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, tp_target_inr_fixed=1000.0)
        return legs, net_pts

    def test_tp_fires_first(self):
        legs, net_pts = self._legs_and_net()
        # signed_now = 200 - 2*42 - 2*42 = 32 -> mtm=2080 >= TP=1000.
        bars = [
            {"datetime": pd.Timestamp("2026-04-08T09:22:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 42, "pe_short": 42}},
        ]
        result = scan_for_exit_trigger(
            legs, bars, net_pts, tp_target_inr=1000.0, sl_target_inr=2500.0,
        )
        assert result is not None
        _, _, _, reason = result
        assert reason == "TP"

    def test_sl_fires_when_loss_exceeds_threshold(self):
        legs, net_pts = self._legs_and_net()
        # Both shorts spike: signed_now = 200 - 2*80 - 2*80 = -120 -> mtm=-7800.
        # SL=2500 -> threshold -2500 -> mtm <= -2500 triggers SL.
        bars = [
            {"datetime": pd.Timestamp("2026-04-08T10:00:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 80, "pe_short": 80}},
        ]
        result = scan_for_exit_trigger(
            legs, bars, net_pts, tp_target_inr=1000.0, sl_target_inr=2500.0,
        )
        assert result is not None
        _, _, mtm, reason = result
        assert reason == "SL"
        assert mtm <= -2500.0

    def test_no_trigger_within_band(self):
        legs, net_pts = self._legs_and_net()
        # mtm = 2*65 = 130; well within (-2500, 1000).
        bars = [
            {"datetime": pd.Timestamp("2026-04-08T10:00:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 49, "pe_short": 49}},
        ]
        result = scan_for_exit_trigger(
            legs, bars, net_pts, tp_target_inr=1000.0, sl_target_inr=2500.0,
        )
        assert result is None

    def test_sl_disabled_when_none(self):
        legs, net_pts = self._legs_and_net()
        # Big loss but SL disabled -> no trigger.
        bars = [
            {"datetime": pd.Timestamp("2026-04-08T10:00:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 80, "pe_short": 80}},
        ]
        result = scan_for_exit_trigger(
            legs, bars, net_pts, tp_target_inr=1000.0, sl_target_inr=None,
        )
        assert result is None

    def test_sl_disabled_when_zero(self):
        legs, net_pts = self._legs_and_net()
        bars = [
            {"datetime": pd.Timestamp("2026-04-08T10:00:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 80, "pe_short": 80}},
        ]
        result = scan_for_exit_trigger(
            legs, bars, net_pts, tp_target_inr=1000.0, sl_target_inr=0,
        )
        assert result is None


from engine.zero_credit_backtest import build_bar_stream


def _bar_row(ts, opt_type, strike, close):
    return {
        "datetime": ts, "underlying": "NIFTY",
        "option_type": opt_type, "expiry_code": 1, "expiry_type": "WEEK",
        "strike": strike, "atm_strike": 24500.0,
        "strike_offset": int(round((strike - 24500.0) / 50)),
        "moneyness": "OTM", "spot": 24500.0,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 1, "oi": 1, "iv": 15.0,
    }


class TestBuildBarStream:
    def _legs(self):
        return {
            "ce_long":  LegFill(option_type="CE", side="BUY",  lots=1,
                                premium_target_inr=100.0, strike=24500.0,
                                entry_price=100.0),
            "pe_long":  LegFill(option_type="PE", side="BUY",  lots=1,
                                premium_target_inr=100.0, strike=24500.0,
                                entry_price=100.0),
            "ce_short": LegFill(option_type="CE", side="SELL", lots=2,
                                premium_target_inr=50.0, strike=24600.0,
                                entry_price=50.0),
            "pe_short": LegFill(option_type="PE", side="SELL", lots=2,
                                premium_target_inr=50.0, strike=24400.0,
                                entry_price=50.0),
        }

    def test_yields_per_minute_bars_when_all_legs_present(self):
        legs = self._legs()
        rows = []
        for minute in [21, 22, 23]:
            ts = f"2026-04-08T09:{minute:02d}:00+05:30"
            rows.append(_bar_row(ts, "CE", 24500.0, 99.0))
            rows.append(_bar_row(ts, "PE", 24500.0, 99.0))
            rows.append(_bar_row(ts, "CE", 24600.0, 49.0))
            rows.append(_bar_row(ts, "PE", 24400.0, 49.0))
        df = pd.DataFrame(rows)

        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        assert len(bars) == 3
        for b in bars:
            assert set(b["prices"].keys()) == set(legs.keys())
            assert b["force_exit"] is False

    def test_carries_forward_within_max_gap(self):
        legs = self._legs()
        rows = []
        # ce_long missing at 09:22; available again at 09:23.
        for minute in [21, 22, 23]:
            ts = f"2026-04-08T09:{minute:02d}:00+05:30"
            if minute != 22:
                rows.append(_bar_row(ts, "CE", 24500.0, 99.0))
            rows.append(_bar_row(ts, "PE", 24500.0, 99.0))
            rows.append(_bar_row(ts, "CE", 24600.0, 49.0))
            rows.append(_bar_row(ts, "PE", 24400.0, 49.0))
        df = pd.DataFrame(rows)

        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        # 09:22 still emits (carry-forward of ce_long).
        assert len(bars) == 3

    def test_force_exit_when_gap_exceeds_max(self):
        # ce_long appears at 09:21 and never again; the other 3 legs continue.
        # The 30-min stale gap kicks in once we reach 09:52+.
        legs = self._legs()
        rows = [
            _bar_row("2026-04-08T09:21:00+05:30", "CE", 24500.0, 99.0),  # ce_long
            _bar_row("2026-04-08T09:21:00+05:30", "PE", 24500.0, 99.0),
            _bar_row("2026-04-08T09:21:00+05:30", "CE", 24600.0, 49.0),
            _bar_row("2026-04-08T09:21:00+05:30", "PE", 24400.0, 49.0),
        ]
        # Continue 3 legs (no ce_long) every minute through 09:55.
        for minute in range(22, 56):
            ts = f"2026-04-08T09:{minute:02d}:00+05:30"
            rows.append(_bar_row(ts, "PE", 24500.0, 99.0))
            rows.append(_bar_row(ts, "CE", 24600.0, 49.0))
            rows.append(_bar_row(ts, "PE", 24400.0, 49.0))
        df = pd.DataFrame(rows)

        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        # Bars 09:21 through 09:51 are full (gap of ce_long stays <= 30).
        # At 09:52 the gap on ce_long becomes 31 > 30 -> force_exit.
        assert any(b["force_exit"] for b in bars)
        normal_bars = [b for b in bars if not b["force_exit"]]
        force_bars = [b for b in bars if b["force_exit"]]
        assert len(force_bars) == 1
        # force_exit bar yields the LAST FULL bar's prices (09:51's, which
        # carry-forward ce_long=99.0 from 09:21).
        assert force_bars[0]["prices"]["ce_long"] == 99.0
        # Last normal bar before force exit is 09:51.
        assert normal_bars[-1]["datetime"] == pd.Timestamp("2026-04-08T09:51:00+05:30")


from engine.zero_credit_backtest import run_one_day, DayContext


def _full_chain_at(ts, atm_strike, ce_premiums, pe_premiums, expiry_type="WEEK"):
    """Mock a complete option chain at one timestamp. ce_premiums/pe_premiums
    are dicts {strike: open_price}."""
    rows = []
    for strike, open_ in ce_premiums.items():
        rows.append({
            "datetime": ts, "underlying": "NIFTY",
            "option_type": "CE", "expiry_type": expiry_type, "expiry_code": 1,
            "atm_strike": atm_strike,
            "strike_offset": int(round((strike - atm_strike) / 50)),
            "moneyness": "ATM" if strike == atm_strike else "OTM",
            "strike": strike, "spot": atm_strike,
            "open": open_, "high": open_, "low": open_, "close": open_,
            "volume": 1, "oi": 1, "iv": 15.0,
        })
    for strike, open_ in pe_premiums.items():
        rows.append({
            "datetime": ts, "underlying": "NIFTY",
            "option_type": "PE", "expiry_type": expiry_type, "expiry_code": 1,
            "atm_strike": atm_strike,
            "strike_offset": int(round((strike - atm_strike) / 50)),
            "moneyness": "ATM" if strike == atm_strike else "OTM",
            "strike": strike, "spot": atm_strike,
            "open": open_, "high": open_, "low": open_, "close": open_,
            "volume": 1, "oi": 1, "iv": 15.0,
        })
    return rows


def _make_ctx(**overrides):
    defaults = dict(
        date=date(2026, 4, 8),
        entry_time_str="09:20",
        time_exit_str="15:20",
        buy_premium_target_inr=100.0,
        sell_premium_target_inr=50.0,
        buy_lots=1, sell_lots=2,
        premium_match_tolerance_inr=20.0,
        tp_target_inr=1000.0,
        sl_target_inr=2500.0,
        data_gap_force_exit_minutes=30,
    )
    defaults.update(overrides)
    return DayContext(**defaults)


class TestRunOneDay:
    def test_skip_when_no_entry_bar(self):
        ctx = _make_ctx()
        df = pd.DataFrame()
        trade = run_one_day(df, ctx)
        assert trade.skip_reason == "no_entry_bar"

    def test_skip_when_buy_leg_outside_tolerance(self):
        # No CE strike has open within 20 of 100 -> skip
        rows = _full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24400: 200.0, 24500: 130.0, 24600: 35.0},
            pe_premiums={24400: 30.0,  24500: 105.0, 24600: 195.0},
        )
        ctx = _make_ctx()
        trade = run_one_day(pd.DataFrame(rows), ctx)
        assert trade.skip_reason and trade.skip_reason.startswith(
            "no_strike_within_tolerance"
        )

    def test_time_exit_at_1520_when_tp_never_fires(self):
        rows = []
        rows.extend(_full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
        ))
        # Holding bars: identical prices throughout => MTM never moves => no TP.
        for hh in range(9, 16):
            for mm in range(0, 60):
                if (hh == 9 and mm < 21) or (hh == 15 and mm > 19):
                    continue
                ts = f"2026-04-08T{hh:02d}:{mm:02d}:00+05:30"
                rows.extend(_full_chain_at(
                    ts, atm_strike=24500.0,
                    ce_premiums={24500: 100.0, 24600: 50.0},
                    pe_premiums={24500: 100.0, 24400: 50.0},
                ))
        rows.extend(_full_chain_at(
            "2026-04-08T15:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
        ))
        df = pd.DataFrame(rows)

        ctx = _make_ctx()
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        assert trade.exit_reason == "TIME"
        assert trade.exit_time == "15:20"
        assert trade.pnl_inr == pytest.approx(0.0)

    def test_tp_fires_intraday(self):
        rows = []
        rows.extend(_full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
        ))
        # 09:21: shorts decay enough to clear TP.
        # signed_now = 1*100 + 1*100 + -2*30 + -2*30 = 80; net=0.
        # mtm_pts = 80 -> mtm_inr = 5200 >= 1000 -> TP at 09:21.
        rows.extend(_full_chain_at(
            "2026-04-08T09:21:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 30.0},
            pe_premiums={24500: 100.0, 24400: 30.0},
        ))
        df = pd.DataFrame(rows)

        ctx = _make_ctx()
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        assert trade.exit_reason == "TP"
        assert trade.exit_time == "09:21"
        assert trade.pnl_inr == pytest.approx(80.0 * 65)

    def test_sl_fires_intraday(self):
        rows = []
        rows.extend(_full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
        ))
        # 09:21: shorts spike -> big loss.
        # signed_now = 200 - 2*100 - 2*100 = -200; net=0; mtm_pts=-200; mtm_inr=-13000.
        # SL=2500 -> threshold -2500; -13000 <= -2500 -> SL fires.
        rows.extend(_full_chain_at(
            "2026-04-08T09:21:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 100.0},
            pe_premiums={24500: 100.0, 24400: 100.0},
        ))
        df = pd.DataFrame(rows)

        ctx = _make_ctx(sl_target_inr=2500.0)
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        assert trade.exit_reason == "SL"
        assert trade.exit_time == "09:21"
        assert trade.pnl_inr <= -2500.0

    def test_no_sl_when_disabled(self):
        rows = []
        rows.extend(_full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
        ))
        # 09:21: big loss; SL disabled -> no exit until 15:20.
        rows.extend(_full_chain_at(
            "2026-04-08T09:21:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 100.0},
            pe_premiums={24500: 100.0, 24400: 100.0},
        ))
        # Continue with the same big-loss prices through 15:20 so neither
        # TP nor SL ever fires; force a TIME exit.
        for hh in range(9, 16):
            for mm in range(0, 60):
                if (hh == 9 and mm <= 21) or (hh == 15 and mm > 19):
                    continue
                ts = f"2026-04-08T{hh:02d}:{mm:02d}:00+05:30"
                rows.extend(_full_chain_at(
                    ts, atm_strike=24500.0,
                    ce_premiums={24500: 100.0, 24600: 100.0},
                    pe_premiums={24500: 100.0, 24400: 100.0},
                ))
        df = pd.DataFrame(rows)

        ctx = _make_ctx(sl_target_inr=None)
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        assert trade.exit_reason == "TIME"

    def test_filters_to_week_expiry_only(self):
        # MONTH-tagged rows present at 09:20 but with very different premiums.
        # The picker must use only WEEK rows.
        rows = _full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
            expiry_type="WEEK",
        )
        rows += _full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 250.0, 24600: 200.0},  # would beat WEEK on delta
            pe_premiums={24500: 250.0, 24400: 200.0},
            expiry_type="MONTH",
        )
        rows += _full_chain_at(
            "2026-04-08T15:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
            expiry_type="WEEK",
        )
        df = pd.DataFrame(rows)

        ctx = _make_ctx()
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        assert trade.legs["ce_long"].entry_price == 100.0


from engine.zero_credit_backtest import run_backtest, parse_config


class TestParseConfig:
    def test_extracts_all_fields(self):
        config = {
            "entry": {"entry_time": "09:20"},
            "structure": {
                "buy_premium_target_inr": 100,
                "sell_premium_target_inr": 50,
                "buy_lots": 1, "sell_lots": 2,
                "premium_match_tolerance_inr": 20,
            },
            "exit": {
                "tp_target_inr": 800,
                "sl_target_inr": 2500,
                "time_exit": "15:20",
                "data_gap_force_exit_minutes": 30,
            },
            "sizing": {"reference_capital": 200000},
            "backtest_start": "2025-01-01",
            "backtest_end":   "2026-05-08",
        }
        params = parse_config(config)
        assert params["entry_time"] == "09:20"
        assert params["time_exit"] == "15:20"
        assert params["buy_premium_target_inr"] == 100.0
        assert params["sell_premium_target_inr"] == 50.0
        assert params["buy_lots"] == 1
        assert params["sell_lots"] == 2
        assert params["premium_match_tolerance_inr"] == 20.0
        assert params["tp_target_inr"] == 800.0
        assert params["sl_target_inr"] == 2500.0
        assert params["data_gap_force_exit_minutes"] == 30
        assert params["reference_capital"] == 200000.0

    def test_sl_target_can_be_omitted(self):
        config = {
            "entry": {"entry_time": "09:20"},
            "structure": {
                "buy_premium_target_inr": 100,
                "sell_premium_target_inr": 50,
                "buy_lots": 1, "sell_lots": 2,
                "premium_match_tolerance_inr": 20,
            },
            "exit": {
                "tp_target_inr": 800,
                "time_exit": "15:20",
                "data_gap_force_exit_minutes": 30,
            },
            "sizing": {"reference_capital": 200000},
            "backtest_start": "2025-01-01",
            "backtest_end":   "2026-05-08",
        }
        params = parse_config(config)
        assert params["sl_target_inr"] is None


class TestRunBacktest:
    def _two_day_df(self):
        rows = []
        for day, atm in [(8, 24500.0), (9, 24600.0)]:
            ts_entry = f"2026-04-{day:02d}T09:20:00+05:30"
            rows += _full_chain_at(
                ts_entry, atm_strike=atm,
                ce_premiums={atm: 100.0, atm + 100: 50.0},
                pe_premiums={atm: 100.0, atm - 100: 50.0},
            )
            ts_post = f"2026-04-{day:02d}T09:21:00+05:30"
            rows += _full_chain_at(
                ts_post, atm_strike=atm,
                ce_premiums={atm: 100.0, atm + 100: 30.0},
                pe_premiums={atm: 100.0, atm - 100: 30.0},
            )
            ts_exit = f"2026-04-{day:02d}T15:20:00+05:30"
            rows += _full_chain_at(
                ts_exit, atm_strike=atm,
                ce_premiums={atm: 100.0, atm + 100: 30.0},
                pe_premiums={atm: 100.0, atm - 100: 30.0},
            )
        return pd.DataFrame(rows)

    def test_runs_each_trading_day_in_range(self):
        df = self._two_day_df()
        config = {
            "entry": {"entry_time": "09:20"},
            "structure": {
                "buy_premium_target_inr": 100, "sell_premium_target_inr": 50,
                "buy_lots": 1, "sell_lots": 2,
                "premium_match_tolerance_inr": 20,
            },
            "exit": {"tp_target_inr": 1000, "time_exit": "15:20",
                     "data_gap_force_exit_minutes": 30},
            "sizing": {"reference_capital": 200000},
            "backtest_start": "2026-04-08",
            "backtest_end":   "2026-04-09",
        }
        result = run_backtest(df, config)
        trades = result["trades"]
        assert len(trades) == 2
        for t in trades:
            assert t.exit_reason == "TP"
            assert t.pnl_inr > 0
        assert trades[0].running_equity_inr > 200000
        assert trades[1].running_equity_inr > trades[0].running_equity_inr
        assert trades[0].return_pct == pytest.approx(
            trades[0].pnl_inr / 200000.0
        )

    def test_skip_day_with_no_data(self):
        df = self._two_day_df()
        config = {
            "entry": {"entry_time": "09:20"},
            "structure": {
                "buy_premium_target_inr": 100, "sell_premium_target_inr": 50,
                "buy_lots": 1, "sell_lots": 2,
                "premium_match_tolerance_inr": 20,
            },
            "exit": {"tp_target_inr": 1000, "time_exit": "15:20",
                     "data_gap_force_exit_minutes": 30},
            "sizing": {"reference_capital": 200000},
            "backtest_start": "2026-04-08",
            "backtest_end":   "2026-04-10",  # 10th has no data -> naturally skipped
        }
        result = run_backtest(df, config)
        trades = result["trades"]
        assert len(trades) == 2


import tempfile

from engine.zero_credit_backtest import (
    build_equity_curve,
    summarize_metrics,
    max_consecutive_losses,
    write_trades_csv,
    write_equity_csv,
    trades_to_dataframe,
    print_summary,
)


def _trade(date_, pnl_inr, exit_reason="TP", skip_reason=None,
           running_equity=200000.0, capital=200000.0):
    return ZeroCreditTrade(
        date=date_, entry_time="09:20",
        atm_strike=24500.0, spot_at_entry=24500.0,
        net_debit_pts=0.0, net_debit_inr=0.0, tp_target_inr=1000.0,
        exit_time="15:20" if skip_reason is None else "",
        exit_reason="" if skip_reason else exit_reason,
        pnl_pts=pnl_inr / 65 if pnl_inr else 0.0,
        pnl_inr=pnl_inr,
        return_pct=pnl_inr / capital if capital else 0.0,
        running_equity_inr=running_equity,
        skip_reason=skip_reason, legs={},
    )


class TestMaxConsecutiveLosses:
    def test_basic_run(self):
        assert max_consecutive_losses([10, -5, -8, -3, 7, -1]) == 3

    def test_no_losses(self):
        assert max_consecutive_losses([10, 5, 0, 7]) == 0

    def test_empty(self):
        assert max_consecutive_losses([]) == 0


class TestBuildEquityCurve:
    def test_one_row_per_trade_with_drawdown(self):
        trades = [
            _trade("2026-04-08",  500.0, running_equity=200500.0),
            _trade("2026-04-09", -1500.0, running_equity=199000.0),
            _trade("2026-04-10",  800.0, running_equity=199800.0),
        ]
        curve = build_equity_curve(trades, starting_capital=200000.0)
        assert len(curve) == 3
        assert curve.iloc[0]["equity_inr"] == 200500.0
        assert curve.iloc[1]["drawdown_inr"] == pytest.approx(1500.0)
        assert curve.iloc[2]["drawdown_inr"] == pytest.approx(700.0)

    def test_skipped_days_carry_running_equity(self):
        trades = [
            _trade("2026-04-08",   500.0, running_equity=200500.0),
            _trade("2026-04-09",     0.0, skip_reason="no_entry_bar",
                   running_equity=200500.0),
            _trade("2026-04-10",  -200.0, running_equity=200300.0),
        ]
        curve = build_equity_curve(trades, starting_capital=200000.0)
        assert len(curve) == 3
        assert bool(curve.iloc[1]["in_trade"]) is False
        assert curve.iloc[1]["equity_inr"] == 200500.0


class TestSummarizeMetrics:
    def test_counts_and_pnl(self):
        trades = [
            _trade("2026-04-08",  1000.0, running_equity=201000.0),
            _trade("2026-04-09", -2000.0, running_equity=199000.0),
            _trade("2026-04-10",   500.0, running_equity=199500.0),
            _trade("2026-04-11",     0.0, skip_reason="no_entry_bar",
                   running_equity=199500.0),
        ]
        s = summarize_metrics(trades, starting_capital=200000.0)
        assert s["total_days_processed"] == 4
        assert s["trades_placed"] == 3
        assert s["trades_skipped"] == 1
        assert s["wins"] == 2
        assert s["losses"] == 1
        assert s["total_pnl_inr"] == pytest.approx(-500.0)
        assert s["best_trade_inr"] == 1000.0
        assert s["worst_trade_inr"] == -2000.0
        assert s["max_consecutive_losses"] == 1
        assert s["exit_reason_counts"]["TP"] == 3
        assert s["skip_reason_counts"]["no_entry_bar"] == 1
        assert "sharpe" not in s
        assert "sortino" not in s


class TestCsvWriters:
    def test_trades_csv_has_per_leg_columns(self):
        trade = _trade("2026-04-08", 1000.0, running_equity=201000.0)
        trade.legs = {
            "ce_long":  LegFill(option_type="CE", side="BUY", lots=1,
                                premium_target_inr=100.0, strike=24500.0,
                                entry_price=98.5, exit_price=110.0),
            "pe_long":  LegFill(option_type="PE", side="BUY", lots=1,
                                premium_target_inr=100.0, strike=24500.0,
                                entry_price=99.5, exit_price=80.0),
            "ce_short": LegFill(option_type="CE", side="SELL", lots=2,
                                premium_target_inr=50.0, strike=24600.0,
                                entry_price=49.0, exit_price=30.0),
            "pe_short": LegFill(option_type="PE", side="SELL", lots=2,
                                premium_target_inr=50.0, strike=24400.0,
                                entry_price=51.0, exit_price=70.0),
        }
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            write_trades_csv([trade], f.name)
            df = pd.read_csv(f.name)
        assert "ce_long_strike" in df.columns
        assert "ce_long_entry" in df.columns
        assert "ce_long_exit" in df.columns
        assert df.iloc[0]["ce_long_strike"] == 24500.0

    def test_equity_csv_columns(self):
        trades = [
            _trade("2026-04-08", 500.0, running_equity=200500.0),
            _trade("2026-04-09", -200.0, running_equity=200300.0),
        ]
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            write_equity_csv(trades, starting_capital=200000.0, path=f.name)
            df = pd.read_csv(f.name)
        assert list(df.columns) == [
            "date", "equity_inr", "drawdown_inr", "drawdown_pct", "in_trade"
        ]
        assert len(df) == 2


from pathlib import Path


class TestRunEntrypoint:
    def test_writes_csvs_and_returns_paths(self, tmp_path):
        rows = []
        for day_str, atm in [("2026-04-08", 24500.0), ("2026-04-09", 24600.0)]:
            for ts_str, ce500, ce600, pe500, pe400 in [
                (f"{day_str}T09:20:00+05:30", 100.0, 50.0, 100.0, 50.0),
                (f"{day_str}T09:21:00+05:30", 100.0, 30.0, 100.0, 30.0),
                (f"{day_str}T15:20:00+05:30", 100.0, 30.0, 100.0, 30.0),
            ]:
                ce_p = {atm: ce500, atm + 100: ce600}
                pe_p = {atm: pe500, atm - 100: pe400}
                rows += _full_chain_at(
                    ts_str, atm_strike=atm,
                    ce_premiums=ce_p, pe_premiums=pe_p,
                )
        df = pd.DataFrame(rows)
        df["datetime"] = pd.to_datetime(df["datetime"])

        parquet_path = tmp_path / "fake.parquet"
        df.to_parquet(parquet_path)

        config = {
            "name": "zero_credit", "strategy_type": "zero_credit",
            "instruments": ["NIFTY"],
            "entry": {"entry_time": "09:20"},
            "structure": {
                "buy_premium_target_inr": 100, "sell_premium_target_inr": 50,
                "buy_lots": 1, "sell_lots": 2,
                "premium_match_tolerance_inr": 20,
            },
            "exit": {"tp_target_inr": 1000, "time_exit": "15:20",
                     "data_gap_force_exit_minutes": 30},
            "sizing": {"reference_capital": 200000},
            "backtest_start": "2026-04-08",
            "backtest_end":   "2026-04-09",
        }
        from engine.zero_credit_backtest import run
        result = run(
            config,
            options_path=str(parquet_path),
            output_dir=str(tmp_path / "out"),
        )

        assert "trades_csv" in result and "equity_csv" in result
        assert Path(result["trades_csv"]).exists()
        assert Path(result["equity_csv"]).exists()
        assert result["summary"]["trades_placed"] == 2
        assert result["summary"]["total_pnl_inr"] > 0
