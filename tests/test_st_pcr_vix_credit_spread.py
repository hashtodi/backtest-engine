"""Tests for the SuperTrend + PCR + VIX CREDIT-spread engine.

Covers the load-bearing custom logic:
  * full-chain PCR and the previous-candle "last PCR" pick,
  * the 15-min signal gating (SuperTrend flip + PCR + VIX),
  * the credit-spread structure (bull-put / bear-call) with ATM from the
    ENTRY-bar OPEN spot and round-half-up rounding,
  * the TP (20% of credit) / SL (25 pts) / EOD exits with SAME-bar-close fills,
    the absence of any SuperTrend-reversal exit, and SL > TP priority,
  * the credit band skip and the expiry-day roll.
"""
import math

import pandas as pd
import pytest

from config import NIFTY_WEEKLY_EXPIRY_DATES
from engine.st_pcr_vix_credit_spread_backtest import (
    StPcrVixDayContext,
    _candle_close_pcr,
    _expiry_code_for,
    build_day_entry_signals,
    pcr_by_minute,
    run_one_day,
)

ATM = 22000.0
DATE = "2025-01-02"


# --------------------------------------------------------------------------- #
#  PCR (full-chain, previous-candle last value)                               #
# --------------------------------------------------------------------------- #

def _opt(t, otype, strike, o, c, oi):
    return {"_time": t, "option_type": otype, "strike": float(strike),
            "open": float(o), "close": float(c), "oi": int(oi)}


class TestPcr:
    def test_full_chain_ratio(self):
        rows = [
            _opt("11:14:00", "PE", 21900, 1, 1, 300),
            _opt("11:14:00", "PE", 21800, 1, 1, 200),   # PE total 500
            _opt("11:14:00", "CE", 22100, 1, 1, 100),
            _opt("11:14:00", "CE", 22200, 1, 1, 150),   # CE total 250
        ]
        pcr = pcr_by_minute(pd.DataFrame(rows))
        assert pcr["11:14:00"] == pytest.approx(500 / 250)

    def test_candle_close_pcr_picks_last_minute_in_window(self):
        pcr_min = {"11:00:00": 1.0, "11:09:00": 1.2, "11:14:00": 1.5,
                   "11:15:00": 9.9}  # 11:15 is the NEXT candle -> excluded
        # candle P starts 11:00, window 11:00..11:14 -> last value is 11:14.
        val = _candle_close_pcr(pcr_min, pd.Timestamp("2025-01-02 11:00"))
        assert val == pytest.approx(1.5)

    def test_candle_close_pcr_nan_when_window_empty(self):
        assert math.isnan(_candle_close_pcr({"09:30:00": 1.1},
                                            pd.Timestamp("2025-01-02 11:00")))


# --------------------------------------------------------------------------- #
#  Signal gating (build_day_entry_signals)                                    #
# --------------------------------------------------------------------------- #

def _params(**over):
    p = dict(window_start="09:46:00", window_end="14:15:00",
             use_pcr_filter=True, use_vix_filter=True,
             pcr_bull_min=1.1, pcr_bear_max=0.9,
             dvix_bull_max=0.3, dvix_bear_min=-0.3)
    p.update(over)
    return p


def _candles(rows):
    """rows: list of dicts each with start (HH:MM) + the per-candle fields."""
    idx = [pd.Timestamp(f"2025-01-02 {r.pop('start')}") for r in rows]
    df = pd.DataFrame(rows, index=idx)
    df["_date"] = DATE
    return df


def _bull_row(**over):
    r = dict(start="11:15", open=100.0, high=101.0, low=99.0, close=100.0,
             dir=-1.0, vix_close=13.0, prev_dir=1.0, dvix=-0.1,
             is_contiguous=True)
    r.update(over)
    return r


def _bear_row(**over):
    r = dict(start="11:15", open=100.0, high=101.0, low=99.0, close=98.0,
             dir=1.0, vix_close=13.0, prev_dir=-1.0, dvix=0.1,
             is_contiguous=True)
    r.update(over)
    return r


class TestSignalGating:
    def test_bull_signal_fires(self):
        sig = build_day_entry_signals(_candles([_bull_row()]),
                                      {"11:14:00": 1.5}, _params())
        assert "11:30:00" in sig
        kind, pcr_ref, dvix, cndl = sig["11:30:00"]
        assert kind == "LONG" and pcr_ref == pytest.approx(1.5) and cndl == "11:15"

    def test_bear_signal_fires(self):
        sig = build_day_entry_signals(_candles([_bear_row()]),
                                      {"11:14:00": 0.7}, _params())
        assert sig["11:30:00"][0] == "SHORT"

    def test_no_flip_no_signal(self):
        # prev_dir already bullish -> not a fresh bull flip.
        sig = build_day_entry_signals(_candles([_bull_row(prev_dir=-1.0)]),
                                      {"11:14:00": 1.5}, _params())
        assert sig == {}

    def test_pcr_gate(self):
        sig = build_day_entry_signals(_candles([_bull_row()]),
                                      {"11:14:00": 1.0}, _params())  # < 1.1
        assert sig == {}

    def test_pcr_filter_off_ignores_pcr(self):
        # A failing PCR (1.0 < 1.1) still fires when use_pcr_filter is off.
        sig = build_day_entry_signals(_candles([_bull_row()]),
                                      {"11:14:00": 1.0},
                                      _params(use_pcr_filter=False))
        assert sig["11:30:00"][0] == "LONG"

    def test_flip_alone_fires_with_both_filters_off(self):
        # No PCR data at all + VIX off + PCR off -> the flip alone triggers.
        sig = build_day_entry_signals(_candles([_bull_row(dvix=9.0)]), {},
                                      _params(use_pcr_filter=False,
                                              use_vix_filter=False))
        assert sig["11:30:00"][0] == "LONG"

    def test_vix_gate_blocks_when_rising_too_fast(self):
        sig = build_day_entry_signals(_candles([_bull_row(dvix=0.5)]),
                                      {"11:14:00": 1.5}, _params())  # > 0.3
        assert sig == {}

    def test_vix_filter_off_ignores_dvix(self):
        sig = build_day_entry_signals(_candles([_bull_row(dvix=5.0)]),
                                      {"11:14:00": 1.5},
                                      _params(use_vix_filter=False))
        assert sig["11:30:00"][0] == "LONG"

    def test_non_contiguous_prev_candle_skipped(self):
        sig = build_day_entry_signals(_candles([_bull_row(is_contiguous=False)]),
                                      {"11:14:00": 1.5}, _params())
        assert sig == {}

    def test_entry_window_excludes_early_0945_entry(self):
        # signal candle 09:30 -> entry 09:45, before the 09:46 window start.
        r = _bull_row(start="09:30")
        sig = build_day_entry_signals(_candles([r]), {"09:29:00": 1.5}, _params())
        assert sig == {}


# --------------------------------------------------------------------------- #
#  Structure + exits (run_one_day)                                            #
# --------------------------------------------------------------------------- #

def _spread_day(path, otype, sell_strike, buy_strike):
    """`path` maps minute -> (sell_open, sell_close, buy_open, buy_close)."""
    rows = []
    for t, (so, sc, bo, bc) in path.items():
        rows.append(_opt(t, otype, sell_strike, so, sc, 0))
        rows.append(_opt(t, otype, buy_strike, bo, bc, 0))
    return pd.DataFrame(rows)


def _ctx(**over):
    kw = dict(date=DATE, expiry_code=1, lots=1, sell_offset_abs=2,
              buy_offset_abs=4, tp_credit_frac=0.20,
              sl_pts=25.0, square_off_time="15:15",
              max_trades_per_day=0, strike_step=50, lot_size=65)
    kw.update(over)
    return StPcrVixDayContext(**kw)


def _entry(minute, kind):
    return {minute: (kind, 1.5, -0.1, minute[:5])}


def _spot_map(path, spot=ATM):
    return {t: spot for t in path}


class TestLongBullPut:
    # LONG -> bull-put: SELL PE 21900 (ATM-100), BUY PE 21800 (ATM-200).
    def test_structure_and_take_profit_same_bar_close(self):
        path = {
            "10:00:00": (100, 95, 20, 18),   # entry at OPEN: credit = 100-20 = 80
            "10:01:00": (70, 70, 10, 10),    # spread_val 60 -> profit 20 >= 16 (TP)
            "15:15:00": (40, 40, 12, 12),
        }
        opts = _spread_day(path, "PE", 21900, 21800)
        trades, skips = run_one_day(opts, _entry("10:00:00", "LONG"),
                                    _spot_map(path), _spot_map(path), _ctx())
        assert sum(skips.values()) == 0 and len(trades) == 1
        tr = trades[0]
        assert tr.signal == "LONG" and tr.spread == "BULL_PUT"
        assert tr.atm_strike == 22000.0
        assert tr.legs["sell"].option_type == "PE" and tr.legs["sell"].strike == 21900.0
        assert tr.legs["buy"].strike == 21800.0
        # entry at OPEN, not close:
        assert tr.legs["sell"].entry_price == 100.0 and tr.legs["buy"].entry_price == 20.0
        assert tr.net_credit_pts == pytest.approx(80.0)
        assert tr.tp_threshold_pts == pytest.approx(16.0)   # 20% of 80
        assert tr.exit_reason == "TP"
        assert tr.entry_time == "10:00" and tr.exit_time == "10:01"
        # pnl = (sell_entry+buy_exit) - (sell_exit+buy_entry) = (100+10)-(70+20)=20
        assert tr.pnl_pts == pytest.approx(20.0)
        assert tr.pnl_inr == pytest.approx(20.0 * 65)   # no brokerage/costs

    def test_stop_loss_25_pts(self):
        path = {
            "10:00:00": (100, 95, 20, 18),   # credit 80
            "10:01:00": (120, 120, 10, 10),  # spread_val 110 -> profit -30 <= -25 SL
            "15:15:00": (40, 40, 12, 12),
        }
        opts = _spread_day(path, "PE", 21900, 21800)
        trades, _ = run_one_day(opts, _entry("10:00:00", "LONG"),
                                _spot_map(path), _spot_map(path), _ctx())
        tr = trades[0]
        assert tr.exit_reason == "SL"
        assert tr.pnl_pts == pytest.approx(-30.0)
        assert tr.pnl_inr == pytest.approx(-30.0 * 65)


class TestShortBearCall:
    # SHORT -> bear-call: SELL CE 22100 (ATM+100), BUY CE 22200 (ATM+200).
    def test_structure_and_eod(self):
        path = {
            "12:00:00": (90, 88, 15, 14),    # credit 75
            "12:01:00": (85, 85, 14, 14),    # profit 4 < 15 (TP=20%*75), no exit
            "15:15:00": (80, 80, 12, 12),    # EOD close: spread 68 -> profit 7
        }
        opts = _spread_day(path, "CE", 22100, 22200)
        trades, _ = run_one_day(opts, _entry("12:00:00", "SHORT"),
                                _spot_map(path), _spot_map(path), _ctx())
        tr = trades[0]
        assert tr.spread == "BEAR_CALL" and tr.signal == "SHORT"
        assert tr.legs["sell"].option_type == "CE" and tr.legs["sell"].strike == 22100.0
        assert tr.legs["buy"].strike == 22200.0
        assert tr.exit_reason == "EOD" and tr.exit_time == "15:15"
        # pnl = (90+12) - (80+15) = 7
        assert tr.pnl_pts == pytest.approx(7.0)


class TestExitDiscipline:
    def test_no_supertrend_reversal_exit_reason_ever(self):
        # Whatever happens, the only exit reasons this engine emits are TP/SL/EOD.
        path = {
            "10:00:00": (100, 95, 20, 18),
            "10:01:00": (96, 96, 19, 19),
            "10:02:00": (70, 70, 10, 10),    # TP
            "15:15:00": (40, 40, 12, 12),
        }
        opts = _spread_day(path, "PE", 21900, 21800)
        trades, _ = run_one_day(opts, _entry("10:00:00", "LONG"),
                                _spot_map(path), _spot_map(path), _ctx())
        assert all(t.exit_reason in {"TP", "SL", "EOD"} for t in trades)
        assert trades[0].exit_reason != "ST_REVERSE"

    def test_sl_has_priority_over_tp_same_bar(self):
        # A bar that is both <= -SL and would also be a (huge) TP -> SL wins.
        path = {
            "10:00:00": (100, 95, 20, 18),   # credit 80
            "10:01:00": (130, 130, 5, 5),    # spread 125 -> profit -45 <= -25 SL
            "15:15:00": (40, 40, 12, 12),
        }
        opts = _spread_day(path, "PE", 21900, 21800)
        trades, _ = run_one_day(opts, _entry("10:00:00", "LONG"),
                                _spot_map(path), _spot_map(path), _ctx())
        assert trades[0].exit_reason == "SL"

    def test_tp_threshold_is_20pct_of_credit(self):
        # TP threshold = 0.20 * credit; fires when realized profit reaches it.
        path = {
            "10:00:00": (100, 95, 20, 18),   # credit 80 -> TP threshold 16 pts profit
            "10:01:00": (90, 90, 14, 14),    # profit 80-76=4 < 16, no exit
            "10:02:00": (70, 70, 10, 10),    # spread 60 -> profit 20 >= 16 TP
            "15:15:00": (40, 40, 12, 12),
        }
        opts = _spread_day(path, "PE", 21900, 21800)
        trades, _ = run_one_day(opts, _entry("10:00:00", "LONG"),
                                _spot_map(path), _spot_map(path), _ctx())
        tr = trades[0]
        assert tr.tp_threshold_pts == pytest.approx(16.0)   # 0.20 * 80
        assert tr.exit_reason == "TP" and tr.exit_time == "10:02"


class TestCreditBand:
    def test_nonpositive_credit_skipped(self):
        # sell open <= buy open -> not a credit -> skip.
        path = {
            "10:00:00": (20, 20, 30, 30),
            "15:15:00": (10, 10, 12, 12),
        }
        opts = _spread_day(path, "PE", 21900, 21800)
        trades, skips = run_one_day(opts, _entry("10:00:00", "LONG"),
                                    _spot_map(path), _spot_map(path), _ctx())
        assert trades == [] and skips["credit_out_of_band"] == 1

    def test_max_credit_band_skipped(self):
        path = {
            "10:00:00": (100, 95, 10, 9),    # credit 90 > max 50
            "15:15:00": (10, 10, 12, 12),
        }
        opts = _spread_day(path, "PE", 21900, 21800)
        trades, skips = run_one_day(opts, _entry("10:00:00", "LONG"),
                                    _spot_map(path), _spot_map(path),
                                    _ctx(max_credit_pts=50.0))
        assert trades == [] and skips["credit_out_of_band"] == 1


class TestAtmRounding:
    def test_round_half_up_from_entry_open_spot(self):
        # spot 22030 -> 22030/50=440.6 -> floor(441.1)=441 -> 22050.
        path = {
            "10:00:00": (100, 95, 20, 18),
            "15:15:00": (40, 40, 12, 12),
        }
        opts = _spread_day(path, "PE", 21950, 21850)  # ATM 22050 -> -100/-200
        spot = {t: 22030.0 for t in path}
        trades, skips = run_one_day(opts, _entry("10:00:00", "LONG"),
                                    spot, spot, _ctx())
        assert sum(skips.values()) == 0
        assert trades[0].atm_strike == 22050.0


class TestExpiryRoll:
    def test_rolls_to_code_2_on_expiry_day(self):
        expiry = NIFTY_WEEKLY_EXPIRY_DATES[0].isoformat()
        assert _expiry_code_for(expiry, expiry_roll=True) == 2

    def test_code_1_on_non_expiry_day(self):
        # day after an expiry is not itself an expiry (weeklies are ~7 apart).
        from datetime import date, timedelta
        non_expiry = (NIFTY_WEEKLY_EXPIRY_DATES[0] + timedelta(days=1)).isoformat()
        assert _expiry_code_for(non_expiry, expiry_roll=True) == 1

    def test_roll_disabled_always_code_1(self):
        expiry = NIFTY_WEEKLY_EXPIRY_DATES[0].isoformat()
        assert _expiry_code_for(expiry, expiry_roll=False) == 1
