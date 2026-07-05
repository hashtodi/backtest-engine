"""Tests for engine/pcr_momentum_backtest.py."""
from datetime import date, datetime as _dt, timedelta

import pandas as pd
import pytest

from engine.pcr_momentum_backtest import (
    DEFAULT_FORCE_EXIT_TIME,
    DEFAULT_PCR_FIRST_TIME,
    DEFAULT_PCR_SECOND_TIME,
    DayContext,
    PcrMomentumTrade,
    compute_pcr,
    pcr_signal,
    run_one_day,
    run_backtest,
    summarize_metrics,
    trades_to_dataframe,
)


# --------------------------------------------------------------------------- #
#  Synthetic row helpers                                                      #
# --------------------------------------------------------------------------- #

def _row(date_str, time, opt_type, offset, strike, close,
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
        "open": open_,
        "close": close,
        "oi": oi,
        "_time": time,
        "_date": date_str,
    }


def _grid_at_time(date_str, time, atm=24500.0, ce_oi=10000, pe_oi=10000,
                  ce_close=100.0, pe_close=100.0, spot=24500.0):
    """Build all 21 strike rows (offsets -10..+10) for both CE and PE at one timestamp.

    CE OI and PE OI are constant across offsets within a side for easy PCR math.
    """
    rows = []
    for ot in ["CE", "PE"]:
        for off in range(-10, 11):
            strike = atm + off * 50
            close = ce_close if ot == "CE" else pe_close
            oi = ce_oi if ot == "CE" else pe_oi
            rows.append(_row(date_str, time, ot, off, strike, close,
                             oi=oi, spot=spot))
    return rows


def _full_day_frame(
    date_str="2025-03-10",
    atm=24500.0,
    spot=24500.0,
    # PCR controls: per-side OI at each strike. PCR sums 10 strikes each side.
    ce_oi_first=10000, pe_oi_first=10000,
    ce_oi_second=10000, pe_oi_second=15000,  # default: PCR rises (puts grow)
    pcr_first_time=DEFAULT_PCR_FIRST_TIME,
    pcr_second_time=DEFAULT_PCR_SECOND_TIME,
    force_exit_time=DEFAULT_FORCE_EXIT_TIME,
    # Spread leg pricing at fill and force-exit (defaults yield zero P&L).
    sell_offset_abs=2,
    buy_offset_abs=6,
    sell_entry=50.0,
    buy_entry=20.0,
    sell_exit=50.0,
    buy_exit=20.0,
    # Optional intraday bar (CLOSE prices) so the SL/TP scanner has something
    # to trigger on. Engine detects at T, fills at T+1 OPEN.
    intraday_time=None,           # e.g. "11:00:00"
    intraday_sell_close=None,
    intraday_buy_close=None,
    side_expected="PE",
):
    """Build a day frame with: first snapshot, second snapshot, fill bar (T+1),
    and a force-exit bar. The spread legs' OPEN at the fill bar and CLOSE at
    the force-exit bar are tuned via the sell_entry/buy_entry/sell_exit/buy_exit
    args (per contract, in INR/pts)."""
    rows = []

    # First snapshot
    rows.extend(_grid_at_time(date_str, pcr_first_time, atm=atm,
                              ce_oi=ce_oi_first, pe_oi=pe_oi_first,
                              spot=spot))
    # Second snapshot
    rows.extend(_grid_at_time(date_str, pcr_second_time, atm=atm,
                              ce_oi=ce_oi_second, pe_oi=pe_oi_second,
                              spot=spot))

    # Fill bar = pcr_second_time + 1m. Override the two leg OPEN prices.
    fill_time = (_dt.strptime(pcr_second_time, "%H:%M:%S")
                 + timedelta(minutes=1)).strftime("%H:%M:%S")
    fill_rows = _grid_at_time(date_str, fill_time, atm=atm,
                              ce_oi=ce_oi_second, pe_oi=pe_oi_second,
                              spot=spot)
    sell_off_signed = sell_offset_abs if side_expected == "CE" else -sell_offset_abs
    buy_off_signed = buy_offset_abs if side_expected == "CE" else -buy_offset_abs
    for r in fill_rows:
        if r["option_type"] == side_expected and r["strike_offset"] == sell_off_signed:
            r["open"] = sell_entry
            r["close"] = sell_entry
        elif r["option_type"] == side_expected and r["strike_offset"] == buy_off_signed:
            r["open"] = buy_entry
            r["close"] = buy_entry
    rows.extend(fill_rows)

    # Optional intraday bar for SL/TP detection (T) + its T+1 fill bar.
    if intraday_time is not None:
        intra_rows = _grid_at_time(date_str, intraday_time, atm=atm,
                                   ce_oi=ce_oi_second, pe_oi=pe_oi_second,
                                   spot=spot)
        for r in intra_rows:
            if r["option_type"] == side_expected and r["strike_offset"] == sell_off_signed and intraday_sell_close is not None:
                r["close"] = intraday_sell_close
            elif r["option_type"] == side_expected and r["strike_offset"] == buy_off_signed and intraday_buy_close is not None:
                r["close"] = intraday_buy_close
        rows.extend(intra_rows)

        intra_fill_time = (_dt.strptime(intraday_time, "%H:%M:%S")
                           + timedelta(minutes=1)).strftime("%H:%M:%S")
        intra_fill_rows = _grid_at_time(date_str, intra_fill_time, atm=atm,
                                        ce_oi=ce_oi_second, pe_oi=pe_oi_second,
                                        spot=spot)
        # Fill OPEN matches the intraday CLOSE (no slippage in synthetic frame).
        for r in intra_fill_rows:
            if r["option_type"] == side_expected and r["strike_offset"] == sell_off_signed and intraday_sell_close is not None:
                r["open"] = intraday_sell_close
                r["close"] = intraday_sell_close
            elif r["option_type"] == side_expected and r["strike_offset"] == buy_off_signed and intraday_buy_close is not None:
                r["open"] = intraday_buy_close
                r["close"] = intraday_buy_close
        rows.extend(intra_fill_rows)

    # Force-exit bar. Override the two leg CLOSE prices.
    forced_rows = _grid_at_time(date_str, force_exit_time, atm=atm,
                                ce_oi=ce_oi_second, pe_oi=pe_oi_second,
                                spot=spot)
    for r in forced_rows:
        if r["option_type"] == side_expected and r["strike_offset"] == sell_off_signed:
            r["close"] = sell_exit
        elif r["option_type"] == side_expected and r["strike_offset"] == buy_off_signed:
            r["close"] = buy_exit
    rows.extend(forced_rows)

    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
#  compute_pcr                                                                #
# --------------------------------------------------------------------------- #

class TestComputePcr:
    def test_simple_ratio(self):
        rows = _grid_at_time("2025-03-10", "09:20:00", ce_oi=10000, pe_oi=15000)
        df = pd.DataFrame(rows)
        out = compute_pcr(df)
        assert out is not None
        pcr, ce_sum, pe_sum = out
        # 10 CE strikes (offsets 1..10) at 10000 each; same for PE.
        assert ce_sum == pytest.approx(100000.0)
        assert pe_sum == pytest.approx(150000.0)
        assert pcr == pytest.approx(1.5)

    def test_returns_none_when_no_ce(self):
        # Build PE-only frame (all CE OI = 0 effectively by removing them).
        rows = _grid_at_time("2025-03-10", "09:20:00", ce_oi=0, pe_oi=10000)
        df = pd.DataFrame(rows)
        out = compute_pcr(df)
        assert out is None

    def test_excludes_itm_strikes(self):
        # ITM rows (offset >0 for PE, <0 for CE) must NOT contribute.
        rows = _grid_at_time("2025-03-10", "09:20:00", ce_oi=1000, pe_oi=1000)
        # Inject huge ITM OI that should be ignored.
        for r in rows:
            if r["option_type"] == "CE" and r["strike_offset"] < 0:
                r["oi"] = 999999  # ITM CE -- ignored
            if r["option_type"] == "PE" and r["strike_offset"] > 0:
                r["oi"] = 999999  # ITM PE -- ignored
        df = pd.DataFrame(rows)
        pcr, ce_sum, pe_sum = compute_pcr(df)
        assert ce_sum == pytest.approx(10000.0)
        assert pe_sum == pytest.approx(10000.0)
        assert pcr == pytest.approx(1.0)

    def test_n_each_side_restricts_window(self):
        rows = _grid_at_time("2025-03-10", "09:20:00", ce_oi=1000, pe_oi=1000)
        df = pd.DataFrame(rows)
        # n=3 -> sum only 3 strikes each side -> 3000 each, PCR=1.
        pcr, ce_sum, pe_sum = compute_pcr(df, n_each_side=3)
        assert ce_sum == pytest.approx(3000.0)
        assert pe_sum == pytest.approx(3000.0)
        assert pcr == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
#  pcr_signal                                                                 #
# --------------------------------------------------------------------------- #

class TestPcrSignal:
    def test_pe_side_when_above_threshold_and_rising(self):
        side, cs, cm = pcr_signal(1.2, 1.4, pcr_threshold=1.0, min_pcr_delta=0.1)
        assert side == "PE"
        assert cs is True and cm is True

    def test_ce_side_when_below_threshold_and_falling(self):
        side, cs, cm = pcr_signal(0.8, 0.6, pcr_threshold=1.0, min_pcr_delta=0.1)
        assert side == "CE"
        assert cs is True and cm is True

    def test_pe_side_blocked_when_pcr_falling(self):
        # Above threshold but PCR drops -- no trade.
        side, cs, cm = pcr_signal(1.5, 1.4, pcr_threshold=1.0, min_pcr_delta=0.1)
        assert side is None
        assert cs is True
        assert cm is False

    def test_ce_side_blocked_when_pcr_rising(self):
        side, cs, cm = pcr_signal(0.8, 0.9, pcr_threshold=1.0, min_pcr_delta=0.1)
        assert side is None
        assert cs is True
        assert cm is False

    def test_pe_side_blocked_when_delta_too_small(self):
        side, cs, cm = pcr_signal(1.2, 1.25, pcr_threshold=1.0, min_pcr_delta=0.1)
        assert side is None
        assert cs is True
        assert cm is False

    def test_skipped_when_pcr_exactly_equals_threshold(self):
        side, cs, cm = pcr_signal(1.0, 1.5, pcr_threshold=1.0, min_pcr_delta=0.1)
        assert side is None
        assert cs is False
        assert cm is False

    def test_min_delta_zero_allows_direction_only(self):
        side, _, _ = pcr_signal(1.2, 1.2001, pcr_threshold=1.0, min_pcr_delta=0.0)
        assert side == "PE"

    def test_min_delta_boundary_inclusive(self):
        # delta exactly == min_pcr_delta should pass (>= comparison).
        side, _, cm = pcr_signal(1.2, 1.3, pcr_threshold=1.0, min_pcr_delta=0.1)
        assert side == "PE"
        assert cm is True


# --------------------------------------------------------------------------- #
#  run_one_day                                                                #
# --------------------------------------------------------------------------- #

class TestRunOneDay:
    def _ctx(self, **overrides):
        defaults = dict(
            date=date(2025, 3, 10),
            pcr_first_time=DEFAULT_PCR_FIRST_TIME,
            pcr_second_time=DEFAULT_PCR_SECOND_TIME,
            force_exit_time=DEFAULT_FORCE_EXIT_TIME,
            lots=1,
            sell_offset_abs=2,
            buy_offset_abs=6,
            tp_inr=0.0,  # disabled -- force a TIME exit by default
            sl_inr=0.0,
        )
        defaults.update(overrides)
        return DayContext(**defaults)

    def test_pe_side_full_trade_zero_pnl(self):
        df = _full_day_frame(
            ce_oi_first=10000, pe_oi_first=12000,   # PCR_1 = 1.2
            ce_oi_second=10000, pe_oi_second=14000, # PCR_2 = 1.4 (delta = +0.2)
            sell_entry=50.0, buy_entry=20.0,
            sell_exit=50.0, buy_exit=20.0,
            side_expected="PE",
        )
        ctx = self._ctx()
        t = run_one_day(df, ctx)
        assert t.skip_reason is None
        assert t.side == "PE"
        assert t.exit_reason == "TIME"
        assert t.pcr_first == pytest.approx(1.2)
        assert t.pcr_second == pytest.approx(1.4)
        assert t.pcr_delta == pytest.approx(0.2)
        assert t.net_credit_pts == pytest.approx(30.0)  # 50 - 20
        assert t.pnl_inr == pytest.approx(0.0)

    def test_ce_side_full_trade(self):
        df = _full_day_frame(
            ce_oi_first=15000, pe_oi_first=10000,   # PCR_1 = 0.667
            ce_oi_second=20000, pe_oi_second=10000, # PCR_2 = 0.5 (delta = -0.167)
            sell_entry=60.0, buy_entry=25.0,
            sell_exit=60.0, buy_exit=25.0,
            side_expected="CE",
        )
        ctx = self._ctx()
        t = run_one_day(df, ctx)
        assert t.skip_reason is None
        assert t.side == "CE"
        assert t.exit_reason == "TIME"

    def test_skips_when_momentum_insufficient(self):
        df = _full_day_frame(
            ce_oi_first=10000, pe_oi_first=12000,    # PCR_1 = 1.2
            ce_oi_second=10000, pe_oi_second=12100,  # PCR_2 = 1.21 -- delta 0.01
            side_expected="PE",
        )
        # Pin min_pcr_delta high enough that 0.01 fails the filter, regardless
        # of the engine default.
        ctx = self._ctx(min_pcr_delta=0.1)
        t = run_one_day(df, ctx)
        assert t.skip_reason == "momentum_insufficient"
        assert t.side == ""

    def test_skips_when_pcr_equals_threshold(self):
        df = _full_day_frame(
            ce_oi_first=10000, pe_oi_first=10000,    # PCR_1 = 1.0
            ce_oi_second=10000, pe_oi_second=15000,
            side_expected="PE",
        )
        ctx = self._ctx()
        t = run_one_day(df, ctx)
        assert t.skip_reason == "pcr_at_threshold"

    def test_skips_when_first_bar_missing(self):
        df = _full_day_frame(side_expected="PE")
        df = df[df["_time"] != DEFAULT_PCR_FIRST_TIME]
        ctx = self._ctx()
        t = run_one_day(df, ctx)
        assert t.skip_reason == "no_pcr_first_bar"

    def test_tp_exit_with_profitable_premium_decay(self):
        # PE side. Intraday bar at 11:00 shows short PE dropping 10 pts.
        # Live P&L at 11:00 = (50 - 40) pts * 65 = +650 -> trips TP=500.
        df = _full_day_frame(
            ce_oi_first=10000, pe_oi_first=12000,
            ce_oi_second=10000, pe_oi_second=14000,
            sell_entry=50.0, buy_entry=20.0,
            intraday_time="11:00:00",
            intraday_sell_close=40.0, intraday_buy_close=20.0,
            sell_exit=50.0, buy_exit=20.0,  # force-exit prices (unused: TP fires first)
            side_expected="PE",
        )
        ctx = self._ctx(tp_inr=500.0, sl_inr=0.0)
        t = run_one_day(df, ctx)
        assert t.skip_reason is None
        assert t.exit_reason == "TP"
        assert t.exit_signal_time == "11:00"
        assert t.exit_time == "11:01"
        assert t.pnl_inr > 0

    def test_sl_exit_when_premium_expands(self):
        # Short premium widens by 20 pts at 11:00 -> live P&L = -20 * 65 = -1300 -> SL=500.
        df = _full_day_frame(
            ce_oi_first=10000, pe_oi_first=12000,
            ce_oi_second=10000, pe_oi_second=14000,
            sell_entry=50.0, buy_entry=20.0,
            intraday_time="11:00:00",
            intraday_sell_close=70.0, intraday_buy_close=20.0,
            sell_exit=50.0, buy_exit=20.0,
            side_expected="PE",
        )
        ctx = self._ctx(tp_inr=0.0, sl_inr=500.0)
        t = run_one_day(df, ctx)
        assert t.skip_reason is None
        assert t.exit_reason == "SL"
        assert t.pnl_inr < 0


# --------------------------------------------------------------------------- #
#  run_backtest end-to-end                                                    #
# --------------------------------------------------------------------------- #

class TestRunBacktest:
    def test_multi_day_aggregates(self):
        cfg = {
            "expiry": {"expiry_type": "WEEK", "expiry_code": 1},
            "entry": {
                "pcr_first_time": "09:20",
                "pcr_second_time": "10:00",
                "pcr_threshold": 1.0,
                "min_pcr_delta": 0.1,
                "pcr_strikes_each_side": 10,
            },
            "structure": {"sell_offset_abs": 2, "buy_offset_abs": 6, "lots": 1},
            "exit": {"force_exit_time": "15:00", "tp_inr": 0.0, "sl_inr": 0.0},
            "sizing": {"reference_capital": 200000},
            "backtest_start": "2025-03-10",
            "backtest_end": "2025-03-11",
        }
        day1 = _full_day_frame(
            date_str="2025-03-10",
            ce_oi_first=10000, pe_oi_first=12000,
            ce_oi_second=10000, pe_oi_second=14000,
            sell_entry=50.0, buy_entry=20.0,
            sell_exit=50.0, buy_exit=20.0,
            side_expected="PE",
        )
        day2 = _full_day_frame(
            date_str="2025-03-11",
            ce_oi_first=15000, pe_oi_first=10000,
            ce_oi_second=20000, pe_oi_second=10000,
            sell_entry=60.0, buy_entry=25.0,
            sell_exit=60.0, buy_exit=25.0,
            side_expected="CE",
        )
        df = pd.concat([day1, day2], ignore_index=True)
        result = run_backtest(df, cfg)
        trades = result["trades"]
        assert len(trades) == 2
        sides = [t.side for t in trades]
        assert sides == ["PE", "CE"]
        for t in trades:
            assert t.skip_reason is None
            assert t.exit_reason == "TIME"

    def test_summary_and_csv_shape(self):
        cfg = {
            "expiry": {"expiry_type": "WEEK", "expiry_code": 1},
            "entry": {
                "pcr_first_time": "09:20",
                "pcr_second_time": "10:00",
                "pcr_threshold": 1.0,
                "min_pcr_delta": 0.1,
            },
            "structure": {"sell_offset_abs": 2, "buy_offset_abs": 6, "lots": 1},
            "exit": {"force_exit_time": "15:00", "tp_inr": 0.0, "sl_inr": 0.0},
            "sizing": {"reference_capital": 200000},
            "backtest_start": "2025-03-10",
            "backtest_end": "2025-03-10",
        }
        df = _full_day_frame(
            ce_oi_first=10000, pe_oi_first=12000,
            ce_oi_second=10000, pe_oi_second=14000,
            sell_entry=50.0, buy_entry=20.0,
            sell_exit=50.0, buy_exit=20.0,
            side_expected="PE",
        )
        result = run_backtest(df, cfg)
        trades = result["trades"]
        summary = summarize_metrics(trades, 200000.0)
        assert summary["trades_placed"] == 1
        assert summary["pe_trades"] == 1
        assert summary["ce_trades"] == 0
        out_df = trades_to_dataframe(trades)
        # Spread leg columns are flattened into the trades DataFrame.
        assert "pe_short_strike" in out_df.columns
        assert "pe_long_strike" in out_df.columns
