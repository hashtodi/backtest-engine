"""Tests for engine/regime_rsi_spread_backtest.py."""
import pandas as pd
import pytest

from engine.regime_rsi_spread_backtest import (
    LOT_SIZE_NIFTY,
    RegimeRsiDayContext,
    build_signal_bars,
    generate_signals,
    run_one_day,
    run_backtest,
    summarize_metrics,
    trades_to_dataframe,
)


# --------------------------------------------------------------------------- #
#  Fixtures                                                                   #
# --------------------------------------------------------------------------- #

OFFSETS = range(-7, 8)


def _minrange(start, end):
    """Inclusive list of 'HH:MM:00' minute strings."""
    sh, sm = int(start[:2]), int(start[3:5])
    eh, em = int(end[:2]), int(end[3:5])
    return [f"{x // 60:02d}:{x % 60:02d}:00" for x in range(sh * 60 + sm, eh * 60 + em + 1)]


def _chain_at(date_str, time_str, spot, atm=24500.0,
              all_shift=0.0, sell_shift=0.0, open_delta=7.0):
    """Full synthetic option chain for one minute.

    close: CE = 100 - off*10, PE = 100 + off*10  (sell leg |off|=2 -> 80,
    buy leg |off|=6 -> 40 -> 40 pts credit). open = close + open_delta so a
    fill that reads OPEN is distinguishable from one that reads close.
    `all_shift` moves every leg; `sell_shift` moves only the |off|==2 legs.
    """
    rows = []
    for ot in ("CE", "PE"):
        for off in OFFSETS:
            strike = atm + off * 50
            close = (100.0 - off * 10 if ot == "CE" else 100.0 + off * 10)
            close += all_shift
            if abs(off) == 2:
                close += sell_shift
            close = max(0.5, close)
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
                "open": max(0.5, close + open_delta),
                "close": close,
                "oi": 10000,
                "_time": time_str,
                "_date": date_str,
            })
    return rows


def _day_df(date_str, times, spot_default=24500.0, atm=24500.0,
            spot_over=None, all_shift_over=None, sell_shift_over=None):
    spot_over = spot_over or {}
    all_shift_over = all_shift_over or {}
    sell_shift_over = sell_shift_over or {}
    rows = []
    for t in times:
        rows.extend(_chain_at(
            date_str, t, spot_over.get(t, spot_default), atm=atm,
            all_shift=all_shift_over.get(t, 0.0),
            sell_shift=sell_shift_over.get(t, 0.0),
        ))
    return pd.DataFrame(rows)


def _bars(rows):
    """Build a day's 15m signal-bar frame from minimal dicts."""
    defaults = dict(regime_bull=True, regime_flip=False,
                    long_sig=False, short_sig=False, within_dist=True)
    full = []
    for r in rows:
        d = dict(defaults)
        d.update(r)
        full.append(d)
    return pd.DataFrame(full)


def _ctx(**kw):
    defaults = dict(
        date="2025-03-10", lots=1, sell_offset_abs=2, buy_offset_abs=6,
        sl_points=20.0, tp_points=20.0,
        window_start="09:30:00", window_end="15:00:00",
        square_off_time="10:00:00", use_filter=True,
    )
    defaults.update(kw)
    return RegimeRsiDayContext(**defaults)


# --------------------------------------------------------------------------- #
#  Signal generation                                                          #
# --------------------------------------------------------------------------- #

class TestGenerateSignals:
    def _frame(self, closes):
        n = len(closes)
        return pd.DataFrame({
            "date": ["2025-03-10"] * n,
            "bar_start": [f"{9 + i // 4:02d}:{(i % 4) * 15:02d}:00" for i in range(n)],
            "close_minute": [f"{9 + i // 4:02d}:{(i % 4) * 15 + 14:02d}:00" for i in range(n)],
            "close": [float(c) for c in closes],
        })

    def test_regime_and_distance_match_wma(self):
        closes = [100, 98, 96, 99, 101, 100, 97, 95, 98, 102]
        out = generate_signals(self._frame(closes), rsi_length=2,
                               rsi_ma_length=2, wma_length=3, max_dist=2.0)
        # bar index 2: closes[0:3]=[100,98,96], weights oldest->newest [1,2,3]
        # -> (1*100 + 2*98 + 3*96)/6 = 584/6
        assert out["wma"].iloc[2] == pytest.approx(584 / 6)
        # regime_bull == close > wma everywhere wma is defined
        defined = out["wma"].notna()
        assert ((out.loc[defined, "close"] > out.loc[defined, "wma"])
                == out.loc[defined, "regime_bull"]).all()
        # within_dist == |close - wma| <= max_dist
        exp = (out["close"] - out["wma"]).abs() <= 2.0
        assert (exp.fillna(False) == out["within_dist"]).all()

    def test_crossover_columns_are_self_consistent(self):
        closes = [100, 98, 96, 99, 101, 100, 97, 95, 98, 102, 99, 101]
        out = generate_signals(self._frame(closes), rsi_length=2,
                               rsi_ma_length=2, wma_length=3, max_dist=999)
        rsi, ma = out["rsi"], out["rsi_ma"]
        # at least one of each kind of cross shows up in a zig-zag series
        assert out["long_sig"].any()
        assert out["short_sig"].any()
        # a bar is never both a crossover and a crossunder
        assert not (out["long_sig"] & out["short_sig"]).any()
        for i in range(1, len(out)):
            if out["long_sig"].iloc[i]:
                assert rsi.iloc[i - 1] <= ma.iloc[i - 1] and rsi.iloc[i] > ma.iloc[i]
            if out["short_sig"].iloc[i]:
                assert rsi.iloc[i - 1] >= ma.iloc[i - 1] and rsi.iloc[i] < ma.iloc[i]


# --------------------------------------------------------------------------- #
#  Entry fills at the NEXT 1-min OPEN                                         #
# --------------------------------------------------------------------------- #

class TestEntryFill:
    def test_long_entry_fills_next_minute_open(self):
        times = _minrange("09:40", "10:00")
        day_df = _day_df("2025-03-10", times)            # flat spot 24500
        bars = _bars([{"bar_start": "09:30:00", "close_minute": "09:44:00",
                       "long_sig": True, "regime_bull": True}])
        trades, skipped = run_one_day(day_df, bars, _ctx())
        assert skipped == 0 and len(trades) == 1
        t = trades[0]
        assert t.direction == "LONG"
        assert t.entry_time == "09:45"          # next minute after 09:44 close
        assert t.entry_spot == 24500.0
        assert t.atm_strike == 24500.0
        # bull-put: SELL PE-2 (24400), BUY PE-6 (24200), filled at OPEN (close+7)
        assert t.legs["sell"].option_type == "PE"
        assert (t.legs["sell"].strike, t.legs["sell"].entry_price) == (24400.0, 87.0)
        assert (t.legs["buy"].strike, t.legs["buy"].entry_price) == (24200.0, 47.0)
        assert t.net_credit_pts == pytest.approx(40.0)

    def test_short_entry_uses_call_legs(self):
        times = _minrange("09:40", "10:00")
        day_df = _day_df("2025-03-10", times)
        bars = _bars([{"bar_start": "09:30:00", "close_minute": "09:44:00",
                       "short_sig": True, "regime_bull": False}])
        trades, _ = run_one_day(day_df, bars, _ctx())
        assert len(trades) == 1 and trades[0].direction == "SHORT"
        t = trades[0]
        assert t.legs["sell"].option_type == "CE"
        assert (t.legs["sell"].strike, t.legs["sell"].entry_price) == (24600.0, 87.0)
        assert (t.legs["buy"].strike, t.legs["buy"].entry_price) == (24800.0, 47.0)


# --------------------------------------------------------------------------- #
#  Exits: SL/TP on the spot close, fill at the NEXT 1-min OPEN                #
# --------------------------------------------------------------------------- #

class TestExits:
    def _long_setup(self, spot_over, sell_shift_over=None):
        times = _minrange("09:40", "10:00")
        day_df = _day_df("2025-03-10", times, spot_over=spot_over,
                         sell_shift_over=sell_shift_over)
        bars = _bars([{"bar_start": "09:30:00", "close_minute": "09:44:00",
                       "long_sig": True, "regime_bull": True}])
        return day_df, bars

    def test_tp_exit_fills_next_minute_open(self):
        # spot hits +20 at 09:50 -> TP detected on the 09:50 close, exit 09:51.
        # sell leg drops 10 at 09:51 only -> +10 pts profit on the short leg.
        day_df, bars = self._long_setup(
            spot_over={"09:50:00": 24525, "09:51:00": 24525},
            sell_shift_over={"09:51:00": -10.0},
        )
        trades, _ = run_one_day(day_df, bars, _ctx())
        t = trades[0]
        assert t.exit_reason == "TP"
        assert t.exit_time == "09:51"
        assert t.legs["sell"].exit_price == 77.0     # OPEN at 09:51 (80-10+7)
        assert t.legs["buy"].exit_price == 47.0       # unchanged
        assert t.pnl_pts == pytest.approx(10.0)
        assert t.pnl_inr == pytest.approx(10.0 * LOT_SIZE_NIFTY)

    def test_sl_exit_fills_next_minute_open(self):
        day_df, bars = self._long_setup(
            spot_over={"09:50:00": 24475, "09:51:00": 24475})
        trades, _ = run_one_day(day_df, bars, _ctx())
        t = trades[0]
        assert t.exit_reason == "SL"
        assert t.exit_time == "09:51"

    def test_time_squareoff_when_no_sl_tp(self):
        times = _minrange("09:40", "10:00")
        day_df = _day_df("2025-03-10", times)           # flat -> never SL/TP
        bars = _bars([{"bar_start": "09:30:00", "close_minute": "09:44:00",
                       "long_sig": True, "regime_bull": True}])
        trades, _ = run_one_day(day_df, bars, _ctx(square_off_time="10:00:00"))
        t = trades[0]
        assert t.exit_reason == "TIME"
        assert t.exit_time == "10:00"


# --------------------------------------------------------------------------- #
#  One trade per regime / flip re-arm / no reversal / filter                  #
# --------------------------------------------------------------------------- #

class TestRegimeBudget:
    def _two_signal_bars(self, second):
        """First LONG at 09:44 (exits TP by 09:51); a second bar at 09:59."""
        first = {"bar_start": "09:30:00", "close_minute": "09:44:00",
                 "long_sig": True, "regime_bull": True}
        return _bars([first, second])

    def test_second_signal_same_regime_is_blocked(self):
        times = _minrange("09:40", "10:14")
        day_df = _day_df("2025-03-10", times,
                         spot_over={t: 24525 for t in _minrange("09:50", "09:52")})
        # 2nd bull LONG signal at 09:59 -> blocked (trade_taken, no flip).
        second = {"bar_start": "09:45:00", "close_minute": "09:59:00",
                  "long_sig": True, "regime_bull": True}
        trades, _ = run_one_day(day_df, self._two_signal_bars(second),
                                _ctx(square_off_time="10:14:00"))
        assert len(trades) == 1

    def test_regime_flip_rearms_a_second_trade(self):
        times = _minrange("09:40", "10:14")
        day_df = _day_df("2025-03-10", times,
                         spot_over={t: 24525 for t in _minrange("09:50", "09:52")})
        # flip to bear at 09:59 -> re-armed -> SHORT taken (enters 10:00 open).
        second = {"bar_start": "09:45:00", "close_minute": "09:59:00",
                  "short_sig": True, "regime_bull": False, "regime_flip": True}
        trades, _ = run_one_day(day_df, self._two_signal_bars(second),
                                _ctx(square_off_time="10:14:00"))
        assert [t.direction for t in trades] == ["LONG", "SHORT"]
        assert trades[1].entry_time == "10:00"

    def test_no_reversal_while_position_open(self):
        # First LONG never exits (flat spot, SL/TP off); flip + SHORT at 09:59
        # is ignored because a position is still open.
        times = _minrange("09:40", "10:14")
        day_df = _day_df("2025-03-10", times)
        bars = _bars([
            {"bar_start": "09:30:00", "close_minute": "09:44:00",
             "long_sig": True, "regime_bull": True},
            {"bar_start": "09:45:00", "close_minute": "09:59:00",
             "short_sig": True, "regime_bull": False, "regime_flip": True},
        ])
        trades, _ = run_one_day(day_df, bars,
                                _ctx(sl_points=0, tp_points=0,
                                     square_off_time="10:14:00"))
        assert len(trades) == 1
        assert trades[0].direction == "LONG"
        assert trades[0].exit_reason == "TIME"

    def test_filter_blocks_against_regime_but_off_allows(self):
        times = _minrange("09:40", "10:00")
        day_df = _day_df("2025-03-10", times)
        # bullish cross while regime is BEAR
        bars = _bars([{"bar_start": "09:30:00", "close_minute": "09:44:00",
                       "long_sig": True, "regime_bull": False}])
        blocked, _ = run_one_day(day_df, bars, _ctx(use_filter=True))
        assert len(blocked) == 0
        allowed, _ = run_one_day(day_df, bars, _ctx(use_filter=False))
        assert len(allowed) == 1 and allowed[0].direction == "LONG"

    def test_distance_gate_blocks_entry(self):
        times = _minrange("09:40", "10:00")
        day_df = _day_df("2025-03-10", times)
        bars = _bars([{"bar_start": "09:30:00", "close_minute": "09:44:00",
                       "long_sig": True, "regime_bull": True, "within_dist": False}])
        trades, _ = run_one_day(day_df, bars, _ctx())
        assert len(trades) == 0

    def test_signal_outside_window_blocked(self):
        times = _minrange("09:40", "10:00")
        day_df = _day_df("2025-03-10", times)
        bars = _bars([{"bar_start": "09:15:00", "close_minute": "09:44:00",
                       "long_sig": True, "regime_bull": True}])
        trades, _ = run_one_day(day_df, bars, _ctx(window_start="09:30:00"))
        assert len(trades) == 0


# --------------------------------------------------------------------------- #
#  Reporting                                                                  #
# --------------------------------------------------------------------------- #

class TestReporting:
    def test_summary_and_dataframe(self):
        times = _minrange("09:40", "10:00")
        day_df = _day_df("2025-03-10", times,
                         spot_over={"09:50:00": 24525, "09:51:00": 24525},
                         sell_shift_over={"09:51:00": -10.0})
        bars = _bars([{"bar_start": "09:30:00", "close_minute": "09:44:00",
                       "long_sig": True, "regime_bull": True}])
        trades, _ = run_one_day(day_df, bars, _ctx())
        for tr in trades:
            tr.running_equity_inr = 200000 + tr.pnl_inr
        s = summarize_metrics(trades, 200000)
        assert s["total_trades"] == 1 and s["wins"] == 1
        df = trades_to_dataframe(trades)
        assert {"sell_strike", "buy_entry", "exit_reason"} <= set(df.columns)
