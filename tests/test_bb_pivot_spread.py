"""Tests for the BB-Pivot credit-spread engine.

Covers the two pieces of custom logic that are NOT shared with the regime_rsi
template: the Bollinger+RSI+pivot signal gating, and the credit-value-based
TP/SL/EOD exit execution (with the next-minute-open fill rule).
"""
import pandas as pd
import pytest

from engine.bb_pivot_spread_backtest import (
    BbPivotDayContext,
    build_signal_bars,
    generate_signals,
    run_one_day,
)


# --------------------------------------------------------------------------- #
#  30-min bar construction                                                    #
# --------------------------------------------------------------------------- #

class TestBuildSignalBars:
    def test_true_ohlc_aggregation(self):
        # Two 30-min buckets (anchor 09:15) built from 1-min OHLC.
        data = [
            ("09:15:00", 100, 105, 99, 101),
            ("09:30:00", 101, 110, 100, 108),   # bucket 1 high = 110
            ("09:44:00", 108, 109, 95, 96),      # bucket 1 low = 95, close = 96
            ("09:45:00", 96, 97, 90, 92),        # bucket 2 open = 96
            ("10:14:00", 92, 120, 91, 118),      # bucket 2 high = 120, close = 118
        ]
        df = pd.DataFrame([
            {"_date": "2025-01-02", "_time": t, "open": o, "high": h,
             "low": l, "close": c} for (t, o, h, l, c) in data])
        bars = build_signal_bars(df, timeframe_min=30)
        assert list(bars["bar_start"]) == ["09:15:00", "09:45:00"]
        b1 = bars.iloc[0]
        assert (b1["open"], b1["high"], b1["low"], b1["close"]) == (100, 110, 95, 96)
        b2 = bars.iloc[1]
        assert (b2["open"], b2["high"], b2["low"], b2["close"]) == (96, 120, 90, 118)

    def test_snapshot_fallback_derives_ohlc(self):
        # Only a `spot` column -> high/low derived from the snapshots.
        df = pd.DataFrame([
            {"_date": "2025-01-02", "_time": "09:15:00", "spot": 100},
            {"_date": "2025-01-02", "_time": "09:30:00", "spot": 107},
            {"_date": "2025-01-02", "_time": "09:44:00", "spot": 103},
        ])
        b = build_signal_bars(df, timeframe_min=30).iloc[0]
        assert (b["open"], b["high"], b["low"], b["close"]) == (100, 107, 100, 103)


# --------------------------------------------------------------------------- #
#  Signal generation                                                          #
# --------------------------------------------------------------------------- #

def _bars(closes, highs, lows, bar_starts):
    return pd.DataFrame({
        "date": ["2025-01-02"] * len(closes),
        "bar_start": bar_starts,
        "close_minute": [bs[:2] + ":" + str(int(bs[3:5]) + 29).zfill(2) + ":00"
                         for bs in bar_starts],
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
    })


class TestGenerateSignals:
    # Four rising closes -> RSI = 100; last close hugs the upper band; a pivot
    # (R1 from the prior bar) sits 5 pts away -> all three gates pass.
    BASE_CLOSES = [21980.0, 21990.0, 22000.0, 22010.0]
    # Prior bar (index 2) H/L are chosen so its pivots straddle the last close
    # (22010) by ~2 pts -- near enough for the default tolerance, but no pivot
    # coincides with the close (which would make the gate un-falsifiable).
    BASE_HIGHS = [21985.0, 21995.0, 22004.0, 22015.0]
    BASE_LOWS = [21975.0, 21985.0, 21996.0, 22005.0]
    BAR_STARTS = ["09:15:00", "09:45:00", "10:15:00", "10:45:00"]

    def _gen(self, **over):
        bars = _bars(self.BASE_CLOSES, self.BASE_HIGHS, self.BASE_LOWS,
                     self.BAR_STARTS)
        kw = dict(bb_period=2, bb_std=2.0, rsi_length=2, rsi_upper=60,
                  rsi_lower=30, adx_period=2, adx_max=1000.0,
                  band_tol_pct=0.005, pivot_tol_pct=0.001,
                  pivot_tol_pts=15, timeframe_min=30,
                  window_start="09:45", window_end="14:00")
        kw.update(over)
        return generate_signals(bars, **kw)

    def test_bear_call_fires_when_all_gates_pass(self):
        out = self._gen()
        last = out.iloc[-1]
        assert last["upper_touch"]
        assert last["rsi"] > 60
        assert not pd.isna(last["adx"])      # ADX is actually computed
        assert last["pivot_ok"]
        assert last["bear_call_sig"]
        assert not last["bull_put_sig"]
        assert last["in_window"]

    def test_adx_gate_is_necessary(self):
        # The rising series trends strongly (ADX ~ 100); a low cap blocks entry.
        out = self._gen(adx_max=10.0)
        assert out.iloc[-1]["adx"] > 10
        assert not out.iloc[-1]["bear_call_sig"]

    def test_adx_zero_disables_filter(self):
        # adx_max <= 0 turns the filter off -> the other gates still pass.
        out = self._gen(adx_max=0.0)
        assert out.iloc[-1]["bear_call_sig"]

    def test_rsi_gate_is_necessary(self):
        # Require RSI > 200 (impossible) -> bear_call must not fire.
        out = self._gen(rsi_upper=200)
        assert not out.iloc[-1]["bear_call_sig"]

    def test_band_gate_is_necessary(self):
        # Tolerance 0 -> the close is not exactly on the band -> no touch.
        out = self._gen(band_tol_pct=0.0)
        assert not out.iloc[-1]["upper_touch"]
        assert not out.iloc[-1]["bear_call_sig"]

    def test_pivot_gate_is_necessary(self):
        # Zero tolerance -> the nearest pivot (5 pts away) is out of reach.
        out = self._gen(pivot_tol_pct=0.0, pivot_tol_pts=0.0)
        assert not out.iloc[-1]["pivot_ok"]
        assert not out.iloc[-1]["bear_call_sig"]

    def test_window_gate_excludes_late_bars(self):
        bars = _bars(self.BASE_CLOSES, self.BASE_HIGHS, self.BASE_LOWS,
                     ["12:45:00", "13:15:00", "13:45:00", "14:15:00"])
        out = generate_signals(bars, bb_period=2, rsi_length=2,
                               window_start="09:45", window_end="14:00")
        # Last bar's close_time is 14:45 -> outside the window.
        assert not out.iloc[-1]["in_window"]


# --------------------------------------------------------------------------- #
#  Exit execution (credit-value based)                                        #
# --------------------------------------------------------------------------- #

ATM = 22000.0


def _row(t, otype, offset, strike, spot, o, c):
    return {"_time": t, "option_type": otype, "strike_offset": offset,
            "moneyness": "ATM" if offset == 0 else "OTM", "strike": strike,
            "spot": spot, "open": o, "close": c}


def _bear_call_day(price_path):
    """Build a one-day option frame for a BEAR_CALL trade.

    `price_path` maps minute -> (sell_open, sell_close, buy_open, buy_close)
    for the +2 (sell, 22100) and +6 (buy, 22300) CE legs. An ATM CE row is
    added each minute so strike selection resolves.
    """
    rows = []
    for t, (so, sc, bo, bc) in price_path.items():
        rows.append(_row(t, "CE", 0, ATM, ATM, 0.0, 0.0))      # ATM anchor
        rows.append(_row(t, "CE", 2, 22100.0, ATM, so, sc))    # sell leg
        rows.append(_row(t, "CE", 6, 22300.0, ATM, bo, bc))    # buy leg
    return pd.DataFrame(rows)


def _signal_bars():
    # One in-window bar whose close minute is 09:44 -> entry fills AT 09:44.
    return pd.DataFrame([{
        "close_minute": "09:44:00", "in_window": True,
        "bull_put_sig": False, "bear_call_sig": True,
    }])


def _ctx(**over):
    kw = dict(date="2025-01-02", expiry_code=1, lots=1, sell_offset_abs=2,
              buy_offset_abs=6, tp_ratio=0.5, sl_ratio=1.5,
              square_off_time="15:15", max_trades_per_day=3)
    kw.update(over)
    return BbPivotDayContext(**kw)


class TestExitExecution:
    def test_take_profit(self):
        # Entry AT the 09:44 signal bar's CLOSE: credit = 100-20 = 80; TP
        # threshold = 0.5*80 = 40. value decays 45 (no) -> 36 (<=40, fire at
        # 09:46) -> fill 09:47 open.
        path = {
            # signal/entry minute (09:44): ATM + premiums from THIS bar's CLOSE
            # (100/20 -> credit 80), NOT the open (999); if the engine read the
            # open this trade would be skipped (credit 0).
            "09:44:00": (999, 100, 999, 20),
            "09:45:00": (0, 60, 0, 15),       # value 45 (> 40, no TP)
            "09:46:00": (0, 50, 0, 14),       # value 36 (<= 40) -> TP
            "09:47:00": (48, 48, 13, 13),     # exit fill (next-min open)
            "15:15:00": (40, 40, 12, 12),
        }
        trades, skipped = run_one_day(_bear_call_day(path), _signal_bars(), _ctx())
        assert skipped == 0 and len(trades) == 1
        tr = trades[0]
        assert tr.signal == "BEAR_CALL" and tr.direction == "SHORT"
        assert tr.legs["sell"].strike == 22100.0 and tr.legs["buy"].strike == 22300.0
        assert tr.legs["sell"].entry_price == 100.0 and tr.legs["buy"].entry_price == 20.0
        assert tr.net_credit_pts == pytest.approx(80.0)
        assert tr.exit_reason == "TP"
        assert tr.entry_time == "09:44" and tr.exit_time == "09:47"
        # pnl_pts = (sell_entry+buy_exit) - (sell_exit+buy_entry) = (100+13)-(48+20)
        assert tr.pnl_pts == pytest.approx(45.0)
        assert tr.pnl_inr == pytest.approx(45.0 * 65)

    def test_stop_loss(self):
        # credit = 80; SL threshold = 1.5*80 = 120. value widens to 130 -> SL.
        path = {
            "09:44:00": (999, 100, 999, 20),  # entry, credit 80
            "09:45:00": (0, 150, 0, 20),      # value 130 >= 120 -> SL
            "09:46:00": (160, 160, 22, 22),   # exit fill (next-min open)
            "15:15:00": (40, 40, 12, 12),
        }
        trades, _ = run_one_day(_bear_call_day(path), _signal_bars(), _ctx())
        tr = trades[0]
        assert tr.exit_reason == "SL"
        assert tr.entry_time == "09:44" and tr.exit_time == "09:46"
        # pnl_pts = (100+22) - (160+20) = -58
        assert tr.pnl_pts == pytest.approx(-58.0)

    def test_end_of_day(self):
        # value never crosses TP/SL -> square-off at 15:15 close.
        path = {
            "09:44:00": (999, 100, 999, 20),  # entry, credit 80
            "09:45:00": (0, 90, 0, 18),       # value 72 (between 40 and 120)
            "15:15:00": (0, 85, 0, 17),       # EOD close, value 68
        }
        trades, _ = run_one_day(_bear_call_day(path), _signal_bars(), _ctx())
        tr = trades[0]
        assert tr.exit_reason == "EOD"
        assert tr.entry_time == "09:44" and tr.exit_time == "15:15"
        # pnl_pts = (100+17) - (85+20) = 12  (closed at 15:15 CLOSE prices)
        assert tr.pnl_pts == pytest.approx(12.0)

    def test_nonpositive_credit_is_skipped(self):
        # sell close <= buy close at the entry minute -> not a credit spread.
        path = {
            "09:44:00": (20, 20, 30, 30),     # credit = -10
            "09:45:00": (0, 15, 0, 25),
            "15:15:00": (0, 10, 0, 20),
        }
        trades, skipped = run_one_day(_bear_call_day(path), _signal_bars(), _ctx())
        assert trades == [] and skipped == 1

    def test_tp_ratio_zero_disables_tp(self):
        # With tp_ratio=0 the deep-decay path no longer takes profit -> EOD.
        path = {
            "09:44:00": (999, 100, 999, 20),  # entry, credit 80
            "09:45:00": (0, 25, 0, 13),       # value 12 -> would TP if enabled
            "15:15:00": (0, 30, 0, 14),
        }
        trades, _ = run_one_day(_bear_call_day(path), _signal_bars(),
                                _ctx(tp_ratio=0.0))
        assert trades[0].exit_reason == "EOD"
