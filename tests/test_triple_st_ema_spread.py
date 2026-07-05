"""Tests for the Triple-SuperTrend + EMA9/21 credit-spread engine.

Covers the load-bearing custom logic:
  * the no-look-ahead HTF attachment (the 12:35 boundary case),
  * the regime + EMA-cross + window signal gating,
  * the credit-spread structure and the INR TP/SL/EOD exit execution with the
    entry-at-close / exit-next-min-open fill rules,
  * unlimited intraday re-entry, and the expiry-day roll.
"""
import pandas as pd
import pytest

import os

import numpy as np

from engine.triple_st_ema_spread_backtest import (
    TripleStEmaDayContext,
    _attach_htf_dir_rolling,
    _expiry_code_for,
    _finalize_signals,
    attach_last_completed,
    load_spot_1m,
    run_one_day,
)

SPOT_PATH = "data/spot/nifty/NIFTY_1m.parquet"


# --------------------------------------------------------------------------- #
#  HTF attachment — no look-ahead (the 12:35 boundary)                        #
# --------------------------------------------------------------------------- #

class TestAttachLastCompleted:
    """At the candle closing at 12:35 (parquet row 12:35), the freshly-closed
    5m (12:30-12:34) and 10m (12:25-12:34) bars are visible, the 3m used is
    12:30-12:32, and the HTF bars *starting* at 12:35 are NOT used."""

    def _target(self):
        return pd.to_datetime([
            "2025-01-02 12:34", "2025-01-02 12:35", "2025-01-02 12:36",
        ])

    def test_5m_new_bar_appears_at_1235_not_1234(self):
        # 5m bars start-labeled 12:25 (dir -1) and 12:30 (dir +1).
        htf = pd.Series([-1, 1], index=pd.to_datetime(
            ["2025-01-02 12:25", "2025-01-02 12:30"]))
        att = attach_last_completed(htf, self._target(), 5)
        # 12:34 still sees the 12:25-12:29 bar; the 12:30-12:34 bar only
        # becomes visible from 12:35 (lookahead_off).
        assert att.iloc[0] == -1      # row 12:34 -> 12:25 bar
        assert att.iloc[1] == 1       # row 12:35 -> 12:30-12:34 bar (new)
        assert att.iloc[2] == 1       # row 12:36 -> still the 12:30 bar

    def test_10m_new_bar_appears_at_1235(self):
        htf = pd.Series([-1, 1], index=pd.to_datetime(
            ["2025-01-02 12:15", "2025-01-02 12:25"]))
        att = attach_last_completed(htf, self._target(), 10)
        assert att.iloc[0] == -1      # 12:34 -> 12:15 bar
        assert att.iloc[1] == 1       # 12:35 -> 12:25-12:34 bar (new)

    def test_3m_freshest_is_1230_1232_at_1235(self):
        # 3m bars start-labeled 12:30 (dir +1, covers 12:30-12:32) and 12:33
        # (dir -1, covers 12:33-12:35).
        htf = pd.Series([1, -1], index=pd.to_datetime(
            ["2025-01-02 12:30", "2025-01-02 12:33"]))
        att = attach_last_completed(htf, self._target(), 3)
        assert att.iloc[1] == 1       # 12:35 -> 12:30-12:32 bar (12:33 not yet)
        assert att.iloc[2] == -1      # 12:36 -> 12:33-12:35 bar now visible


# --------------------------------------------------------------------------- #
#  Spot loader timezone (regression)                                          #
# --------------------------------------------------------------------------- #

class TestSpotLoaderTimezone:
    @pytest.mark.skipif(not os.path.exists(SPOT_PATH),
                        reason="spot parquet not available")
    def test_dt_is_wall_clock_not_utc(self):
        # Regression: `.values` on a tz-aware series converted dt to UTC,
        # shifting every bar by -5:30 and silently breaking the anchored HTF
        # resample (between_time / resample(offset=09:15)). dt must carry the
        # IST wall-clock time, matching the _time string.
        df = load_spot_1m(SPOT_PATH, "2026-05-01", "2026-05-05", warmup_days=1)
        if df.empty:
            pytest.skip("no spot rows in range")
        tod = pd.to_datetime(df["dt"]).dt.strftime("%H:%M:%S")
        assert (tod.values == df["_time"].values).all()
        assert df["_time"].min() >= "09:15:00"


# --------------------------------------------------------------------------- #
#  Rolling (trailing-window) SuperTrend                                       #
# --------------------------------------------------------------------------- #

class TestRollingSuperTrend:
    def _spot(self, n=80):
        dt = pd.date_range("2025-01-02 09:15", periods=n, freq="1min",
                           tz="Asia/Kolkata")
        # ramp up then down so SuperTrend flips between both directions
        base = np.concatenate([np.linspace(100, 160, n // 2),
                               np.linspace(160, 100, n - n // 2)])
        return pd.DataFrame({"dt": dt, "open": base, "high": base + 1.0,
                             "low": base - 1.0, "close": base})

    def test_updates_every_minute_both_directions(self):
        d = _attach_htf_dir_rolling(self._spot(), 5, 3.0, 12)
        assert len(d) == 80
        vals = set(np.unique(d[~np.isnan(d)]).tolist())
        assert vals == {-1.0, 1.0}      # both uptrend and downtrend appear

    def test_no_lookahead_truncation_invariance(self):
        # SuperTrend on a trailing window is causal: directions for bars < k must
        # not change when future bars (>= k) are removed.
        spot = self._spot(80)
        full = _attach_htf_dir_rolling(spot, 5, 3.0, 12)
        k = 70
        trunc = _attach_htf_dir_rolling(spot.iloc[:k].copy(), 5, 3.0, 12)
        a, b = full[:k], trunc
        mask = ~(np.isnan(a) | np.isnan(b))
        assert mask.sum() > 0
        assert np.array_equal(a[mask], b[mask])


# --------------------------------------------------------------------------- #
#  Signal gating (regime + cross + window)                                    #
# --------------------------------------------------------------------------- #

def _sig_df(times, d1, d2, d3, ef, es):
    return pd.DataFrame({
        "_time": times, "dir1": d1, "dir2": d2, "dir3": d3,
        "ema_fast": ef, "ema_slow": es,
    })


class TestFinalizeSignals:
    def test_long_signal_fires_only_on_fresh_cross_in_long_regime(self):
        out = _finalize_signals(_sig_df(
            ["09:31:00", "09:32:00", "09:33:00"],
            [-1, -1, -1], [-1, -1, -1], [-1, -1, -1],
            [9, 11, 12], [10, 10, 10]), "09:30", "14:45")
        assert list(out["long_sig"]) == [False, True, False]
        assert not out["short_sig"].any()

    def test_regime_mismatch_blocks_signal(self):
        # Same cross at row 1, but one timeframe disagrees -> regime NONE.
        out = _finalize_signals(_sig_df(
            ["09:31:00", "09:32:00", "09:33:00"],
            [-1, -1, -1], [-1, -1, -1], [-1, 1, -1],
            [9, 11, 12], [10, 10, 10]), "09:30", "14:45")
        assert not out["long_sig"].any()
        assert list(out["regime"]) == ["LONG", "NONE", "LONG"]

    def test_short_signal_in_short_regime(self):
        out = _finalize_signals(_sig_df(
            ["09:31:00", "09:32:00", "09:33:00"],
            [1, 1, 1], [1, 1, 1], [1, 1, 1],
            [11, 9, 8], [10, 10, 10]), "09:30", "14:45")
        assert list(out["short_sig"]) == [False, True, False]
        assert not out["long_sig"].any()

    def test_window_gate(self):
        out = _finalize_signals(_sig_df(
            ["09:20:00", "09:31:00", "14:50:00"],
            [-1, -1, -1], [-1, -1, -1], [-1, -1, -1],
            [9, 11, 12], [10, 10, 10]), "09:30", "14:45")
        assert list(out["in_window"]) == [False, True, False]


# --------------------------------------------------------------------------- #
#  Exit execution / structure (run_one_day)                                   #
# --------------------------------------------------------------------------- #

ATM = 22000.0


def _opt_row(t, otype, strike, o, c):
    return {"_time": t, "option_type": otype, "strike": float(strike),
            "open": float(o), "close": float(c)}


def _spread_day(path, otype, sell_strike, buy_strike):
    """`path` maps minute -> (sell_open, sell_close, buy_open, buy_close)."""
    rows = []
    for t, (so, sc, bo, bc) in path.items():
        rows.append(_opt_row(t, otype, sell_strike, so, sc))
        rows.append(_opt_row(t, otype, buy_strike, bo, bc))
    return pd.DataFrame(rows)


def _signals(sig_minute, kind, path):
    """One signal at `sig_minute`; every minute in `path` carries spot_close=ATM
    so the spot map is populated for exit reporting."""
    rows = []
    for t in path:
        rows.append({
            "_time": t, "_date": "2025-01-02", "in_window": True,
            "spot_close": ATM,
            "long_sig": (t == sig_minute and kind == "LONG"),
            "short_sig": (t == sig_minute and kind == "SHORT"),
        })
    return pd.DataFrame(rows)


def _ctx(**over):
    kw = dict(date="2025-01-02", expiry_code=1, lots=1, sell_offset_abs=2,
              buy_offset_abs=6, tp_inr=800.0, sl_inr=650.0,
              square_off_time="15:15", max_trades_per_day=0, strike_step=50,
              lot_size=65)
    kw.update(over)
    return TripleStEmaDayContext(**kw)


class TestLongBullPut:
    # LONG -> bull-put: SELL PE 21900 (ATM-100), BUY PE 21700 (ATM-300).
    def test_take_profit(self):
        path = {
            "09:35:00": (999, 100, 999, 20),  # entry uses CLOSE (credit 80), not open
            "09:36:00": (0, 85, 0, 18),       # live=(100+18)-(85+20)=13 -> 845 >= 800 TP
            "09:37:00": (83, 83, 17, 17),     # exit fills next-min OPEN
            "15:15:00": (40, 40, 12, 12),
        }
        opts = _spread_day(path, "PE", 21900, 21700)
        trades, skips = run_one_day(opts, _signals("09:35:00", "LONG", path), _ctx())
        assert sum(skips.values()) == 0 and len(trades) == 1
        tr = trades[0]
        assert tr.signal == "LONG" and tr.spread == "BULL_PUT" and tr.direction == "LONG"
        assert tr.atm_strike == 22000.0
        assert tr.legs["sell"].option_type == "PE" and tr.legs["sell"].strike == 21900.0
        assert tr.legs["buy"].strike == 21700.0
        assert tr.legs["sell"].entry_price == 100.0 and tr.legs["buy"].entry_price == 20.0
        assert tr.net_credit_pts == pytest.approx(80.0)
        assert tr.exit_reason == "TP"
        assert tr.entry_time == "09:35" and tr.exit_time == "09:37"
        # pnl_pts = (100+17) - (83+20) = 14
        assert tr.pnl_pts == pytest.approx(14.0)
        assert tr.pnl_inr == pytest.approx(14.0 * 65)

    def test_stop_loss(self):
        path = {
            "09:35:00": (999, 100, 999, 20),  # credit 80
            "09:36:00": (0, 115, 0, 22),      # live=(100+22)-(115+20)=-13 -> -845 <= -650 SL
            "09:37:00": (118, 118, 23, 23),   # exit next-min OPEN
            "15:15:00": (40, 40, 12, 12),
        }
        opts = _spread_day(path, "PE", 21900, 21700)
        trades, _ = run_one_day(opts, _signals("09:35:00", "LONG", path), _ctx())
        tr = trades[0]
        assert tr.exit_reason == "SL"
        assert tr.entry_time == "09:35" and tr.exit_time == "09:37"
        # pnl_pts = (100+23) - (118+20) = -15
        assert tr.pnl_pts == pytest.approx(-15.0)

    def test_end_of_day(self):
        path = {
            "09:35:00": (999, 100, 999, 20),  # credit 80
            "09:36:00": (0, 95, 0, 19),       # live=(100+19)-(95+20)=4 -> 260, no TP/SL
            "15:15:00": (0, 90, 0, 18),       # EOD close
        }
        opts = _spread_day(path, "PE", 21900, 21700)
        trades, _ = run_one_day(opts, _signals("09:35:00", "LONG", path), _ctx())
        tr = trades[0]
        assert tr.exit_reason == "EOD"
        assert tr.exit_time == "15:15"
        # pnl_pts = (100+18) - (90+20) = 8
        assert tr.pnl_pts == pytest.approx(8.0)

    def test_nonpositive_credit_skipped(self):
        path = {
            "09:35:00": (20, 20, 30, 30),     # sell 20 <= buy 30 -> credit -10
            "09:36:00": (0, 15, 0, 25),
            "15:15:00": (0, 10, 0, 20),
        }
        opts = _spread_day(path, "PE", 21900, 21700)
        trades, skips = run_one_day(opts, _signals("09:35:00", "LONG", path), _ctx())
        assert trades == [] and skips["nonpositive_credit"] == 1
        assert sum(skips.values()) == 1

    def test_buy_leg_missing_is_counted_separately(self):
        # Only the SELL leg (PE 21900) is listed; the far-OTM buy leg (PE 21700)
        # is absent -> skipped as buy_leg_missing, NOT as a non-positive credit.
        opts = pd.DataFrame([
            _opt_row("09:35:00", "PE", 21900, 100, 100),
            _opt_row("15:15:00", "PE", 21900, 90, 90),
        ])
        sigs = _signals("09:35:00", "LONG", ["09:35:00", "15:15:00"])
        trades, skips = run_one_day(opts, sigs, _ctx())
        assert trades == []
        assert skips["buy_leg_missing"] == 1
        assert skips["nonpositive_credit"] == 0
        assert sum(skips.values()) == 1

    def test_tp_inr_zero_disables_tp(self):
        path = {
            "09:35:00": (999, 100, 999, 20),
            "09:36:00": (0, 30, 0, 14),       # deep decay -> would TP if enabled
            "15:15:00": (0, 35, 0, 15),
        }
        opts = _spread_day(path, "PE", 21900, 21700)
        trades, _ = run_one_day(opts, _signals("09:35:00", "LONG", path),
                                _ctx(tp_inr=0.0))
        assert trades[0].exit_reason == "EOD"


class TestShortBearCall:
    # SHORT -> bear-call: SELL CE 22100 (ATM+100), BUY CE 22300 (ATM+300).
    def test_structure_and_eod(self):
        path = {
            "09:35:00": (999, 100, 999, 20),  # credit 80
            "15:15:00": (0, 80, 0, 16),       # EOD
        }
        opts = _spread_day(path, "CE", 22100, 22300)
        trades, _ = run_one_day(opts, _signals("09:35:00", "SHORT", path), _ctx())
        tr = trades[0]
        assert tr.signal == "SHORT" and tr.spread == "BEAR_CALL" and tr.direction == "SHORT"
        assert tr.legs["sell"].option_type == "CE" and tr.legs["sell"].strike == 22100.0
        assert tr.legs["sell"].strike_offset == 2
        assert tr.legs["buy"].strike == 22300.0 and tr.legs["buy"].strike_offset == 6
        assert tr.exit_reason == "EOD"
        # pnl_pts = (100+16) - (80+20) = 16
        assert tr.pnl_pts == pytest.approx(16.0)


class TestPositionManagement:
    def test_unlimited_reentry_same_day(self):
        path = {
            "09:35:00": (999, 100, 999, 20),  # entry 1
            "09:36:00": (0, 85, 0, 18),       # TP 1
            "09:37:00": (83, 83, 17, 17),     # exit 1 (next-min open)
            "09:40:00": (999, 100, 999, 20),  # entry 2
            "09:41:00": (0, 85, 0, 18),       # TP 2
            "09:42:00": (83, 83, 17, 17),     # exit 2
            "15:15:00": (40, 40, 12, 12),
        }
        opts = _spread_day(path, "PE", 21900, 21700)
        sigs = _signals("09:35:00", "LONG", path)
        # add a second LONG signal at 09:40
        sigs.loc[sigs["_time"] == "09:40:00", "long_sig"] = True
        trades, _ = run_one_day(opts, sigs, _ctx())
        assert len(trades) == 2
        assert all(t.exit_reason == "TP" for t in trades)
        assert trades[0].entry_time == "09:35" and trades[1].entry_time == "09:40"

    def test_signal_while_in_trade_is_ignored(self):
        path = {
            "09:35:00": (999, 100, 999, 20),  # entry
            "09:36:00": (0, 95, 0, 19),       # in-trade; a 2nd signal here is ignored
            "15:15:00": (0, 90, 0, 18),       # EOD
        }
        opts = _spread_day(path, "PE", 21900, 21700)
        sigs = _signals("09:35:00", "LONG", path)
        sigs.loc[sigs["_time"] == "09:36:00", "long_sig"] = True
        trades, _ = run_one_day(opts, sigs, _ctx())
        assert len(trades) == 1


# --------------------------------------------------------------------------- #
#  Expiry-day roll                                                            #
# --------------------------------------------------------------------------- #

class TestExpiryRoll:
    def test_rolls_to_code_2_on_expiry_day(self):
        # 2026-06-23 is a NIFTY weekly expiry (config.NIFTY_WEEKLY_EXPIRY_DATES).
        assert _expiry_code_for("2026-06-23", expiry_roll=True) == 2

    def test_nearest_weekly_on_non_expiry_day(self):
        assert _expiry_code_for("2026-06-22", expiry_roll=True) == 1

    def test_roll_disabled_always_code_1(self):
        assert _expiry_code_for("2026-06-23", expiry_roll=False) == 1
