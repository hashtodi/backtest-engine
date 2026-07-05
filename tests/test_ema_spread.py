"""Tests for engine/ema_spread_backtest.py."""
import pandas as pd
import pytest

from engine.ema_spread_backtest import (
    LOT_SIZE_NIFTY,
    EmaDayContext,
    build_signal_bars,
    generate_signals,
    eligible_signals,
    run_one_day,
    run_backtest,
    summarize_metrics,
    trades_to_dataframe,
)


# --------------------------------------------------------------------------- #
#  Fixtures                                                                   #
# --------------------------------------------------------------------------- #

def _spot_rows(date_str, start_time, spots):
    """Per-minute spot rows starting at start_time (HH:MM), one per spot value."""
    h, m = (int(x) for x in start_time.split(":"))
    rows = []
    for i, s in enumerate(spots):
        tm = h * 60 + m + i
        rows.append({
            "_date": date_str,
            "_time": f"{tm // 60:02d}:{tm % 60:02d}:00",
            "spot": float(s),
        })
    return pd.DataFrame(rows)


def _chain_rows(date_str, time_str, spot, atm=24500.0,
                sell_shift=0.0, all_shift=0.0,
                option_types=("CE", "PE"), offsets=range(-7, 8)):
    """Full synthetic option chain for one minute.

    Pricing: CE close = 100 - off*10, PE close = 100 + off*10, so the sell leg
    (|off|=2) is at 80 and the buy leg (|off|=6) is at 40 -> 40 pts credit.
    `sell_shift` moves only the |off|==2 rows; `all_shift` moves every row.
    """
    rows = []
    for ot in option_types:
        for off in offsets:
            strike = atm + off * 50
            close = (100.0 - off * 10 if ot == "CE" else 100.0 + off * 10)
            close += all_shift
            if abs(off) == 2:
                close += sell_shift
            moneyness = "ATM" if off == 0 else (
                "OTM" if (ot == "CE" and off > 0) or (ot == "PE" and off < 0)
                else "ITM"
            )
            rows.append({
                "datetime": f"{date_str}T{time_str}+05:30",
                "underlying": "NIFTY",
                "option_type": ot,
                "expiry_type": "WEEK",
                "expiry_code": 1,
                "strike_offset": off,
                "moneyness": moneyness,
                "strike": strike,
                "spot": float(spot),
                "open": max(0.5, close),
                "close": max(0.5, close),
                "oi": 10000,
                "_time": time_str,
                "_date": date_str,
            })
    return rows


def _day_frame(date_str, minute_specs):
    """Build a day's options frame. minute_specs: list of dicts with keys
    time, spot and optional sell_shift / all_shift / option_types."""
    rows = []
    for spec in minute_specs:
        rows.extend(_chain_rows(
            date_str, spec["time"], spec["spot"],
            sell_shift=spec.get("sell_shift", 0.0),
            all_shift=spec.get("all_shift", 0.0),
            option_types=spec.get("option_types", ("CE", "PE")),
        ))
    return pd.DataFrame(rows)


def _ctx(**kw):
    defaults = dict(
        date="2025-03-10",
        lots=1,
        sell_offset_abs=2,
        buy_offset_abs=6,
        sl_points=50.0,
        tp_points=75.0,
        square_off_time="15:15:00",
    )
    defaults.update(kw)
    return EmaDayContext(**defaults)


# --------------------------------------------------------------------------- #
#  Signal-series construction                                                 #
# --------------------------------------------------------------------------- #

class TestBuildSignalBars:
    def test_three_minute_buckets_anchored_0915(self):
        spot = _spot_rows("2025-03-10", "09:15", [100, 101, 102, 103, 104, 105, 106, 107, 108])
        bars = build_signal_bars(spot, timeframe_min=3)
        assert list(bars["bar_start"]) == ["09:15:00", "09:18:00", "09:21:00"]
        assert list(bars["close_minute"]) == ["09:17:00", "09:20:00", "09:23:00"]
        assert list(bars["close"]) == [102.0, 105.0, 108.0]

    def test_partial_bucket_uses_last_available_minute(self):
        spot = _spot_rows("2025-03-10", "09:15", [100, 101, 102, 103])  # 09:18 alone
        bars = build_signal_bars(spot, timeframe_min=3)
        assert list(bars["close_minute"]) == ["09:17:00", "09:18:00"]
        assert list(bars["close"]) == [102.0, 103.0]

    def test_bars_span_multiple_days(self):
        d1 = _spot_rows("2025-03-10", "09:15", [100, 101, 102])
        d2 = _spot_rows("2025-03-11", "09:15", [200, 201, 202])
        bars = build_signal_bars(pd.concat([d1, d2]), timeframe_min=3)
        assert list(bars["date"]) == ["2025-03-10", "2025-03-11"]
        assert list(bars["close"]) == [102.0, 202.0]


class TestGenerateSignals:
    def _bars(self, closes, dates=None):
        n = len(closes)
        return pd.DataFrame({
            "date": dates or ["2025-03-10"] * n,
            "bar_start": [f"{(9*60+30+3*i)//60:02d}:{(9*60+30+3*i)%60:02d}:00" for i in range(n)],
            "close_minute": [f"{(9*60+32+3*i)//60:02d}:{(9*60+32+3*i)%60:02d}:00" for i in range(n)],
            "close": [float(c) for c in closes],
        })

    def test_long_on_crossover_above_upper_band(self):
        # length=9 (alpha 0.2): EMA stays 100, then bar 11 closes 130:
        # ema=106, upper=116 -> 130 > 116 with prev 100 <= prev upper 110.
        bars = generate_signals(self._bars([100] * 10 + [130]), ema_length=9, buffer=10.0)
        assert list(bars["signal"])[:-1] == [None] * 10
        assert bars.iloc[-1]["signal"] == "LONG"

    def test_short_on_crossunder_lower_band(self):
        bars = generate_signals(self._bars([100] * 10 + [70]), ema_length=9, buffer=10.0)
        assert bars.iloc[-1]["signal"] == "SHORT"

    def test_prev_close_inside_buffer_zone_still_signals(self):
        # Pine-faithful: prev close 105 is above the EMA but below the band.
        bars = generate_signals(self._bars([100] * 10 + [105, 130]), ema_length=9, buffer=10.0)
        assert bars.iloc[-2]["signal"] is None
        assert bars.iloc[-1]["signal"] == "LONG"

    def test_no_repeat_signal_while_staying_above_band(self):
        bars = generate_signals(self._bars([100] * 10 + [130, 135]), ema_length=9, buffer=10.0)
        assert bars.iloc[-2]["signal"] == "LONG"
        assert bars.iloc[-1]["signal"] is None

    def test_ema_continuous_across_days(self):
        closes = [100] * 10 + [130]
        dates = ["2025-03-10"] * 10 + ["2025-03-11"]
        bars = generate_signals(self._bars(closes, dates), ema_length=9, buffer=10.0)
        assert bars.iloc[-1]["signal"] == "LONG"  # carryover EMA, no daily reset


class TestEligibleSignals:
    def test_window_uses_bar_start_time(self):
        bars = pd.DataFrame({
            "date": ["2025-03-10"] * 4,
            "bar_start": ["09:27:00", "09:30:00", "14:57:00", "15:00:00"],
            "close_minute": ["09:29:00", "09:32:00", "14:59:00", "15:02:00"],
            "close": [1.0] * 4,
            "signal": ["LONG", "LONG", "SHORT", "SHORT"],
        })
        out = eligible_signals(bars, "09:30:00", "15:00:00",
                               "2025-03-10", "2025-03-10")
        assert out == {"2025-03-10": [("09:32:00", "LONG"), ("14:59:00", "SHORT")]}

    def test_dates_outside_backtest_window_excluded(self):
        bars = pd.DataFrame({
            "date": ["2025-03-10", "2025-03-11"],
            "bar_start": ["10:00:00", "10:00:00"],
            "close_minute": ["10:02:00", "10:02:00"],
            "close": [1.0, 1.0],
            "signal": ["LONG", "LONG"],
        })
        out = eligible_signals(bars, "09:30:00", "15:00:00",
                               "2025-03-11", "2025-03-11")
        assert list(out.keys()) == ["2025-03-11"]


# --------------------------------------------------------------------------- #
#  run_one_day                                                                #
# --------------------------------------------------------------------------- #

class TestEntry:
    def test_long_signal_sells_put_spread_at_signal_close(self):
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "15:15:00", "spot": 24512.0},
        ])
        trades, skipped = run_one_day(day, [("10:23:00", "LONG")], _ctx())
        assert skipped == 0
        assert len(trades) == 1
        t = trades[0]
        assert t.direction == "LONG"
        assert t.entry_time == "10:23"
        assert t.entry_spot == 24512.0
        assert t.atm_strike == 24500.0
        sell = t.legs["sell"]
        buy = t.legs["buy"]
        assert (sell.option_type, sell.strike, sell.entry_price) == ("PE", 24400.0, 80.0)
        assert (buy.option_type, buy.strike, buy.entry_price) == ("PE", 24200.0, 40.0)
        assert t.net_credit_pts == pytest.approx(40.0)

    def test_short_signal_sells_call_spread(self):
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "15:15:00", "spot": 24512.0},
        ])
        trades, _ = run_one_day(day, [("10:23:00", "SHORT")], _ctx())
        t = trades[0]
        assert t.direction == "SHORT"
        assert (t.legs["sell"].option_type, t.legs["sell"].strike) == ("CE", 24600.0)
        assert (t.legs["buy"].option_type, t.legs["buy"].strike) == ("CE", 24800.0)

    def test_missing_leg_skips_signal(self):
        # Chain without PE rows -> LONG entry cannot fill.
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0, "option_types": ("CE",)},
            {"time": "15:15:00", "spot": 24512.0},
        ])
        trades, skipped = run_one_day(day, [("10:23:00", "LONG")], _ctx())
        assert trades == []
        assert skipped == 1

    def test_same_direction_signal_ignored_while_open(self):
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "10:26:00", "spot": 24512.0},
            {"time": "15:15:00", "spot": 24512.0},
        ])
        trades, _ = run_one_day(
            day, [("10:23:00", "LONG"), ("10:26:00", "LONG")], _ctx())
        assert len(trades) == 1


class TestExits:
    def test_sl_for_long_when_spot_drops_50(self):
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "10:24:00", "spot": 24462.0, "sell_shift": 20.0},
            {"time": "15:15:00", "spot": 24462.0},
        ])
        trades, _ = run_one_day(day, [("10:23:00", "LONG")], _ctx())
        t = trades[0]
        assert t.exit_reason == "SL"
        assert t.exit_time == "10:24"
        assert t.exit_spot == 24462.0
        # (sell_e + buy_x) - (sell_x + buy_e) = (80+40) - (100+40) = -20
        assert t.pnl_pts == pytest.approx(-20.0)
        assert t.pnl_inr == pytest.approx(-20.0 * LOT_SIZE_NIFTY)

    def test_tp_for_long_when_spot_rises_75(self):
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "10:24:00", "spot": 24587.0, "sell_shift": -20.0},
            {"time": "15:15:00", "spot": 24587.0},
        ])
        trades, _ = run_one_day(day, [("10:23:00", "LONG")], _ctx())
        t = trades[0]
        assert t.exit_reason == "TP"
        assert t.pnl_pts == pytest.approx(20.0)

    def test_sl_for_short_when_spot_rises_50(self):
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "10:24:00", "spot": 24562.0, "sell_shift": 20.0},
            {"time": "15:15:00", "spot": 24562.0},
        ])
        trades, _ = run_one_day(day, [("10:23:00", "SHORT")], _ctx())
        assert trades[0].exit_reason == "SL"

    def test_time_exit_at_square_off(self):
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "10:24:00", "spot": 24520.0},
            {"time": "15:15:00", "spot": 24530.0, "all_shift": 5.0},
        ])
        trades, _ = run_one_day(day, [("10:23:00", "LONG")], _ctx())
        t = trades[0]
        assert t.exit_reason == "TIME"
        assert t.exit_time == "15:15"
        # symmetric +5 shift on both legs cancels out
        assert t.pnl_pts == pytest.approx(0.0)

    def test_pnl_scales_with_lots(self):
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "10:24:00", "spot": 24462.0, "sell_shift": 20.0},
            {"time": "15:15:00", "spot": 24462.0},
        ])
        trades, _ = run_one_day(day, [("10:23:00", "LONG")], _ctx(lots=4))
        assert trades[0].pnl_inr == pytest.approx(-20.0 * LOT_SIZE_NIFTY * 4)


class TestReversal:
    def test_opposite_signal_reverses_position(self):
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "10:26:00", "spot": 24500.0},
            {"time": "15:15:00", "spot": 24500.0},
        ])
        trades, _ = run_one_day(
            day, [("10:23:00", "LONG"), ("10:26:00", "SHORT")], _ctx())
        assert len(trades) == 2
        first, second = trades
        assert first.exit_reason == "REVERSAL"
        assert first.exit_time == "10:26"
        assert second.direction == "SHORT"
        assert second.entry_time == "10:26"
        assert second.entry_spot == 24500.0
        assert second.exit_reason == "TIME"

    def test_sl_and_opposite_signal_same_minute_exits_sl_then_enters(self):
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "10:26:00", "spot": 24462.0},
            {"time": "15:15:00", "spot": 24462.0},
        ])
        trades, _ = run_one_day(
            day, [("10:23:00", "LONG"), ("10:26:00", "SHORT")], _ctx())
        assert len(trades) == 2
        assert trades[0].exit_reason == "SL"
        assert trades[1].direction == "SHORT"
        assert trades[1].entry_time == "10:26"


class TestFillFallback:
    def test_exit_fill_walks_forward_when_legs_missing(self):
        # SL detected at 10:24 (spot always present via CE rows) but PE legs
        # are missing that minute; they reappear at 10:25.
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "10:24:00", "spot": 24462.0, "option_types": ("CE",)},
            {"time": "10:25:00", "spot": 24470.0, "sell_shift": 20.0},
            {"time": "15:15:00", "spot": 24470.0},
        ])
        trades, _ = run_one_day(day, [("10:23:00", "LONG")], _ctx())
        t = trades[0]
        assert t.exit_reason == "SL"
        assert t.exit_time == "10:25"
        assert t.fill_fallback is True
        assert t.pnl_pts == pytest.approx(-20.0)

    def test_exit_falls_back_to_last_known_prices(self):
        # PE legs vanish after entry and never come back, square-off included.
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "10:24:00", "spot": 24462.0, "option_types": ("CE",)},
            {"time": "15:15:00", "spot": 24462.0, "option_types": ("CE",)},
        ])
        trades, _ = run_one_day(day, [("10:23:00", "LONG")], _ctx())
        t = trades[0]
        assert t.exit_reason == "SL"
        assert t.fill_fallback is True
        assert t.pnl_pts == pytest.approx(0.0)  # entry closes reused


# --------------------------------------------------------------------------- #
#  run_backtest integration                                                   #
# --------------------------------------------------------------------------- #

def _integration_frame(dates):
    """Days where 10 flat 3-min bars at 24500 are followed by a jump to 24650
    (close minute 09:47). With ema_length=9, buffer=10 -> LONG at 09:47."""
    rows = []
    for d in dates:
        for i in range(33):  # 09:15 .. 09:47
            tm = 9 * 60 + 15 + i
            time_str = f"{tm // 60:02d}:{tm % 60:02d}:00"
            spot = 24650.0 if tm >= 9 * 60 + 45 else 24500.0
            if time_str in ("09:47:00",):
                rows.extend(_chain_rows(d, time_str, spot, atm=24650.0))
            else:
                rows.extend(_chain_rows(d, time_str, spot, atm=24650.0,
                                        option_types=("CE",), offsets=[7]))
        rows.extend(_chain_rows(d, "15:15:00", 24650.0, atm=24650.0))
    return pd.DataFrame(rows)


def _config(start, end, **kw):
    cfg = {
        "signal": {"timeframe_min": 3, "ema_length": 9, "buffer_points": 10,
                   "warmup_days": 0},
        "entry": {"window_start": "09:30", "window_end": "15:00"},
        "structure": {"sell_offset_abs": 2, "buy_offset_abs": 6, "lots": 1},
        "exit": {"sl_points": 50, "tp_points": 75, "square_off_time": "15:15"},
        "sizing": {"reference_capital": 200000},
        "backtest_start": start,
        "backtest_end": end,
    }
    cfg.update(kw)
    return cfg


class TestRunBacktest:
    def test_signal_day_produces_trade(self):
        df = _integration_frame(["2025-03-10"])
        result = run_backtest(df, _config("2025-03-10", "2025-03-10"))
        trades = result["trades"]
        assert len(trades) == 1
        t = trades[0]
        assert t.direction == "LONG"
        assert t.entry_time == "09:47"
        assert t.exit_reason == "TIME"

    def test_warmup_days_feed_ema_but_do_not_trade(self):
        # Same pattern both days; only day 2 is inside the backtest window.
        df = _integration_frame(["2025-03-10", "2025-03-11"])
        result = run_backtest(df, _config("2025-03-11", "2025-03-11"))
        trades = result["trades"]
        assert all(t.date == "2025-03-11" for t in trades)

    def test_running_equity_accumulates(self):
        df = _integration_frame(["2025-03-10"])
        result = run_backtest(df, _config("2025-03-10", "2025-03-10"))
        t = result["trades"][0]
        assert t.running_equity_inr == pytest.approx(200000 + t.pnl_inr)


# --------------------------------------------------------------------------- #
#  Reporting                                                                  #
# --------------------------------------------------------------------------- #

class TestReporting:
    def _trades(self):
        day = _day_frame("2025-03-10", [
            {"time": "10:23:00", "spot": 24512.0},
            {"time": "10:24:00", "spot": 24462.0, "sell_shift": 20.0},
            {"time": "15:15:00", "spot": 24462.0},
        ])
        trades, _ = run_one_day(day, [("10:23:00", "LONG")], _ctx())
        return trades

    def test_summarize_metrics(self):
        trades = self._trades()
        s = summarize_metrics(trades, 200000.0)
        assert s["total_trades"] == 1
        assert s["losses"] == 1
        assert s["exit_reason_counts"] == {"SL": 1}
        assert s["long_trades"] == 1

    def test_trades_to_dataframe_has_leg_columns(self):
        df = trades_to_dataframe(self._trades())
        assert "sell_strike" in df.columns
        assert "buy_strike" in df.columns
        assert df.iloc[0]["sell_strike"] == 24400.0
