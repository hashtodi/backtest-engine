"""Tests for engine/oi_wall_backtest.py."""
from datetime import date, datetime as _dt, timedelta

import pandas as pd
import pytest

from engine.oi_wall_backtest import (
    LOT_SIZE_NIFTY,
    DayContext,
    LegFill,
    OiWallTrade,
    check_conditions,
    pick_wall,
    run_one_day,
    run_backtest,
    summarize_metrics,
    trades_to_dataframe,
)


# --------------------------------------------------------------------------- #
#  Fixtures: synthetic row builders                                           #
# --------------------------------------------------------------------------- #

def _row(date_str, time, opt_type, offset, strike, close,
         open_=None, oi=10000, spot=24500.0, atm_strike=24500.0):
    """Build a single synthetic option-row dict for one minute.
    `open_` defaults to `close` (so most tests can ignore the OHLC distinction)."""
    if open_ is None:
        open_ = close
    moneyness = "ATM" if offset == 0 else ("OTM" if (
        (opt_type == "CE" and offset > 0) or (opt_type == "PE" and offset < 0)
    ) else "ITM")
    return {
        "datetime": f"{date_str}T{time}+05:30",
        "underlying": "NIFTY",
        "option_type": opt_type,
        "expiry_type": "MONTH",
        "expiry_code": 1,
        "strike_offset": offset,
        "moneyness": moneyness,
        "strike": strike,
        "spot": spot,
        "open": open_,
        "close": close,
        "oi": oi,
        "_time": time,
        "_date": date_str,
    }


def _full_day_frame(date_str="2025-03-10", spot=24500.0, atm=24500.0,
                    wall_oi_boost=200000, opt_for_wall="CE",
                    wall_offset=3,
                    wall_price_1030_delta=-2.0, wall_oi_1030_delta=5000,
                    entry_time_str="10:30:00",
                    force_exit_time_str="14:30:00"):
    """Build a frame containing the 3 timestamps the engine needs."""
    rows = []
    # 10:00 bar: ATM + 10 OTM CE (offsets 1..10) + 10 OTM PE (offsets -1..-10)
    base_oi = 10000
    for ot in ["CE", "PE"]:
        for off in range(-10, 11):
            strike = atm + off * 50
            close = max(1.0, 200 - abs(off) * 15)
            oi = base_oi + (wall_oi_boost if (ot == opt_for_wall and off == wall_offset) else off * 100)
            rows.append(_row(date_str, "10:00:00", ot, off, strike, close,
                             oi=oi, spot=spot, atm_strike=atm))

    # Signal bar (default 10:30): same strikes/offsets; tweak the wall row to satisfy conditions.
    # Also emit an identical FILL bar one minute later so T+1 fills succeed with the same prices.
    fill_time_str = (_dt.strptime(entry_time_str, "%H:%M:%S")
                     + timedelta(minutes=1)).strftime("%H:%M:%S")
    for bar_time in (entry_time_str, fill_time_str):
        for ot in ["CE", "PE"]:
            for off in range(-10, 11):
                strike = atm + off * 50
                close = max(1.0, 200 - abs(off) * 15)
                oi = base_oi + (wall_oi_boost if (ot == opt_for_wall and off == wall_offset) else off * 100)
                if ot == opt_for_wall and off == wall_offset:
                    # tighten price (cond1), bump OI (cond2)
                    close = close + wall_price_1030_delta
                    oi = oi + wall_oi_1030_delta
                rows.append(_row(date_str, bar_time, ot, off, strike, close,
                                 oi=oi, spot=spot, atm_strike=atm))

    # Forced-exit bar: drop wall row prices, keep spread strikes available
    for ot in ["CE", "PE"]:
        for off in range(-10, 11):
            strike = atm + off * 50
            # synthesize wider spread exits so we can hand-verify P&L
            close = max(0.5, (200 - abs(off) * 15) + (5 if ot == opt_for_wall else 0))
            rows.append(_row(date_str, force_exit_time_str, ot, off, strike, close,
                             oi=base_oi, spot=spot, atm_strike=atm))
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
#  pick_wall                                                                  #
# --------------------------------------------------------------------------- #

class TestPickWall:
    def _slice(self, rows):
        return pd.DataFrame(rows)

    def test_picks_highest_oi_ce(self):
        rows = [
            _row("2025-03-10", "10:00:00", "CE", 1, 24550, 80,  oi=10000),
            _row("2025-03-10", "10:00:00", "CE", 3, 24650, 60,  oi=99999),  # winner
            _row("2025-03-10", "10:00:00", "PE", -2, 24400, 70, oi=80000),
        ]
        ot, off, strike, _p, oi = pick_wall(self._slice(rows))
        assert ot == "CE"
        assert off == 3
        assert strike == 24650
        assert oi == 99999

    def test_picks_pe_when_pe_has_higher_oi(self):
        rows = [
            _row("2025-03-10", "10:00:00", "CE", 5, 24750, 40, oi=10000),
            _row("2025-03-10", "10:00:00", "PE", -4, 24300, 55, oi=50000),
        ]
        ot, off, *_ = pick_wall(self._slice(rows))
        assert ot == "PE"
        assert off == -4

    def test_excludes_atm_and_itm(self):
        rows = [
            _row("2025-03-10", "10:00:00", "CE", 0, 24500, 120, oi=999999),  # ATM
            _row("2025-03-10", "10:00:00", "CE", -1, 24450, 130, oi=999999), # ITM CE
            _row("2025-03-10", "10:00:00", "PE", 1, 24550, 130, oi=999999),  # ITM PE
            _row("2025-03-10", "10:00:00", "CE", 2, 24600, 80,  oi=10),      # OTM CE
        ]
        ot, off, *_ = pick_wall(self._slice(rows))
        assert ot == "CE" and off == 2

    def test_excludes_beyond_offset_10(self):
        rows = [
            _row("2025-03-10", "10:00:00", "CE", 11, 25050, 5, oi=999999),  # too far
            _row("2025-03-10", "10:00:00", "PE", -3, 24350, 70, oi=10),
        ]
        ot, off, *_ = pick_wall(self._slice(rows))
        assert ot == "PE" and off == -3

    def test_ce_wins_cross_type_tie(self):
        rows = [
            _row("2025-03-10", "10:00:00", "CE", 3, 24650, 60, oi=50000),
            _row("2025-03-10", "10:00:00", "PE", -3, 24350, 60, oi=50000),
        ]
        ot, off, *_ = pick_wall(self._slice(rows))
        assert ot == "CE"
        assert off == 3

    def test_closer_to_atm_wins_intra_type_tie(self):
        rows = [
            _row("2025-03-10", "10:00:00", "CE", 5, 24750, 50, oi=50000),
            _row("2025-03-10", "10:00:00", "CE", 2, 24600, 80, oi=50000),  # closer to ATM
        ]
        ot, off, *_ = pick_wall(self._slice(rows))
        assert ot == "CE" and off == 2

    def test_empty_returns_none(self):
        assert pick_wall(pd.DataFrame()) is None


# --------------------------------------------------------------------------- #
#  check_conditions                                                           #
# --------------------------------------------------------------------------- #

class TestCheckConditions:
    def test_both_pass(self):
        c1, c2, n = check_conditions(
            p_10=50, oi_10=10000, p_1030=48, oi_1030=12000,
        )
        assert (c1, c2) == (True, True)
        assert n == 2

    def test_neither_passes(self):
        c1, c2, n = check_conditions(
            p_10=50, oi_10=10000, p_1030=55, oi_1030=8000,
        )
        assert (c1, c2) == (False, False)
        assert n == 0

    def test_equality_counts(self):
        # price equal -> c1 True (<=); oi equal -> c2 True (>=)
        c1, c2, n = check_conditions(
            p_10=50, oi_10=10000, p_1030=50, oi_1030=10000,
        )
        assert (c1, c2) == (True, True)
        assert n == 2

    def test_only_price_passes(self):
        c1, c2, n = check_conditions(
            p_10=50, oi_10=10000, p_1030=48, oi_1030=8000,
        )
        assert (c1, c2) == (True, False)
        assert n == 1


# --------------------------------------------------------------------------- #
#  run_one_day end-to-end on synthetic data                                   #
# --------------------------------------------------------------------------- #

def _ctx(date_str="2025-03-10", minc=1, lots=1, force_exit_time="14:30:00",
         tp_inr=1000.0, sl_inr=1000.0):
    """Test context: defaults match the synthetic 14:30-exit frames and the
    pre-2-lot calibration of TP/SL fixtures (per-lot Rs 1000 / Rs 1000)."""
    return DayContext(
        date=date.fromisoformat(date_str),
        min_conditions_to_enter=minc,
        lots=lots,
        force_exit_time=force_exit_time,
        tp_inr=tp_inr, sl_inr=sl_inr,
    )


class TestRunOneDayCE:
    def test_ce_wall_full_pass(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3,
                             wall_price_1030_delta=-2.0,
                             wall_oi_1030_delta=5000)
        trade = run_one_day(df, _ctx())
        assert trade.skip_reason is None
        assert trade.wall_option_type == "CE"
        assert trade.wall_offset == 3
        assert trade.exit_reason == "TIME"
        assert trade.conditions_passed == 2
        # CE side -> sell offset +2, buy offset +4
        assert {l.strike_offset for l in trade.legs.values()} == {2, 4}
        # P&L per contract = (sell_e + buy_x) - (sell_x + buy_e), * 65 -> INR
        sell_leg = next(l for l in trade.legs.values() if l.side == "SELL")
        buy_leg = next(l for l in trade.legs.values() if l.side == "BUY")
        expected_pts = (sell_leg.entry_price + buy_leg.exit_price) \
                       - (sell_leg.exit_price + buy_leg.entry_price)
        assert trade.pnl_pts == pytest.approx(expected_pts)
        assert trade.pnl_inr == pytest.approx(expected_pts * LOT_SIZE_NIFTY)

    def test_pe_wall_full_pass(self):
        df = _full_day_frame(opt_for_wall="PE", wall_offset=-3)
        trade = run_one_day(df, _ctx())
        assert trade.skip_reason is None
        assert trade.wall_option_type == "PE"
        # PE -> offsets -2 and -4
        assert {l.strike_offset for l in trade.legs.values()} == {-2, -4}


class TestRunOneDaySkips:
    def test_conditions_not_met(self):
        # Force price up (cond1 False) and OI down (cond2 False) so neither fires.
        df = _full_day_frame(
            opt_for_wall="CE", wall_offset=3,
            wall_price_1030_delta=+5.0,          # price went up
            wall_oi_1030_delta=-5000,            # OI dropped
        )
        trade = run_one_day(df, _ctx())
        assert trade.skip_reason is not None
        assert trade.skip_reason.startswith("conditions_not_met")
        assert trade.skip_reason.endswith("/2")
        assert trade.conditions_passed == 0
        # signal fields populated even on skip
        assert trade.wall_option_type == "CE"
        assert trade.wall_offset == 3
        assert trade.pnl_inr == 0.0

    def test_no_data(self):
        trade = run_one_day(pd.DataFrame(), _ctx())
        assert trade.skip_reason == "no_data_on_entry_day"
        assert trade.pnl_inr == 0.0

    def test_no_wall_pick_bar(self):
        df = _full_day_frame()
        df = df[df["_time"] != "10:00:00"].copy()
        trade = run_one_day(df, _ctx())
        assert trade.skip_reason == "no_wall_pick_bar"

    def test_no_entry_bar(self):
        df = _full_day_frame()
        df = df[df["_time"] != "10:30:00"].copy()
        trade = run_one_day(df, _ctx())
        assert trade.skip_reason == "no_entry_bar"

    def test_wall_missing_at_entry(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # Drop the wall row at 10:30 (CE strike_offset=3)
        mask = ~((df["_time"] == "10:30:00")
                 & (df["option_type"] == "CE") & (df["strike_offset"] == 3))
        trade = run_one_day(df[mask].copy(), _ctx())
        assert trade.skip_reason == "wall_missing_at_entry"

    def test_spread_leg_missing(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # Fills are at signal + 1 = 10:31. Drop the +4 buy-leg row at the FILL bar.
        mask = ~((df["_time"] == "10:31:00")
                 & (df["option_type"] == "CE") & (df["strike_offset"] == 4))
        trade = run_one_day(df[mask].copy(), _ctx())
        assert trade.skip_reason == "spread_leg_missing_at_fill"

    def test_no_entry_fill_bar(self):
        """Signal bar present at 10:30, but the T+1 fill bar (10:31) is missing."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        df = df[df["_time"] != "10:31:00"].copy()
        trade = run_one_day(df, _ctx())
        assert trade.skip_reason == "no_entry_fill_bar"

    def test_exit_leg_missing(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # +2 strike == 24600 (atm 24500 + 2*50). Drop that strike at 14:30.
        mask = ~((df["_time"] == "14:30:00")
                 & (df["option_type"] == "CE") & (df["strike"] == 24600))
        trade = run_one_day(df[mask].copy(), _ctx())
        assert trade.skip_reason == "exit_leg_missing_at_force_exit"


# --------------------------------------------------------------------------- #
#  run_backtest + reporting                                                   #
# --------------------------------------------------------------------------- #

def _inject_intraday_minute(df, time_str, opt_type, atm, sell_off, sell_close,
                            buy_off, buy_close, spot=24500.0,
                            date_str="2025-03-10"):
    """Append the spread legs' rows at `time_str` AND auto-emit identical
    rows at `time_str + 1` so the engine's T+1 exit fill can find prices."""
    next_t = (_dt.strptime(time_str, "%H:%M:%S")
              + timedelta(minutes=1)).strftime("%H:%M:%S")
    rows = []
    for t in (time_str, next_t):
        rows.append(_row(date_str, t, opt_type, sell_off,
                         atm + sell_off * 50, sell_close,
                         oi=10000, spot=spot))
        rows.append(_row(date_str, t, opt_type, buy_off,
                         atm + buy_off * 50, buy_close,
                         oi=10000, spot=spot))
    return pd.concat([df, pd.DataFrame(rows)], ignore_index=True)


class TestTpSlExit:
    """CE wall: sell ATM+2 @170, buy ATM+4 @140; credit 30 pts.
    live_pts = 30 + buy_t - sell_t; live_inr = live_pts * 65."""

    def test_tp_hit_intraday(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # 11:00 spread narrows to 10 -> live_pts=20 -> live_inr=1300 >= 1000
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=150.0,
                                     buy_off=4, buy_close=140.0)
        trade = run_one_day(df, _ctx())
        assert trade.skip_reason is None
        assert trade.exit_reason == "TP"
        assert trade.exit_signal_time == "11:00"
        assert trade.exit_time == "11:01"          # T+1 fill
        assert trade.pnl_inr == pytest.approx(20.0 * LOT_SIZE_NIFTY)

    def test_sl_hit_intraday(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # 11:30 spread widens to 60 -> live_pts=-30 -> live_inr=-1950 <= -1000
        df = _inject_intraday_minute(df, "11:30:00", "CE", atm=24500,
                                     sell_off=2, sell_close=200.0,
                                     buy_off=4, buy_close=140.0)
        trade = run_one_day(df, _ctx())
        assert trade.skip_reason is None
        assert trade.exit_reason == "SL"
        assert trade.exit_signal_time == "11:30"
        assert trade.exit_time == "11:31"          # T+1 fill
        assert trade.pnl_inr == pytest.approx(-30.0 * LOT_SIZE_NIFTY)

    def test_disabled_falls_through_to_time(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # An intraday bar that *would* hit TP, but TP/SL/breach are disabled.
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=150.0,
                                     buy_off=4, buy_close=140.0)
        ctx = DayContext(
            date=date.fromisoformat("2025-03-10"),
            min_conditions_to_enter=1,
            lots=1, force_exit_time="14:30:00",
            tp_inr=0.0, sl_inr=0.0, wall_breach_enabled=False,
        )
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        assert trade.exit_reason == "TIME"
        assert trade.exit_time == "14:30"

    def test_first_hit_wins(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # 11:00 narrow (TP). 11:01 widen (SL). Earliest minute wins.
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=150.0,
                                     buy_off=4, buy_close=140.0)
        df = _inject_intraday_minute(df, "11:01:00", "CE", atm=24500,
                                     sell_off=2, sell_close=200.0,
                                     buy_off=4, buy_close=140.0)
        trade = run_one_day(df, _ctx())
        assert trade.exit_reason == "TP"
        assert trade.exit_signal_time == "11:00"
        assert trade.exit_time == "11:01"          # T+1 fill


class TestWallBreachExit:
    """CE wall: atm=24500, wall_offset=3 -> wall_strike=24650.
    Conditions to satisfy at signal:  spot < 24650 (no premature breach).
    Tests inject an intraday minute with a chosen spot to trigger BREACH."""

    def test_ce_wall_breach_inclusive(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # Spot at the wall: 24650 >= wall_strike 24650 -> BREACH (inclusive).
        # Keep leg prices equal to entry so neither TP nor SL fires.
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=170.0,
                                     buy_off=4, buy_close=140.0,
                                     spot=24650.0)
        trade = run_one_day(df, _ctx())
        assert trade.exit_reason == "BREACH"
        assert trade.exit_signal_time == "11:00"
        assert trade.exit_time == "11:01"

    def test_ce_wall_no_breach_below(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # Spot just under the wall: 24649 < 24650 -> no breach. No TP/SL either.
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=170.0,
                                     buy_off=4, buy_close=140.0,
                                     spot=24649.0)
        trade = run_one_day(df, _ctx())
        assert trade.exit_reason == "TIME"
        assert trade.exit_time == "14:30"

    def test_pe_wall_breach_inclusive(self):
        # PE wall: atm=24500, wall_offset=-3 -> wall_strike=24350.
        df = _full_day_frame(opt_for_wall="PE", wall_offset=-3)
        df = _inject_intraday_minute(df, "11:00:00", "PE", atm=24500,
                                     sell_off=-2, sell_close=170.0,
                                     buy_off=-4, buy_close=140.0,
                                     spot=24350.0)
        trade = run_one_day(df, _ctx())
        assert trade.exit_reason == "BREACH"
        assert trade.exit_signal_time == "11:00"
        assert trade.exit_time == "11:01"

    def test_breach_priority_over_sl(self):
        """Same minute: spread widened to fire SL AND spot at wall.
        Priority BREACH > SL -> exit labelled BREACH."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # sell=200, buy=140 -> live_inr = (170+140)-(200+140) * 65 = -1950 -> SL.
        # spot=24650 -> BREACH. Both fire; BREACH wins.
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=200.0,
                                     buy_off=4, buy_close=140.0,
                                     spot=24650.0)
        trade = run_one_day(df, _ctx())
        assert trade.exit_reason == "BREACH"
        assert trade.exit_signal_time == "11:00"

    def test_breach_toggle_does_not_affect_exit_fill_timing(self):
        """T+1 OPEN fill is uniform: breach on or off, TP/SL fire at T and
        fill on T+1. Only the breach CHECK differs between the two toggles."""
        df_on  = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        df_on  = _inject_intraday_minute(df_on,  "11:00:00", "CE", atm=24500,
                                         sell_off=2, sell_close=150.0,
                                         buy_off=4, buy_close=140.0)
        df_off = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        df_off = _inject_intraday_minute(df_off, "11:00:00", "CE", atm=24500,
                                         sell_off=2, sell_close=150.0,
                                         buy_off=4, buy_close=140.0)
        ctx_on = _ctx(); ctx_on.wall_breach_enabled = True
        ctx_off = _ctx(); ctx_off.wall_breach_enabled = False
        t_on  = run_one_day(df_on,  ctx_on)
        t_off = run_one_day(df_off, ctx_off)
        for t in (t_on, t_off):
            assert t.exit_reason == "TP"
            assert t.exit_signal_time == "11:00"
            assert t.exit_time == "11:01"   # T+1, NOT same-bar

    def test_exit_fill_uses_open_not_close(self):
        """T+1 fill must read the OPEN column, not close.
        Force open != close on the fill bar and confirm engine picks the open."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # 11:00 injection sets up a TP signal.
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=150.0,
                                     buy_off=4, buy_close=140.0)
        # On the fill bar (11:01) override the SHORT-leg open so open != close.
        mask = ((df["_time"] == "11:01:00")
                & (df["option_type"] == "CE")
                & (df["strike"] == 24600.0))   # ATM+2 short
        df.loc[mask, "open"] = 160.0           # close stays 150.0
        trade = run_one_day(df, _ctx())
        assert trade.exit_reason == "TP"
        assert trade.exit_time == "11:01"
        # Short-leg exit_price recorded by the engine should be the OPEN (160), not close (150).
        short = next(l for l in trade.legs.values() if l.side == "SELL")
        assert short.exit_price == pytest.approx(160.0)

    def test_breach_disabled_falls_to_sl(self):
        """Same scenario as above but wall_breach_enabled=False -> SL fires."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=200.0,
                                     buy_off=4, buy_close=140.0,
                                     spot=24650.0)
        ctx = DayContext(
            date=date.fromisoformat("2025-03-10"),
            min_conditions_to_enter=1,
            lots=1, force_exit_time="14:30:00",
            tp_inr=1000.0, sl_inr=1000.0,
            wall_breach_enabled=False,
        )
        trade = run_one_day(df, ctx)
        assert trade.exit_reason == "SL"


class TestTrailingSl:
    """Profit-lock ("trailing") SL step on a CE wall (offset 3).
    Entry credit = 30 pts (sell ATM+2 @170, buy ATM+4 @140);
    live_inr/lot = (30 + buy_t - sell_t) * 65."""

    def _trail_ctx(self, trail_arm_inr=800.0, trail_lock_inr=200.0,
                   tp_inr=1000.0, sl_inr=2000.0, lots=1):
        return DayContext(
            date=date.fromisoformat("2025-03-10"),
            min_conditions_to_enter=1, lots=lots,
            force_exit_time="14:30:00",
            tp_inr=tp_inr, sl_inr=sl_inr,
            trail_arm_inr=trail_arm_inr, trail_lock_inr=trail_lock_inr,
            wall_breach_enabled=False,
        )

    def test_arms_then_locks_profit_tsl(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # 11:00: live = 30 + 140 - 156 = 14 pts -> 910/lot (>= arm 800, < TP 1000): arms, no exit.
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=156.0,
                                     buy_off=4, buy_close=140.0)
        # 11:05: live = 30 + 140 - 168 = 2 pts -> 130/lot (<= lock 200): TSL fires.
        df = _inject_intraday_minute(df, "11:05:00", "CE", atm=24500,
                                     sell_off=2, sell_close=168.0,
                                     buy_off=4, buy_close=140.0)
        trade = run_one_day(df, self._trail_ctx())
        assert trade.skip_reason is None
        assert trade.exit_reason == "TSL"
        assert trade.exit_signal_time == "11:05"
        assert trade.exit_time == "11:06"          # T+1 fill
        assert trade.pnl_inr == pytest.approx(2.0 * LOT_SIZE_NIFTY)

    def test_not_armed_keeps_hard_sl(self):
        """Profit never reaches the arm threshold -> the pre-arm hard SL fires
        and is still labelled SL (not TSL)."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # 11:00: live = 30 + 140 - 200 = -30 pts -> -1950/lot. With sl_inr=1000
        # -> hard SL (-1000) breached; arm (800) never reached.
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=200.0,
                                     buy_off=4, buy_close=140.0)
        trade = run_one_day(df, self._trail_ctx(sl_inr=1000.0))
        assert trade.skip_reason is None
        assert trade.exit_reason == "SL"
        assert trade.exit_signal_time == "11:00"
        assert trade.exit_time == "11:01"
        assert trade.pnl_inr == pytest.approx(-30.0 * LOT_SIZE_NIFTY)

    def test_tp_still_caps_above_lock(self):
        """A bar that clears both the arm and the TP exits as TP (TP > lock)."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # 11:00: live = 30 + 140 - 150 = 20 pts -> 1300/lot (>= TP 1000 and arm 800).
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=150.0,
                                     buy_off=4, buy_close=140.0)
        trade = run_one_day(df, self._trail_ctx())
        assert trade.exit_reason == "TP"
        assert trade.exit_signal_time == "11:00"
        assert trade.pnl_inr == pytest.approx(20.0 * LOT_SIZE_NIFTY)

    def test_disabled_no_lock_falls_to_time(self):
        """Same +910 -> +130 retrace as the TSL test, but trail_arm_inr=0.
        With identical TP/SL the +130 retrace is NOT an exit -> TIME at 14:30,
        proving the step is opt-in."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=156.0,
                                     buy_off=4, buy_close=140.0)
        df = _inject_intraday_minute(df, "11:05:00", "CE", atm=24500,
                                     sell_off=2, sell_close=168.0,
                                     buy_off=4, buy_close=140.0)
        trade = run_one_day(df, self._trail_ctx(trail_arm_inr=0.0))
        assert trade.skip_reason is None
        assert trade.exit_reason == "TIME"
        assert trade.exit_time == "14:30"

    def test_tsl_fill_uses_t_plus_one_open_not_close(self):
        """No look-ahead: the TSL fill must read the T+1 OPEN, not T's close."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # 11:00 arms (910/lot); 11:05 hits the lock (130/lot <= 200).
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=156.0,
                                     buy_off=4, buy_close=140.0)
        df = _inject_intraday_minute(df, "11:05:00", "CE", atm=24500,
                                     sell_off=2, sell_close=168.0,
                                     buy_off=4, buy_close=140.0)
        # On the fill bar (11:06) push the SHORT-leg open away from its close.
        mask = ((df["_time"] == "11:06:00")
                & (df["option_type"] == "CE")
                & (df["strike"] == 24600.0))   # ATM+2 short
        df.loc[mask, "open"] = 175.0           # close stays 168.0
        trade = run_one_day(df, self._trail_ctx())
        assert trade.exit_reason == "TSL"
        assert trade.exit_time == "11:06"
        short = next(l for l in trade.legs.values() if l.side == "SELL")
        # Engine must record the OPEN (175), proving it filled on T+1, not T's close.
        assert short.exit_price == pytest.approx(175.0)

    def test_arm_is_not_retroactive(self):
        """No look-ahead: a dip that occurs BEFORE the arm bar must not be
        treated as armed. If arming peeked forward, the 11:05 dip would fire
        TSL; it must not -- TSL can only fire at 11:15, after the 11:10 arm."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        # 11:00 & 11:05: live 130/lot (<= lock 200) but BELOW arm 800 -> no arm, no exit.
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=168.0,
                                     buy_off=4, buy_close=140.0)
        df = _inject_intraday_minute(df, "11:05:00", "CE", atm=24500,
                                     sell_off=2, sell_close=168.0,
                                     buy_off=4, buy_close=140.0)
        # 11:10: live 910/lot -> ARMS (still > lock, no exit).
        df = _inject_intraday_minute(df, "11:10:00", "CE", atm=24500,
                                     sell_off=2, sell_close=156.0,
                                     buy_off=4, buy_close=140.0)
        # 11:15: live 130/lot -> now armed -> TSL.
        df = _inject_intraday_minute(df, "11:15:00", "CE", atm=24500,
                                     sell_off=2, sell_close=168.0,
                                     buy_off=4, buy_close=140.0)
        trade = run_one_day(df, self._trail_ctx())
        assert trade.exit_reason == "TSL"
        assert trade.exit_signal_time == "11:15"   # NOT 11:00 / 11:05
        assert trade.exit_time == "11:16"


class TestConfigurableEntryTime:
    def test_entry_at_1015(self):
        """Signal time = 10:15 -> conditions evaluated on 10:15 bar; fill at 10:16."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3,
                             entry_time_str="10:15:00",
                             wall_price_1030_delta=-2.0,
                             wall_oi_1030_delta=5000)
        ctx = DayContext(
            date=date.fromisoformat("2025-03-10"),
            min_conditions_to_enter=1,
            lots=1, force_exit_time="14:30:00",
            entry_time="10:15:00",
        )
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        assert trade.signal_time == "10:15"   # conditions checked here
        assert trade.entry_time == "10:16"    # spread filled here (T+1)
        # cond_price/cond_oi reflect the 10:15 bar deltas (down/up).
        assert trade.cond_price_le is True
        assert trade.cond_oi_ge is True
        assert trade.exit_reason == "TIME"
        assert trade.exit_time == "14:30"

    def test_entry_fill_uses_t_plus_one_open_not_close(self):
        """Entry fills at T+1 OPEN, not T+1 close. Force open != close on the
        fill bar and confirm the engine reads OPEN."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3,
                             entry_time_str="10:15:00")
        # ATM=24500; CE wall -> SELL ATM+2=24600, BUY ATM+4=24700.
        # At the fill bar (10:16) the short leg's default close is 170 (=200-30).
        # Override its OPEN to 175 (different from close) so we can verify the
        # engine picks the open price.
        fill_mask = ((df["_time"] == "10:16:00")
                     & (df["option_type"] == "CE")
                     & (df["strike"] == 24600.0))
        df.loc[fill_mask, "open"] = 175.0   # close stays 170
        ctx = DayContext(
            date=date.fromisoformat("2025-03-10"),
            min_conditions_to_enter=1,
            lots=1, force_exit_time="14:30:00",
            entry_time="10:15:00",
        )
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        ce_short = next(l for l in trade.legs.values() if l.side == "SELL")
        assert ce_short.strike == 24600.0
        # Engine must use the OPEN (175), not the close (170).
        assert ce_short.entry_price == pytest.approx(175.0)

    def test_no_entry_bar_when_time_missing(self):
        """If the configured entry_time has no bar, skip with 'no_entry_bar'."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3,
                             entry_time_str="10:30:00")
        ctx = DayContext(
            date=date.fromisoformat("2025-03-10"),
            min_conditions_to_enter=1,
            lots=1, force_exit_time="14:30:00",
            entry_time="10:15:00",  # no 10:15 bar exists in the frame
        )
        trade = run_one_day(df, ctx)
        assert trade.skip_reason == "no_entry_bar"


class TestLotsAndForceExit:
    def test_pnl_scales_with_lots(self):
        """4 lots -> pnl_inr is 4x the 1-lot value on the same scenario."""
        df1 = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        df4 = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        t1 = run_one_day(df1, _ctx(lots=1))
        t4 = run_one_day(df4, _ctx(lots=4))
        assert t1.skip_reason is None and t4.skip_reason is None
        assert t1.pnl_pts == pytest.approx(t4.pnl_pts)
        assert t4.pnl_inr == pytest.approx(t1.pnl_inr * 4)
        assert t4.net_credit_inr == pytest.approx(t1.net_credit_inr * 4)
        # LegFill.lots reflects the configured lots.
        for leg in t4.legs.values():
            assert leg.lots == 4

    def test_tp_threshold_is_per_lot(self):
        """Live P&L = 20 pts * 65 = 1300/lot. With tp_inr=1000/lot and 4 lots,
        threshold = 4000; live (1300 * 4 = 5200) easily exceeds it."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=150.0,
                                     buy_off=4, buy_close=140.0)
        ctx = _ctx(lots=4)  # tp_inr default 1000/lot, threshold 4000
        trade = run_one_day(df, ctx)
        assert trade.exit_reason == "TP"
        # 20 pts * 65 contracts/lot * 4 lots = 5200
        assert trade.pnl_inr == pytest.approx(20.0 * LOT_SIZE_NIFTY * 4)

    def test_tp_threshold_requires_aggregate_to_clear(self):
        """tp_inr=2000/lot * 4 lots = 8000 threshold. Live 5200 < 8000 -> no TP."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        df = _inject_intraday_minute(df, "11:00:00", "CE", atm=24500,
                                     sell_off=2, sell_close=150.0,
                                     buy_off=4, buy_close=140.0)
        ctx = DayContext(
            date=date.fromisoformat("2025-03-10"),
            min_conditions_to_enter=1,
            lots=4, force_exit_time="14:30:00",
            tp_inr=2000.0, sl_inr=2000.0,
        )
        trade = run_one_day(df, ctx)
        assert trade.exit_reason == "TIME"  # threshold not hit

    def test_custom_force_exit_time_1500(self):
        """force_exit_time=15:00 -> exit bar is at 15:00, not 14:30."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3,
                             force_exit_time_str="15:00:00")
        ctx = _ctx(force_exit_time="15:00:00")
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        assert trade.exit_reason == "TIME"
        assert trade.exit_time == "15:00"

    def test_configurable_buy_offset(self):
        """sell_offset_abs=2, buy_offset_abs=6 -> CE wall short ATM+2, long ATM+6."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        ctx = DayContext(
            date=date.fromisoformat("2025-03-10"),
            min_conditions_to_enter=1, lots=1,
            force_exit_time="14:30:00",
            sell_offset_abs=2, buy_offset_abs=6,
        )
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        offs = {l.strike_offset for l in trade.legs.values()}
        assert offs == {2, 6}  # not {2, 4}

    def test_configurable_buy_offset_pe(self):
        """PE wall mirrors to negative offsets."""
        df = _full_day_frame(opt_for_wall="PE", wall_offset=-3)
        ctx = DayContext(
            date=date.fromisoformat("2025-03-10"),
            min_conditions_to_enter=1, lots=1,
            force_exit_time="14:30:00",
            sell_offset_abs=2, buy_offset_abs=6,
        )
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        offs = {l.strike_offset for l in trade.legs.values()}
        assert offs == {-2, -6}

    def test_no_force_exit_bar(self):
        """Force exit configured at 15:00 but frame only has 14:30 bar -> skip."""
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3,
                             force_exit_time_str="14:30:00")
        ctx = _ctx(force_exit_time="15:00:00")
        trade = run_one_day(df, ctx)
        assert trade.skip_reason == "no_force_exit_bar"


class TestRunBacktest:
    def test_three_days(self):
        frames = [
            _full_day_frame(date_str="2025-03-10", opt_for_wall="CE", wall_offset=3),
            _full_day_frame(date_str="2025-03-11", opt_for_wall="PE", wall_offset=-3),
            _full_day_frame(date_str="2025-03-12", opt_for_wall="CE", wall_offset=3),
        ]
        df = pd.concat(frames, ignore_index=True)
        cfg = {
            "entry": {"min_conditions_to_enter": 2},
            "exit": {"force_exit_time": "14:30:00"},
            "sizing": {"reference_capital": 100000},
            "backtest_start": "2025-03-10",
            "backtest_end":   "2025-03-12",
        }
        out = run_backtest(df, cfg)
        trades = out["trades"]
        assert len(trades) == 3
        assert all(t.skip_reason is None for t in trades)
        # running equity is cumulative
        capital = 100000
        running = capital
        for t in trades:
            running += t.pnl_inr
            assert t.running_equity_inr == pytest.approx(running)


class TestReporting:
    def test_summary_counts(self):
        frames = [
            _full_day_frame(date_str="2025-03-10", opt_for_wall="CE", wall_offset=3),
            _full_day_frame(date_str="2025-03-11", opt_for_wall="PE", wall_offset=-3),
            _full_day_frame(
                date_str="2025-03-12", opt_for_wall="CE", wall_offset=3,
                wall_price_1030_delta=+5.0, wall_oi_1030_delta=-5000,
            ),
        ]
        df = pd.concat(frames, ignore_index=True)
        cfg = {
            "entry": {"min_conditions_to_enter": 2},
            "exit": {"force_exit_time": "14:30:00"},
            "sizing": {"reference_capital": 100000},
            "backtest_start": "2025-03-10",
            "backtest_end":   "2025-03-12",
        }
        out = run_backtest(df, cfg)
        s = summarize_metrics(out["trades"], 100000)
        assert s["total_days_processed"] == 3
        assert s["trades_placed"] == 2
        assert s["trades_skipped"] == 1
        assert s["ce_trades"] + s["pe_trades"] == 2

    def test_exit_reason_breakdown(self):
        """exit_reason_counts/_pnl partition the placed trades and their P&L."""
        frames = [
            _full_day_frame(date_str="2025-03-10", opt_for_wall="CE", wall_offset=3),
            _full_day_frame(date_str="2025-03-11", opt_for_wall="PE", wall_offset=-3),
        ]
        df = pd.concat(frames, ignore_index=True)
        cfg = {
            "entry": {"min_conditions_to_enter": 2},
            "exit": {"force_exit_time": "14:30:00"},
            "sizing": {"reference_capital": 100000},
            "backtest_start": "2025-03-10",
            "backtest_end":   "2025-03-11",
        }
        out = run_backtest(df, cfg)
        placed = [t for t in out["trades"] if t.skip_reason is None]
        s = summarize_metrics(out["trades"], 100000)
        # counts partition the placed trades
        assert sum(s["exit_reason_counts"].values()) == len(placed)
        # per-reason P&L sums back to the total P&L
        assert sum(s["exit_reason_pnl"].values()) == pytest.approx(s["total_pnl_inr"])
        # these days hit force-exit -> bucketed under TIME (rendered as EOD)
        assert s["exit_reason_counts"].get("TIME", 0) == len(placed)

    def test_trades_dataframe_has_leg_columns(self):
        df = _full_day_frame(opt_for_wall="CE", wall_offset=3)
        cfg = {
            "entry": {"min_conditions_to_enter": 2},
            "exit": {"force_exit_time": "14:30:00"},
            "sizing": {"reference_capital": 100000},
            "backtest_start": "2025-03-10",
            "backtest_end":   "2025-03-10",
        }
        out = run_backtest(df, cfg)
        tdf = trades_to_dataframe(out["trades"])
        assert "ce_short_strike" in tdf.columns
        assert "ce_long_strike" in tdf.columns
        assert "ce_short_entry" in tdf.columns
        assert "ce_long_exit" in tdf.columns
