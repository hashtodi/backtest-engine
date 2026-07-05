"""Tests for engine/zayn_smc_backtest.py."""
from datetime import date, datetime as _dt, timedelta

import numpy as np
import pandas as pd
import pytest

from engine.zayn_smc_backtest import (
    DEFAULT_FORCE_EXIT_TIME,
    DayContext,
    IndicatorParams,
    ZaynSmcTrade,
    _ema,
    _pivot_points,
    _wilder_atr,
    compute_indicator_state,
    daily_prior_high_low,
    resample_intraday,
    run_one_day,
    summarize_metrics,
    trades_to_dataframe,
)


# --------------------------------------------------------------------------- #
#  Synthetic helpers                                                          #
# --------------------------------------------------------------------------- #

def _bar_1m(date_str, time_str, open_, high, low, close, volume=100):
    return {
        "datetime": f"{date_str}T{time_str}+05:30",
        "ts": pd.Timestamp(f"{date_str} {time_str}"),
        "date": date_str,
        "time": time_str,
        "open": float(open_), "high": float(high),
        "low": float(low), "close": float(close),
        "volume": int(volume),
    }


def _opt_row(date_str, time, opt_type, offset, strike, close,
             open_=None, oi=10000, spot=24500.0):
    if open_ is None:
        open_ = close
    moneyness = "ATM" if offset == 0 else ("OTM" if (
        (opt_type == "CE" and offset > 0) or (opt_type == "PE" and offset < 0)
    ) else "ITM")
    return {
        "datetime": f"{date_str}T{time}+05:30",
        "underlying": "NIFTY",
        "option_type": opt_type,
        "expiry_type": "WEEK",
        "expiry_code": 1,
        "strike_offset": offset,
        "moneyness": moneyness,
        "strike": strike,
        "spot": spot,
        "open": float(open_),
        "close": float(close),
        "oi": oi,
        "_time": time,
        "_date": date_str,
    }


def _grid_at_time(date_str, time, atm=24500.0, ce_close=100.0, pe_close=100.0,
                  ce_open=None, pe_open=None, spot=None):
    """Build all 21 strike rows (offsets -10..+10) for both CE and PE."""
    if spot is None:
        spot = atm
    rows = []
    for ot in ["CE", "PE"]:
        for off in range(-10, 11):
            strike = atm + off * 50
            close = ce_close if ot == "CE" else pe_close
            open_ = (ce_open if ce_open is not None else close) if ot == "CE" \
                else (pe_open if pe_open is not None else close)
            rows.append(_opt_row(date_str, time, ot, off, strike, close,
                                 open_=open_, spot=spot))
    return rows


# --------------------------------------------------------------------------- #
#  Math primitives                                                            #
# --------------------------------------------------------------------------- #

class TestPrimitives:
    def test_ema_matches_known_values(self):
        v = np.array([10.0] * 10)
        ema = _ema(v, 5)
        assert np.isnan(ema[0])
        assert np.isnan(ema[3])
        assert ema[4] == pytest.approx(10.0)
        assert ema[-1] == pytest.approx(10.0)

    def test_wilder_atr_constant_range(self):
        n = 30
        highs = np.full(n, 110.0)
        lows = np.full(n, 100.0)
        closes = np.full(n, 105.0)
        atr = _wilder_atr(highs, lows, closes, 14)
        assert np.isnan(atr[12])
        assert atr[13] == pytest.approx(10.0)
        assert atr[-1] == pytest.approx(10.0)

    def test_pivot_high_strict_centre(self):
        vals = np.array([1, 2, 3, 4, 5, 4, 3, 2, 1, 0, 1], dtype=float)
        # Pivot at index 4 (value 5); confirmed at index 4 + lb = 4 + 3 = 7 with lb=3.
        ph = _pivot_points(vals, 3, "high")
        assert np.isnan(ph[0])
        # Pivot confirmed at index 7
        assert ph[7] == pytest.approx(5.0)
        # No other pivot
        assert np.isnan(ph[8])

    def test_pivot_high_rejects_ties(self):
        vals = np.array([1, 2, 3, 4, 5, 5, 4, 3, 2, 1, 0], dtype=float)
        ph = _pivot_points(vals, 3, "high")
        # Tied peaks at 4 and 5 -> neither is a strict pivot
        assert np.all(np.isnan(ph))


# --------------------------------------------------------------------------- #
#  Resamplers                                                                 #
# --------------------------------------------------------------------------- #

class TestResample:
    def test_5m_bar_close_alignment(self):
        # Build 1-min bars from 09:15 to 09:24 (10 bars) - expect 2 5-min bars.
        rows = []
        for i in range(10):
            t = (_dt.strptime("09:15:00", "%H:%M:%S") + timedelta(minutes=i)).strftime("%H:%M:%S")
            rows.append(_bar_1m("2025-03-10", t, 100 + i, 105 + i, 95 + i, 102 + i))
        df = pd.DataFrame(rows)
        out = resample_intraday(df, minutes=5)
        assert len(out) == 2
        # First bar close = 09:20
        assert out.iloc[0]["time"] == "09:20:00"
        # First bar high = max(105..109) = 109
        assert out.iloc[0]["high"] == pytest.approx(109)
        # Open of first 5-min bar = open of 09:15 1-min bar = 100
        assert out.iloc[0]["open"] == pytest.approx(100)
        # Close = close of 09:19 = 102+4 = 106
        assert out.iloc[0]["close"] == pytest.approx(106)
        assert out.iloc[1]["time"] == "09:25:00"

    def test_daily_prior_high_low(self):
        rows = []
        rows.append(_bar_1m("2025-03-10", "09:15:00", 100, 120, 90, 110))
        rows.append(_bar_1m("2025-03-11", "09:15:00", 110, 115, 100, 112))
        rows.append(_bar_1m("2025-03-12", "09:15:00", 112, 130, 105, 125))
        df = pd.DataFrame(rows)
        out = daily_prior_high_low(df)
        assert np.isnan(out["2025-03-10"][0])
        assert out["2025-03-11"] == (120.0, 90.0)
        assert out["2025-03-12"] == (115.0, 100.0)


# --------------------------------------------------------------------------- #
#  Indicator state                                                            #
# --------------------------------------------------------------------------- #

def _make_5m_frame(date_str, bars):
    """bars: list of (HH:MM:SS_close, open, high, low, close)."""
    rows = []
    for time_close, o, h, l, c in bars:
        rows.append({
            "bar_close_ts": pd.Timestamp(f"{date_str} {time_close}"),
            "date": date_str,
            "time": time_close,
            "open": o, "high": h, "low": l, "close": c, "volume": 100,
        })
    return pd.DataFrame(rows)


class TestIndicatorState:
    def test_session_and_can_enter_window(self):
        # 4 bars: 09:20, 09:25, 15:15, 15:20
        df = _make_5m_frame("2025-03-10", [
            ("09:20:00", 100, 101, 99, 100),
            ("09:25:00", 100, 101, 99, 100),
            ("15:15:00", 100, 101, 99, 100),
            ("15:20:00", 100, 101, 99, 100),
        ])
        params = IndicatorParams(use_bias=False)
        out = compute_indicator_state(df, pd.DataFrame(), {}, params)
        assert list(out["in_session"]) == [True, True, True, True]
        # can_enter: in_session AND not in_flat AND time >= entry_earliest (09:20)
        # in_flat starts at >15:15. So 09:20, 09:25, 15:15 -> can_enter; 15:20 -> NO.
        assert list(out["can_enter"]) == [True, True, True, False]

    def test_displacement_detection(self):
        # Build 20 5-min bars; one giant bullish bar at the end.
        bars = []
        for i in range(19):
            bars.append((f"09:{20 + i * 5 // 60:02d}:00", 100, 102, 99, 101))
        bars = []
        t = _dt.strptime("09:20:00", "%H:%M:%S")
        for i in range(19):
            ts = (t + timedelta(minutes=5 * i)).strftime("%H:%M:%S")
            bars.append((ts, 100, 101, 99, 100))  # small bars
        # Now the displacement bar (well above ATR)
        ts = (t + timedelta(minutes=5 * 19)).strftime("%H:%M:%S")
        bars.append((ts, 100, 120, 99, 119))  # body=19, range=21 -> body_pct ok; range >> atr
        df = _make_5m_frame("2025-03-10", bars)
        params = IndicatorParams(use_bias=False, atr_len=14)
        out = compute_indicator_state(df, pd.DataFrame(), {}, params)
        assert out.iloc[-1]["displacement_up"] == True
        assert out.iloc[-1]["displacement_dn"] == False

    def test_swept_high_using_pdh(self):
        # PDH = 100. Bar pushes above (high=105) and closes back below (close=98).
        df = _make_5m_frame("2025-03-11", [
            ("09:20:00", 99, 105, 97, 98),
        ])
        params = IndicatorParams(use_bias=False, use_orhl=False)
        # daily_hl maps 2025-03-11 -> prev day's H/L = (100, 50)
        out = compute_indicator_state(df, pd.DataFrame(),
                                      {"2025-03-11": (100.0, 50.0)}, params)
        assert bool(out.iloc[0]["swept_high"])
        assert not bool(out.iloc[0]["swept_low"])

    def test_no_sweep_when_close_back_above_level(self):
        df = _make_5m_frame("2025-03-11", [
            ("09:20:00", 99, 105, 97, 102),  # closes ABOVE 100 -> no sweep of highs
        ])
        params = IndicatorParams(use_bias=False, use_orhl=False)
        out = compute_indicator_state(df, pd.DataFrame(),
                                      {"2025-03-11": (100.0, 50.0)}, params)
        assert not bool(out.iloc[0]["swept_high"])

    def test_sweep_mode_emits_signals_directly(self):
        # PDH=100, PDL=50. One bar sweeps high, the next sweeps low.
        df = _make_5m_frame("2025-03-11", [
            ("10:00:00", 99, 105, 97, 98),   # sweep_high -> short_sig
            ("10:05:00", 99, 100, 45, 60),   # sweep_low  -> long_sig
        ])
        params = IndicatorParams(signal_mode="sweep", use_bias=False,
                                 use_orhl=False)
        out = compute_indicator_state(df, pd.DataFrame(),
                                      {"2025-03-11": (100.0, 50.0)}, params)
        assert bool(out.iloc[0]["short_sig"])
        assert not bool(out.iloc[0]["long_sig"])
        assert bool(out.iloc[1]["long_sig"])
        assert not bool(out.iloc[1]["short_sig"])

    def test_sweep_mode_bias_filter_gates_signals(self):
        # PDH=100, bar sweeps high (-> would be a short_sig).
        # HTF shows close > ema -> bias UP, bias DOWN False -> short gated off.
        df = _make_5m_frame("2025-03-11", [
            ("10:00:00", 99, 105, 97, 98),
        ])
        # 60 HTF bars, last close 200, EMA seeded with mean 100 -> close > ema.
        htf_rows = []
        ts0 = pd.Timestamp("2025-03-04 09:15")
        for i in range(60):
            htf_rows.append({
                "bar_close_ts": ts0 + pd.Timedelta(minutes=60 * (i + 1)),
                "date": "", "time": "",
                "open": 100, "high": 100, "low": 100,
                "close": 100 if i < 49 else 200,
                "volume": 0,
            })
        df_htf = pd.DataFrame(htf_rows)
        params = IndicatorParams(signal_mode="sweep", use_bias=True,
                                 use_orhl=False, bias_len=50)
        out = compute_indicator_state(df, df_htf,
                                      {"2025-03-11": (100.0, 50.0)}, params)
        # Sweep still detected, but bias_up=True / bias_dn=False -> short gated.
        assert bool(out.iloc[0]["swept_high"])
        assert bool(out.iloc[0]["bias_up"])
        assert not bool(out.iloc[0]["bias_dn"])
        assert not bool(out.iloc[0]["short_sig"])


# --------------------------------------------------------------------------- #
#  Per-day driver                                                             #
# --------------------------------------------------------------------------- #

def _make_options_day(date_str, atm=24500.0, default_ce=50.0, default_pe=50.0,
                      overrides=None):
    """Build minute-by-minute option grid for [09:20..15:30].
    `overrides` is {(HH:MM:SS, OT, offset): {open|close: value}}.
    """
    overrides = overrides or {}
    rows = []
    start = _dt.strptime("09:20:00", "%H:%M:%S")
    end = _dt.strptime("15:31:00", "%H:%M:%S")
    cur = start
    while cur < end:
        t = cur.strftime("%H:%M:%S")
        grid = _grid_at_time(date_str, t, atm=atm,
                             ce_close=default_ce, pe_close=default_pe)
        for r in grid:
            key = (t, r["option_type"], r["strike_offset"])
            if key in overrides:
                ov = overrides[key]
                if "open" in ov:
                    r["open"] = ov["open"]
                if "close" in ov:
                    r["close"] = ov["close"]
        rows.extend(grid)
        cur += timedelta(minutes=1)
    return pd.DataFrame(rows)


def _ctx(**overrides):
    defaults = dict(
        date=date(2025, 3, 10),
        force_exit_time="15:20:00",
        entry_earliest_time="09:20:00",
        flat_window_start="15:15:00",
        lots=1,
        sell_offset_abs=2,
        buy_offset_abs=6,
        tp_inr=0.0,
        sl_inr=0.0,
    )
    defaults.update(overrides)
    return DayContext(**defaults)


def _make_indicator_frame_with_signals(date_str, signals):
    """signals: list of (HH:MM:SS, long_sig, short_sig). Builds a minimal
    5m frame with the columns the driver needs."""
    rows = []
    for t, ls, ss in signals:
        rows.append({
            "bar_close_ts": pd.Timestamp(f"{date_str} {t}"),
            "date": date_str,
            "time": t,
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
            "volume": 100,
            "in_session": True,
            "can_enter": True,
            "long_sig": ls, "short_sig": ss,
        })
    return pd.DataFrame(rows)


class TestRunOneDay:
    def test_single_long_signal_time_exit(self):
        # Long signal at 10:00 -> enter PE spread at 10:01. No SL/TP. Force exit at 15:20.
        signals = [("10:00:00", True, False)]
        df_5m = _make_indicator_frame_with_signals("2025-03-10", signals)
        opts = _make_options_day("2025-03-10", default_pe=50.0)
        # No price change anywhere -> P&L = 0, exit reason TIME.
        ctx = _ctx()
        trades = run_one_day(df_5m, opts, ctx)
        assert len(trades) == 1
        t = trades[0]
        assert t.side == "PE"
        assert t.signal_direction == "LONG"
        assert t.signal_time == "10:00"
        assert t.entry_time == "10:01"
        assert t.exit_reason == "TIME"
        assert t.exit_time == "15:20"
        assert t.pnl_inr == pytest.approx(0.0)

    def test_short_signal_ce_side(self):
        signals = [("10:00:00", False, True)]
        df_5m = _make_indicator_frame_with_signals("2025-03-10", signals)
        opts = _make_options_day("2025-03-10")
        ctx = _ctx()
        trades = run_one_day(df_5m, opts, ctx)
        assert len(trades) == 1
        assert trades[0].side == "CE"
        assert trades[0].signal_direction == "SHORT"

    def test_no_trade_when_no_signal(self):
        df_5m = _make_indicator_frame_with_signals(
            "2025-03-10",
            [("10:00:00", False, False), ("10:05:00", False, False)],
        )
        opts = _make_options_day("2025-03-10")
        trades = run_one_day(df_5m, opts, _ctx())
        assert trades == []

    def test_opposite_signal_auto_flip(self):
        # Long at 10:00, then SHORT at 11:00 -> close PE, open CE at 11:01.
        # Then TIME exit the CE at 15:20.
        signals = [("10:00:00", True, False), ("11:00:00", False, True)]
        df_5m = _make_indicator_frame_with_signals("2025-03-10", signals)
        opts = _make_options_day("2025-03-10")
        trades = run_one_day(df_5m, opts, _ctx())
        assert len(trades) == 2
        t1, t2 = trades
        assert t1.side == "PE" and t1.exit_reason == "OPP"
        assert t1.exit_signal_time == "11:00"
        assert t1.exit_time == "11:01"
        assert t2.side == "CE" and t2.signal_direction == "SHORT"
        assert t2.signal_time == "11:00"
        assert t2.entry_time == "11:01"
        assert t2.exit_reason == "TIME"

    def test_tp_exit_when_premium_decays(self):
        # Long at 10:00. At 11:00 the short PE drops from 50 -> 40
        # giving live P&L = (50 - 40) pts * 65 contracts = +650.
        signals = [("10:00:00", True, False)]
        df_5m = _make_indicator_frame_with_signals("2025-03-10", signals)
        overrides = {
            # At 11:00 the short PE (offset -2) prints close=40.
            ("11:00:00", "PE", -2): {"close": 40.0},
            # At 11:01 OPEN, that's our exit fill price.
            ("11:01:00", "PE", -2): {"open": 40.0, "close": 40.0},
        }
        opts = _make_options_day("2025-03-10", overrides=overrides)
        trades = run_one_day(df_5m, opts, _ctx(tp_inr=500.0))
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "TP"
        assert t.exit_signal_time == "11:00"
        assert t.exit_time == "11:01"
        assert t.pnl_inr > 0

    def test_sl_exit_when_premium_expands(self):
        signals = [("10:00:00", True, False)]
        df_5m = _make_indicator_frame_with_signals("2025-03-10", signals)
        overrides = {
            # Short PE goes 50 -> 70 at 11:00 close. Live = (50 - 70) * 65 = -1300.
            ("11:00:00", "PE", -2): {"close": 70.0},
            ("11:01:00", "PE", -2): {"open": 70.0, "close": 70.0},
        }
        opts = _make_options_day("2025-03-10", overrides=overrides)
        trades = run_one_day(df_5m, opts, _ctx(sl_inr=500.0))
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "SL"
        assert t.pnl_inr < 0

    def test_multiple_trades_via_consecutive_opposite_signals(self):
        # LONG @ 10:00, SHORT @ 11:00, LONG @ 12:00 -> three trades.
        signals = [
            ("10:00:00", True, False),
            ("11:00:00", False, True),
            ("12:00:00", True, False),
        ]
        df_5m = _make_indicator_frame_with_signals("2025-03-10", signals)
        opts = _make_options_day("2025-03-10")
        trades = run_one_day(df_5m, opts, _ctx())
        assert len(trades) == 3
        assert [t.side for t in trades] == ["PE", "CE", "PE"]
        assert [t.exit_reason for t in trades] == ["OPP", "OPP", "TIME"]
        # trade_idx increments within the day
        assert [t.trade_idx for t in trades] == [1, 2, 3]


# --------------------------------------------------------------------------- #
#  Reporting                                                                  #
# --------------------------------------------------------------------------- #

class TestSummary:
    def test_summary_counts_and_dataframe(self):
        signals = [("10:00:00", True, False), ("11:00:00", False, True)]
        df_5m = _make_indicator_frame_with_signals("2025-03-10", signals)
        opts = _make_options_day("2025-03-10")
        trades = run_one_day(df_5m, opts, _ctx())
        summary = summarize_metrics(trades, 200000.0)
        assert summary["trades_placed"] == 2
        assert summary["pe_trades"] == 1
        assert summary["ce_trades"] == 1
        assert summary["days_with_trades"] == 1
        out_df = trades_to_dataframe(trades)
        assert "side" in out_df.columns
        assert "exit_reason" in out_df.columns
