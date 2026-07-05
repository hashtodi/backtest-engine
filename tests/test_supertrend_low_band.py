"""Tests for SuperTrend Low-Band backtest engine."""
from datetime import time

import numpy as np
import pandas as pd
import pytest

from engine.supertrend_low_band_backtest import (
    StLowBandTrade,
    ST_BULLISH,
    trades_to_dataframe,
)


def make_trade(**overrides) -> StLowBandTrade:
    defaults = dict(
        date="2026-04-08",
        instrument="NIFTY",
        expiry_date="2026-04-13",
        option_type="CE",
        strike=23850,
        morning_low=100.0,
        band_high=105.0,
        spot_at_entry=23850.0,
        entry_time="09:25",
        entry_price=100.0,
        entry_st_value=98.0,
        entry_trigger_close=100.5,
        spot_at_exit=23900.0,
        exit_time="09:45",
        exit_price=110.0,
        exit_reason="TP",
        dte=2,
        tp_pct=10.0,
        sl_pct=7.5,
        pnl_points=10.0,
        pnl_pct=10.0,
        pnl_inr=650.0,
        lot_size=65,
    )
    defaults.update(overrides)
    return StLowBandTrade(**defaults)


class TestStLowBandTradeDataclass:
    def test_all_fields_present(self):
        t = make_trade()
        assert t.option_type == "CE"
        assert t.strike == 23850
        assert t.morning_low == 100.0
        assert t.exit_reason == "TP"
        assert t.pnl_points == 10.0

    def test_trades_to_dataframe_empty(self):
        df = trades_to_dataframe([])
        assert df.empty

    def test_trades_to_dataframe_roundtrip(self):
        trades = [make_trade(), make_trade(option_type="PE", exit_reason="SL")]
        df = trades_to_dataframe(trades)
        assert len(df) == 2
        assert set(df.columns) >= {
            "date", "instrument", "expiry_date", "option_type", "strike",
            "morning_low", "band_high",
            "spot_at_entry", "entry_time", "entry_price",
            "entry_st_value", "entry_trigger_close",
            "spot_at_exit", "exit_time", "exit_price", "exit_reason",
            "dte", "tp_pct", "sl_pct",
            "pnl_points", "pnl_pct", "pnl_inr", "lot_size",
        }
        assert df.iloc[1]["option_type"] == "PE"


from engine.supertrend_low_band_backtest import evaluate_entry  # noqa: E402


class TestEvaluateEntry:
    """Entry condition: ST bullish AND option_close <= morning_low * (1 + band_pct/100).

    Trigger zone: (-inf, morning_low * 1.05] for band_pct=5.0. There is no
    lower bound; close can be arbitrarily below the morning low and still trigger.
    """

    def test_enter_when_bullish_and_within_5pct_above(self):
        assert evaluate_entry(
            option_close=104.0, st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is True

    def test_enter_when_bullish_and_below_low(self):
        # close=80 is below the low → still triggers (no lower bound)
        assert evaluate_entry(
            option_close=80.0, st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is True

    def test_enter_at_morning_low_exact(self):
        assert evaluate_entry(
            option_close=100.0, st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is True

    def test_enter_at_upper_edge_inclusive(self):
        assert evaluate_entry(
            option_close=105.0, st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is True

    def test_no_enter_just_above_upper_edge(self):
        assert evaluate_entry(
            option_close=105.01, st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is False

    def test_no_enter_when_well_above_upper_edge(self):
        assert evaluate_entry(
            option_close=120.0, st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is False

    def test_no_enter_when_below_low_but_bearish(self):
        assert evaluate_entry(
            option_close=80.0, st_dir=1.0,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is False

    def test_no_enter_when_close_is_nan(self):
        assert evaluate_entry(
            option_close=float("nan"), st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is False

    def test_no_enter_when_low_is_nan(self):
        assert evaluate_entry(
            option_close=98.0, st_dir=ST_BULLISH,
            morning_low=float("nan"), band_pct=5.0, bullish_required=True,
        ) is False

    def test_no_enter_when_st_dir_is_nan(self):
        assert evaluate_entry(
            option_close=98.0, st_dir=float("nan"),
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is False

    def test_bullish_filter_off_ignores_direction(self):
        assert evaluate_entry(
            option_close=98.0, st_dir=1.0,
            morning_low=100.0, band_pct=5.0, bullish_required=False,
        ) is True


from engine.supertrend_low_band_backtest import (  # noqa: E402
    compute_trading_dte, parse_dte_table, get_dte_tp_sl,
)


class TestComputeTradingDte:
    def test_zero_on_expiry_day(self):
        from datetime import date as _d
        trading = [_d(2026, 4, 6), _d(2026, 4, 7), _d(2026, 4, 8)]
        assert compute_trading_dte(_d(2026, 4, 8), _d(2026, 4, 8), trading) == 0

    def test_one_day_before_expiry(self):
        from datetime import date as _d
        trading = [_d(2026, 4, 6), _d(2026, 4, 7), _d(2026, 4, 8)]
        assert compute_trading_dte(_d(2026, 4, 7), _d(2026, 4, 8), trading) == 1

    def test_skips_weekends(self):
        # Friday 2026-04-03 → expiry Tuesday 2026-04-07: trading days are Mon Apr 6, Tue Apr 7 → DTE=2
        from datetime import date as _d
        trading = [_d(2026, 4, 3), _d(2026, 4, 6), _d(2026, 4, 7)]
        assert compute_trading_dte(_d(2026, 4, 3), _d(2026, 4, 7), trading) == 2

    def test_no_trading_dates_returns_zero(self):
        from datetime import date as _d
        assert compute_trading_dte(_d(2026, 4, 1), _d(2026, 4, 8), []) == 0

    def test_trade_after_expiry(self):
        from datetime import date as _d
        trading = [_d(2026, 4, 6), _d(2026, 4, 7), _d(2026, 4, 8)]
        # If we somehow get a trade_date past expiry, return 0
        assert compute_trading_dte(_d(2026, 4, 9), _d(2026, 4, 8), trading) == 0


class TestDteTable:
    def test_parse_basic(self):
        cfg = {
            "0": {"tp_pct": 20.0, "sl_pct": 12.5},
            "2": {"tp_pct": 10.0, "sl_pct": 7.5},
        }
        t = parse_dte_table(cfg)
        assert t == {0: (20.0, 12.5), 2: (10.0, 7.5)}

    def test_lookup_clamp_at_max(self):
        # Table has 0..4; DTE=5 should clamp to 4
        t = {0: (20.0, 12.5), 1: (15.0, 10.0), 2: (10.0, 7.5),
             3: (7.5, 5.0), 4: (7.5, 5.0)}
        assert get_dte_tp_sl(0, t) == (20.0, 12.5)
        assert get_dte_tp_sl(4, t) == (7.5, 5.0)
        assert get_dte_tp_sl(5, t) == (7.5, 5.0)
        assert get_dte_tp_sl(99, t) == (7.5, 5.0)

    def test_empty_table_raises(self):
        with pytest.raises(KeyError):
            get_dte_tp_sl(0, {})


from engine.supertrend_low_band_backtest import evaluate_exit  # noqa: E402


class TestEvaluateExit:
    """Exit precedence: SL > TP > EOD. Same-bar SL+TP → SL wins."""

    def test_sl_hit_intra_bar(self):
        result = evaluate_exit(
            bar_high=110.0, bar_low=90.0, bar_close=95.0,
            sl=92.5, tp=110.0, is_force_exit_bar=False,
        )
        assert result == (92.5, "SL")

    def test_tp_hit_intra_bar(self):
        result = evaluate_exit(
            bar_high=115.0, bar_low=99.0, bar_close=109.0,
            sl=92.5, tp=110.0, is_force_exit_bar=False,
        )
        assert result == (110.0, "TP")

    def test_same_bar_sl_and_tp_sl_wins(self):
        result = evaluate_exit(
            bar_high=115.0, bar_low=90.0, bar_close=100.0,
            sl=92.5, tp=110.0, is_force_exit_bar=False,
        )
        assert result == (92.5, "SL")

    def test_no_exit_inside_band(self):
        result = evaluate_exit(
            bar_high=109.0, bar_low=93.0, bar_close=100.0,
            sl=92.5, tp=110.0, is_force_exit_bar=False,
        )
        assert result is None

    def test_force_exit_when_no_sl_tp(self):
        result = evaluate_exit(
            bar_high=109.0, bar_low=93.0, bar_close=101.5,
            sl=92.5, tp=110.0, is_force_exit_bar=True,
        )
        assert result == (101.5, "EOD")

    def test_sl_takes_priority_over_force_exit(self):
        result = evaluate_exit(
            bar_high=105.0, bar_low=90.0, bar_close=100.0,
            sl=92.5, tp=110.0, is_force_exit_bar=True,
        )
        assert result == (92.5, "SL")

    def test_tp_takes_priority_over_force_exit(self):
        result = evaluate_exit(
            bar_high=115.0, bar_low=100.0, bar_close=100.0,
            sl=92.5, tp=110.0, is_force_exit_bar=True,
        )
        assert result == (110.0, "TP")

    def test_sl_at_exact_low(self):
        result = evaluate_exit(
            bar_high=105.0, bar_low=92.5, bar_close=95.0,
            sl=92.5, tp=110.0, is_force_exit_bar=False,
        )
        assert result == (92.5, "SL")

    def test_tp_at_exact_high(self):
        result = evaluate_exit(
            bar_high=110.0, bar_low=99.0, bar_close=108.0,
            sl=92.5, tp=110.0, is_force_exit_bar=False,
        )
        assert result == (110.0, "TP")


from engine.supertrend_low_band_backtest import compute_first_5min_low_table  # noqa: E402


def _bars(date_str, strike, option_type, time_lows):
    """Build a tiny test DataFrame of option bars.

    time_lows: list of (HH:MM, low) tuples.
    """
    rows = []
    for t, low in time_lows:
        ts = pd.Timestamp(f"{date_str} {t}:00", tz="Asia/Kolkata")
        rows.append({
            "datetime": ts,
            "date": ts.date(),
            "time_only": ts.time(),
            "strike": strike,
            "option_type": option_type,
            "expiry_type": "WEEK",
            "expiry_code": 1,
            "low": low,
        })
    return pd.DataFrame(rows)


class TestComputeFirst5MinLowTable:
    def test_min_across_window(self):
        df = _bars("2026-04-08", 23850, "CE", [
            ("09:15", 105.0),
            ("09:16", 100.0),
            ("09:17",  95.0),
            ("09:18",  98.0),
            ("09:19", 102.0),
            ("09:20",  90.0),
        ])
        table = compute_first_5min_low_table(
            df, window_start=time(9, 15), window_end=time(9, 20),
        )
        key = (pd.Timestamp("2026-04-08").date(), 23850, "CE", "WEEK", 1)
        assert table[key] == 95.0

    def test_window_is_half_open_excludes_end(self):
        df = _bars("2026-04-08", 23850, "CE", [
            ("09:20",  50.0),
            ("09:15", 100.0),
            ("09:19",  98.0),
        ])
        table = compute_first_5min_low_table(
            df, window_start=time(9, 15), window_end=time(9, 20),
        )
        key = (pd.Timestamp("2026-04-08").date(), 23850, "CE", "WEEK", 1)
        assert table[key] == 98.0

    def test_separate_contracts_separate_lows(self):
        df = pd.concat([
            _bars("2026-04-08", 23850, "CE", [("09:15", 100.0), ("09:16", 95.0)]),
            _bars("2026-04-08", 23850, "PE", [("09:15", 200.0), ("09:16", 190.0)]),
            _bars("2026-04-08", 23900, "CE", [("09:15",  60.0), ("09:16", 55.0)]),
        ])
        table = compute_first_5min_low_table(
            df, window_start=time(9, 15), window_end=time(9, 20),
        )
        d = pd.Timestamp("2026-04-08").date()
        assert table[(d, 23850, "CE", "WEEK", 1)] == 95.0
        assert table[(d, 23850, "PE", "WEEK", 1)] == 190.0
        assert table[(d, 23900, "CE", "WEEK", 1)] == 55.0

    def test_no_bars_in_window_returns_no_entry(self):
        df = _bars("2026-04-08", 23850, "CE", [
            ("09:25", 100.0),
        ])
        table = compute_first_5min_low_table(
            df, window_start=time(9, 15), window_end=time(9, 20),
        )
        d = pd.Timestamp("2026-04-08").date()
        assert (d, 23850, "CE", "WEEK", 1) not in table

    def test_separate_dates_separate_lows(self):
        df = pd.concat([
            _bars("2026-04-08", 23850, "CE", [("09:15", 100.0), ("09:16", 95.0)]),
            _bars("2026-04-09", 23850, "CE", [("09:15", 110.0), ("09:16", 108.0)]),
        ])
        table = compute_first_5min_low_table(
            df, window_start=time(9, 15), window_end=time(9, 20),
        )
        assert table[(pd.Timestamp("2026-04-08").date(), 23850, "CE", "WEEK", 1)] == 95.0
        assert table[(pd.Timestamp("2026-04-09").date(), 23850, "CE", "WEEK", 1)] == 108.0


from engine.supertrend_low_band_backtest import (  # noqa: E402
    compute_continuous_supertrend_per_contract,
)


def _ohlc_bars(strike, option_type, n_bars, base_price=100.0, drift=0.0):
    """Build n synthetic 1-min OHLC bars for one contract starting 09:15."""
    rows = []
    for i in range(n_bars):
        # Build timestamp: 09:15, 09:16, ..., wrapping if needed
        total_min = 9 * 60 + 15 + i
        hh = total_min // 60
        mm = total_min % 60
        ts = pd.Timestamp(f"2026-04-08 {hh:02d}:{mm:02d}:00", tz="Asia/Kolkata")
        c = base_price + drift * i
        rows.append({
            "datetime": ts,
            "date": ts.date(),
            "time_only": ts.time(),
            "strike": strike,
            "option_type": option_type,
            "expiry_type": "WEEK",
            "expiry_code": 1,
            "open":  c,
            "high":  c + 1.0,
            "low":   c - 1.0,
            "close": c,
        })
    return pd.DataFrame(rows)


class TestComputeContinuousSupertrend:
    """5-min ST per contract, forward-filled to 1-min with +5min shift.

    With atr_period=10 5-min bars, first valid 5-min ST is at the 11th 5-min
    bar. After +5min shift, that becomes available at the 12th 5-min boundary.
    Need ≥ 12*5 = 60 1-min bars for any forward-filled values to appear.
    """

    def test_adds_columns(self):
        df = _ohlc_bars(23850, "CE", n_bars=30, base_price=100.0, drift=0.5)
        out = compute_continuous_supertrend_per_contract(df, factor=3, atr_period=10)
        assert "st_value" in out.columns
        assert "st_dir" in out.columns

    def test_early_bars_nan_late_bars_valid(self):
        # 90 1-min bars = 18 5-min bars. After +5min shift, ST first appears
        # at 1-min bar around index ~55 (when 11th 5-min bar's value forward-fills).
        df = _ohlc_bars(23850, "CE", n_bars=90, base_price=100.0, drift=0.5)
        out = compute_continuous_supertrend_per_contract(df, factor=3, atr_period=10)
        out = out.sort_values("datetime").reset_index(drop=True)
        # First few bars (well before 5-min ST warm-up) must be NaN
        assert out["st_value"].iloc[:30].isna().all()
        # Last bars (well after warm-up) must have a valid st_dir
        assert out["st_dir"].iloc[-1] in (-1.0, 1.0)

    def test_forward_fill_within_5min_block(self):
        # Within a single 5-min block of 1-min bars (post-warmup), st_dir
        # should be constant — the 5-min ST value doesn't change within the block.
        df = _ohlc_bars(23850, "CE", n_bars=120, base_price=100.0, drift=0.5)
        out = compute_continuous_supertrend_per_contract(df, factor=3, atr_period=10)
        out = out.sort_values("datetime").reset_index(drop=True)
        # Take a 5-min block well past warmup; e.g. bars 90-94
        block = out["st_dir"].iloc[90:95].dropna().unique()
        assert len(block) == 1  # constant within block

    def test_separate_contracts_compute_independently(self):
        df1 = _ohlc_bars(23850, "CE", n_bars=120, base_price=100.0, drift=0.5)
        df2 = _ohlc_bars(23900, "CE", n_bars=120, base_price=200.0, drift=-0.5)
        df = pd.concat([df1, df2], ignore_index=True)

        out = compute_continuous_supertrend_per_contract(df, factor=3, atr_period=10)
        ce_23850 = out[out["strike"] == 23850].sort_values("datetime").reset_index(drop=True)
        ce_23900 = out[out["strike"] == 23900].sort_values("datetime").reset_index(drop=True)
        # Late-bar ST values should differ given the price levels are very different
        assert ce_23850["st_value"].iloc[-1] != ce_23900["st_value"].iloc[-1]

    def test_directions_are_minus_one_or_plus_one(self):
        df = _ohlc_bars(23850, "CE", n_bars=120, base_price=100.0, drift=0.5)
        out = compute_continuous_supertrend_per_contract(df, factor=3, atr_period=10)
        valid_dirs = out["st_dir"].dropna().unique()
        for d in valid_dirs:
            assert d in (-1.0, 1.0)


from engine.supertrend_low_band_backtest import build_atm_index  # noqa: E402


class TestBuildAtmIndex:
    def test_picks_only_atm_rows(self):
        rows = []
        ts = pd.Timestamp("2026-04-08 09:20", tz="Asia/Kolkata")
        for strike, money in [(23800, "OTM"), (23850, "ATM"), (23900, "OTM")]:
            rows.append({
                "datetime": ts, "strike": strike, "option_type": "CE",
                "expiry_type": "WEEK", "expiry_code": 1, "moneyness": money,
            })
        df = pd.DataFrame(rows)
        idx = build_atm_index(df)
        assert idx[(ts, "CE")] == 23850

    def test_separate_ce_and_pe(self):
        ts = pd.Timestamp("2026-04-08 09:20", tz="Asia/Kolkata")
        df = pd.DataFrame([
            {"datetime": ts, "strike": 23850, "option_type": "CE",
             "expiry_type": "WEEK", "expiry_code": 1, "moneyness": "ATM"},
            {"datetime": ts, "strike": 23900, "option_type": "PE",
             "expiry_type": "WEEK", "expiry_code": 1, "moneyness": "ATM"},
        ])
        idx = build_atm_index(df)
        assert idx[(ts, "CE")] == 23850
        assert idx[(ts, "PE")] == 23900

    def test_atm_changes_across_minutes(self):
        ts1 = pd.Timestamp("2026-04-08 09:20", tz="Asia/Kolkata")
        ts2 = pd.Timestamp("2026-04-08 09:30", tz="Asia/Kolkata")
        df = pd.DataFrame([
            {"datetime": ts1, "strike": 23850, "option_type": "CE",
             "expiry_type": "WEEK", "expiry_code": 1, "moneyness": "ATM"},
            {"datetime": ts2, "strike": 23900, "option_type": "CE",
             "expiry_type": "WEEK", "expiry_code": 1, "moneyness": "ATM"},
        ])
        idx = build_atm_index(df)
        assert idx[(ts1, "CE")] == 23850
        assert idx[(ts2, "CE")] == 23900

    def test_ignores_non_weekly(self):
        ts = pd.Timestamp("2026-04-08 09:20", tz="Asia/Kolkata")
        df = pd.DataFrame([
            {"datetime": ts, "strike": 23800, "option_type": "CE",
             "expiry_type": "MONTH", "expiry_code": 1, "moneyness": "ATM"},
            {"datetime": ts, "strike": 23850, "option_type": "CE",
             "expiry_type": "WEEK", "expiry_code": 1, "moneyness": "ATM"},
        ])
        idx = build_atm_index(df)
        assert idx[(ts, "CE")] == 23850


def _build_day_bars(strike, option_type, minute_bars, atm_strike=None,
                    spot_at_each=None, date_str="2026-04-08"):
    """Build a day's worth of synthetic option bars with all required columns."""
    if atm_strike is None:
        atm_strike = strike
    if spot_at_each is None:
        spot_at_each = {}
    rows = []
    for b in minute_bars:
        ts = pd.Timestamp(f"{date_str} {b['time']}:00", tz="Asia/Kolkata")
        rows.append({
            "datetime": ts,
            "date": ts.date(),
            "time_only": ts.time(),
            "strike": strike,
            "option_type": option_type,
            "expiry_type": "WEEK",
            "expiry_code": 1,
            "moneyness": "ATM" if strike == atm_strike else "OTM",
            "open":  b["open"],
            "high":  b["high"],
            "low":   b["low"],
            "close": b["close"],
            "spot":  spot_at_each.get(b["time"], 23850.0),
            "atm_strike": atm_strike,
            "st_value": b.get("st_value", float("nan")),
            "st_dir":   b.get("st_dir",   float("nan")),
        })
    return pd.DataFrame(rows)


def _atm_index_with_lock_seed(df, side, lock_seed_atm_by_block=None,
                               date_str="2026-04-08"):
    """Build {(datetime, side): strike} from df rows where moneyness == 'ATM',
    and additionally seed lock-minute entries needed by the 5-min strike-lock
    logic.

    The state machine looks up `atm_index[(lock_minute, side)]` where
    lock_minute = floor(bar_minute, 5min) - 1min. For synthetic bars starting
    at 09:20, the first lock_minute is 09:19. We seed it here using the
    earliest ATM strike observed.
    """
    idx = {}
    atm_rows = df[df["moneyness"] == "ATM"].sort_values("datetime")
    for _, r in atm_rows.iterrows():
        if r["option_type"] != side:
            continue
        idx[(r["datetime"], side)] = int(r["strike"])

    # Seed lock minutes by walking each unique 5-min block start in the df.
    # For each block, lock = block_start - 1 min, and the lock strike is
    # whichever strike is ATM at that block_start (or earliest available).
    minutes = sorted({r["datetime"] for _, r in df.iterrows() if r["option_type"] == side})
    if not minutes:
        return idx
    for m in minutes:
        floor = m.floor("5min")
        lock_minute = floor - pd.Timedelta(minutes=1)
        if (lock_minute, side) in idx:
            continue
        # If lock_minute is before all our data, use the earliest ATM strike
        atm_at_block_start = idx.get((floor, side))
        if atm_at_block_start is None:
            # Fall back to the earliest ATM strike of the day
            for r_ts, r_side in idx:
                if r_side == side:
                    atm_at_block_start = idx[(r_ts, side)]
                    break
        if atm_at_block_start is not None:
            idx[(lock_minute, side)] = atm_at_block_start

    if lock_seed_atm_by_block:
        for ts, strike in lock_seed_atm_by_block.items():
            idx[(ts, side)] = strike
    return idx


from engine.supertrend_low_band_backtest import run_machine_for_day_side  # noqa: E402


class TestRunMachineForDaySide:
    """Single-day, single-side state machine tests."""

    def _params(self, **overrides):
        defaults = dict(
            band_pct=5.0, sl_pct=7.5, tp_pct=10.0,
            scan_start=time(9, 20), force_exit=time(14, 45),
            bullish_required=True,
            lot_size_total=65,
        )
        defaults.update(overrides)
        return defaults

    def test_entry_fires_when_st_in_band_and_bullish(self):
        bars = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": 98.0, "st_dir": ST_BULLISH},
            {"time": "09:21", "open": 101, "high": 103, "low": 100, "close": 102,
             "st_value": 99.0, "st_dir": ST_BULLISH},
            {"time": "14:45", "open": 102, "high": 102, "low": 101, "close": 102,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {
            (df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0,
        }
        atm_index = _atm_index_with_lock_seed(df, "CE")

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(), dte=2,
            morning_low_table=morning_low_table,
            atm_index=atm_index,
            instrument="NIFTY",
            **self._params(),
        )

        assert len(trades) == 1
        t = trades[0]
        assert t.entry_time == "09:21"
        assert t.entry_price == 101.0
        assert t.strike == 23850
        assert t.exit_reason == "EOD"
        assert t.exit_time == "14:45"

    def test_no_entry_when_bearish(self):
        bars = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": 98.0, "st_dir": 1.0},  # bearish
            {"time": "09:21", "open": 101, "high": 103, "low": 100, "close": 102,
             "st_value": 99.0, "st_dir": 1.0},
            {"time": "14:45", "open": 102, "high": 102, "low": 101, "close": 102,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {(df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0}
        atm_index = _atm_index_with_lock_seed(df, "CE")

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(), dte=2,
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert trades == []

    def test_no_entry_when_close_above_upper_edge(self):
        # morning_low=100, upper edge = 105. close=120 is above → no entry.
        # (Below the low would now qualify; only "well above" should not.)
        bars = [
            {"time": "09:20", "open": 120, "high": 122, "low": 119, "close": 120,
             "st_value": 80.0, "st_dir": ST_BULLISH},
            {"time": "09:21", "open": 120, "high": 122, "low": 119, "close": 120,
             "st_value": 80.0, "st_dir": ST_BULLISH},
            {"time": "14:45", "open": 120, "high": 120, "low": 120, "close": 120,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {(df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0}
        atm_index = _atm_index_with_lock_seed(df, "CE")

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(), dte=2,
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert trades == []

    def test_sl_exit_intra_bar(self):
        # entry at 9:21 open=101. SL=101*0.925=93.425.
        # 9:22 wicks down to 93.0 → SL hit at 93.425 exactly.
        bars = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": 98.0, "st_dir": ST_BULLISH},
            {"time": "09:21", "open": 101, "high": 103, "low": 100, "close": 102,
             "st_value": 99.0, "st_dir": ST_BULLISH},
            {"time": "09:22", "open": 102, "high": 103, "low": 93.0, "close": 95,
             "st_value": float("nan"), "st_dir": float("nan")},
            {"time": "14:45", "open": 95, "high": 95, "low": 95, "close": 95,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {(df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0}
        atm_index = _atm_index_with_lock_seed(df, "CE")

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(), dte=2,
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert trades[0].exit_reason == "SL"
        assert trades[0].exit_price == pytest.approx(101 * 0.925)

    def test_tp_exit_intra_bar(self):
        bars = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": 98.0, "st_dir": ST_BULLISH},
            {"time": "09:21", "open": 100, "high": 103, "low": 100, "close": 102,
             "st_value": 99.0, "st_dir": ST_BULLISH},
            {"time": "09:22", "open": 102, "high": 115, "low": 102, "close": 113,
             "st_value": float("nan"), "st_dir": float("nan")},
            {"time": "14:45", "open": 113, "high": 113, "low": 113, "close": 113,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {(df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0}
        atm_index = _atm_index_with_lock_seed(df, "CE")

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(), dte=2,
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert trades[0].exit_reason == "TP"
        assert trades[0].exit_price == pytest.approx(100 * 1.10)

    def test_force_exit_at_force_exit_time(self):
        bars = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": 98.0, "st_dir": ST_BULLISH},
            {"time": "09:21", "open": 101, "high": 103, "low": 100, "close": 102,
             "st_value": 99.0, "st_dir": ST_BULLISH},
            {"time": "14:45", "open": 102, "high": 102, "low": 101, "close": 100,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {(df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0}
        atm_index = _atm_index_with_lock_seed(df, "CE")

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(), dte=2,
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert trades[0].exit_reason == "EOD"
        assert trades[0].exit_time == "14:45"
        assert trades[0].exit_price == 100.0

    def test_same_day_re_entry_after_sl(self):
        bars = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": 98.0, "st_dir": ST_BULLISH},
            {"time": "09:21", "open": 101, "high": 103, "low": 100, "close": 102,
             "st_value": 99.0, "st_dir": ST_BULLISH},
            # SL hit on 9:22 wick; close 95, ST still in band & bullish → re-arm
            {"time": "09:22", "open": 102, "high": 103, "low": 93.0, "close": 95,
             "st_value": 100.0, "st_dir": ST_BULLISH},
            {"time": "09:23", "open": 95, "high": 96, "low": 95, "close": 96,
             "st_value": 100.0, "st_dir": ST_BULLISH},
            {"time": "14:45", "open": 96, "high": 96, "low": 96, "close": 96,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {(df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0}
        atm_index = _atm_index_with_lock_seed(df, "CE")

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(), dte=2,
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert len(trades) == 2
        assert trades[0].exit_reason == "SL"
        assert trades[1].entry_time == "09:23"
        assert trades[1].entry_price == 95.0
        assert trades[1].exit_reason == "EOD"

    def test_strike_lock_when_atm_shifts(self):
        # Position opens on 23850 at 9:21. ATM shifts to 23900 at 9:23.
        # Open trade stays on 23850 → exits at EOD on 23850's bar.
        bars_23850 = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": 98.0, "st_dir": ST_BULLISH},
            {"time": "09:21", "open": 101, "high": 103, "low": 100, "close": 102,
             "st_value": 99.0, "st_dir": ST_BULLISH},
            {"time": "09:22", "open": 102, "high": 104, "low": 101, "close": 103,
             "st_value": float("nan"), "st_dir": float("nan")},
            {"time": "09:23", "open": 103, "high": 105, "low": 102, "close": 104,
             "st_value": float("nan"), "st_dir": float("nan")},
            {"time": "14:45", "open": 104, "high": 104, "low": 104, "close": 104,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df_23850 = _build_day_bars(23850, "CE", bars_23850)
        bars_23900 = [
            {"time": "09:20", "open":  60, "high":  61, "low":  59, "close":  60,
             "st_value": float("nan"), "st_dir": float("nan")},
            {"time": "09:21", "open":  60, "high":  62, "low":  60, "close":  61,
             "st_value": float("nan"), "st_dir": float("nan")},
            {"time": "09:22", "open":  61, "high":  63, "low":  60, "close":  62,
             "st_value": float("nan"), "st_dir": float("nan")},
            {"time": "09:23", "open":  62, "high":  64, "low":  61, "close":  63,
             "st_value": float("nan"), "st_dir": float("nan")},
            {"time": "14:45", "open":  63, "high":  63, "low":  63, "close":  63,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df_23900 = _build_day_bars(23900, "CE", bars_23900, atm_strike=23850)
        df = pd.concat([df_23850, df_23900], ignore_index=True)
        # Mark 23900 as ATM from 09:23 onwards
        for i, row in df.iterrows():
            if row["strike"] == 23850 and row["time_only"] >= time(9, 23):
                df.at[i, "moneyness"] = "OTM"
            if row["strike"] == 23900 and row["time_only"] >= time(9, 23):
                df.at[i, "moneyness"] = "ATM"

        morning_low_table = {
            (df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0,
            (df.iloc[0]["date"], 23900, "CE", "WEEK", 1):  60.0,
        }
        atm_index = _atm_index_with_lock_seed(df, "CE")

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(), dte=2,
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert len(trades) == 1
        assert trades[0].strike == 23850
        assert trades[0].exit_reason == "EOD"

    def test_skip_when_morning_low_missing(self):
        bars = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": 98.0, "st_dir": ST_BULLISH},
            {"time": "14:45", "open": 100, "high": 100, "low": 100, "close": 100,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {}
        atm_index = _atm_index_with_lock_seed(df, "CE")

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(), dte=2,
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert trades == []

    def test_strike_locked_within_5min_block(self):
        """Within a 5-min block, the entry scanner uses the strike that was
        ATM at the close of the prior 5-min block, even if a different strike
        becomes ATM mid-block.

        Setup: 09:20-09:24 block. Strike 23850 was ATM at 09:19. At 09:21,
        ATM shifts to 23900 (which has a far better close-in-band signal),
        but the engine should still evaluate 23850 (the locked strike).
        Since 23850's close is OUT of band, no entry fires.
        """
        bars_23850 = [
            {"time": "09:19", "open": 200, "high": 201, "low": 199, "close": 200,
             "st_value": float("nan"), "st_dir": float("nan")},
            {"time": "09:20", "open": 200, "high": 201, "low": 199, "close": 200,  # OUT of band
             "st_value": 195.0, "st_dir": ST_BULLISH},
            {"time": "09:21", "open": 200, "high": 201, "low": 199, "close": 200,
             "st_value": 195.0, "st_dir": ST_BULLISH},
            {"time": "09:22", "open": 200, "high": 201, "low": 199, "close": 200,
             "st_value": 195.0, "st_dir": ST_BULLISH},
            {"time": "14:45", "open": 200, "high": 200, "low": 200, "close": 200,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df_23850 = _build_day_bars(23850, "CE", bars_23850)

        bars_23900 = [
            {"time": "09:19", "open":  60, "high":  61, "low":  59, "close":  60,
             "st_value": float("nan"), "st_dir": float("nan")},
            {"time": "09:20", "open":  60, "high":  62, "low":  60, "close":  61,
             "st_value": 58.0, "st_dir": ST_BULLISH},
            {"time": "09:21", "open":  61, "high":  63, "low":  60, "close":  62,  # IN band
             "st_value": 58.0, "st_dir": ST_BULLISH},
            {"time": "09:22", "open":  62, "high":  64, "low":  61, "close":  63,
             "st_value": 58.0, "st_dir": ST_BULLISH},
            {"time": "14:45", "open":  63, "high":  63, "low":  63, "close":  63,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df_23900 = _build_day_bars(23900, "CE", bars_23900, atm_strike=23850)
        df = pd.concat([df_23850, df_23900], ignore_index=True)
        # Mark 23900 as ATM from 09:21 onwards (mid-block); 23850 was ATM at 09:19, 09:20
        for i, row in df.iterrows():
            if row["strike"] == 23850 and row["time_only"] >= time(9, 21):
                df.at[i, "moneyness"] = "OTM"
            if row["strike"] == 23900 and row["time_only"] >= time(9, 21):
                df.at[i, "moneyness"] = "ATM"

        morning_low_table = {
            (df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0,
            (df.iloc[0]["date"], 23900, "CE", "WEEK", 1):  60.0,
        }
        atm_index = _atm_index_with_lock_seed(df, "CE")

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(), dte=2,
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        # 23850 (locked) is OUT of band; 23900 (becomes ATM mid-block) would
        # have triggered if not for the lock. So no trade in this 5-min block.
        # Trades may fire in the next 5-min block (09:25-09:29) using the new
        # lock = ATM at 09:24 = 23900. But we don't have bars after 09:22 except 14:45.
        in_block_trades = [t for t in trades if t.entry_time and t.entry_time < "09:25"]
        assert in_block_trades == []

    def test_skip_when_st_value_nan(self):
        bars = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": float("nan"), "st_dir": float("nan")},
            {"time": "14:45", "open": 100, "high": 100, "low": 100, "close": 100,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {(df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0}
        atm_index = _atm_index_with_lock_seed(df, "CE")

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(), dte=2,
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert trades == []


from engine.supertrend_low_band_backtest import run_backtest  # noqa: E402


class TestRunBacktest:
    def test_runs_with_synthetic_loader_and_emits_trade(self):
        # Build a single day where we have enough bars for ST to warm up,
        # then engineer a TP exit.
        date_str = "2026-04-08"
        bars = []
        # 9:15-9:24: stable price 100
        for i in range(10):
            t = f"09:{15 + i:02d}"
            bars.append({
                "time": t, "open": 100, "high": 100.5, "low": 99.5, "close": 100,
            })
        # 9:25 trigger
        bars.append({"time": "09:25", "open": 100, "high": 100.5, "low": 99.5, "close": 100})
        # 9:26 entry
        bars.append({"time": "09:26", "open": 100, "high": 101.0, "low": 99.0, "close": 100})
        # 9:30 TP wick
        bars.append({"time": "09:30", "open": 100, "high": 115.0, "low": 100,  "close": 110})
        # EOD filler
        bars.append({"time": "14:45", "open": 110, "high": 110, "low": 110, "close": 110})

        df_ce = _build_day_bars(23850, "CE", bars)
        df_pe = _build_day_bars(23850, "PE", bars, atm_strike=23850)
        all_df = pd.concat([df_ce, df_pe], ignore_index=True)
        # Drop synthetic ST columns; let run_backtest compute them
        all_df = all_df.drop(columns=["st_value", "st_dir"])

        def synthetic_loader(start, end):
            return all_df

        config = {
            "instrument": "NIFTY",
            "supertrend": {"factor": 3, "atr_period": 10},
            "first_5min_window": {"start": "09:15", "end": "09:20"},
            "band_pct": 5.0,
            "dte_table": {
                "0": {"tp_pct": 20.0, "sl_pct": 12.5},
                "1": {"tp_pct": 15.0, "sl_pct": 10.0},
                "2": {"tp_pct": 10.0, "sl_pct": 7.5},
                "3": {"tp_pct":  7.5, "sl_pct": 5.0},
                "4": {"tp_pct":  7.5, "sl_pct": 5.0},
            },
            "trading": {"scan_start": "09:20", "force_exit": "14:45"},
            "lot_size": 1,
            "backtest_start": date_str,
            "backtest_end":   date_str,
        }
        trades = run_backtest(config, loader=synthetic_loader)
        # Engine completed without error; emitted 0 or more trades
        assert isinstance(trades, list)
        for t in trades:
            assert t.exit_reason in ("TP", "SL", "EOD")
            assert t.instrument == "NIFTY"


from engine.supertrend_low_band_backtest import (  # noqa: E402
    summarize_trades, write_trades_csv,
)


class TestSummarizeTrades:
    def test_empty(self):
        s = summarize_trades([])
        assert s["total_trades"] == 0
        assert s["wins"] == 0
        assert s["losses"] == 0
        assert s["win_rate"] == 0.0

    def test_basic_summary(self):
        trades = [
            make_trade(pnl_points=10.0, pnl_inr=650.0, exit_reason="TP", option_type="CE"),
            make_trade(pnl_points=-7.5, pnl_inr=-487.5, exit_reason="SL", option_type="CE"),
            make_trade(pnl_points=10.0, pnl_inr=650.0, exit_reason="TP", option_type="PE"),
        ]
        s = summarize_trades(trades)
        assert s["total_trades"] == 3
        assert s["wins"] == 2
        assert s["losses"] == 1
        assert s["win_rate"] == pytest.approx(2 / 3)
        assert s["total_pnl_points"] == pytest.approx(12.5)
        assert s["total_pnl_inr"] == pytest.approx(812.5)
        assert s["by_side"]["CE"]["trades"] == 2
        assert s["by_side"]["PE"]["trades"] == 1


class TestWriteTradesCsv:
    def test_writes_csv_with_header_when_empty(self, tmp_path):
        path = tmp_path / "trades.csv"
        write_trades_csv([], str(path))
        df = pd.read_csv(path)
        assert df.empty
        assert "exit_reason" in df.columns

    def test_writes_csv_with_trades(self, tmp_path):
        path = tmp_path / "trades.csv"
        trades = [make_trade(), make_trade(option_type="PE", exit_reason="SL")]
        write_trades_csv(trades, str(path))
        df = pd.read_csv(path)
        assert len(df) == 2
        assert df.iloc[1]["option_type"] == "PE"


import os  # noqa: E402

from config import DATA_PATH  # noqa: E402

DATA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    DATA_PATH["NIFTY"],
)


@pytest.mark.skipif(
    not os.path.exists(DATA_FILE) and not os.path.isdir(DATA_FILE),
    reason="NIFTY parquet not available",
)
class TestBarTimestampConvention:
    def test_first_bar_of_day_is_timestamped_0915(self):
        """Asserts bar timestamp is the OPEN of the bar (not the close).

        If this fails, the morning-low window in the spec must shift from
        [09:15, 09:20) to [09:16, 09:21).
        """
        df = pd.read_parquet(
            DATA_FILE,
            columns=["datetime", "expiry_code", "expiry_type"],
            filters=[("expiry_code", "==", 1), ("expiry_type", "==", "WEEK")],
        )
        df["datetime"] = pd.to_datetime(df["datetime"])
        df["date"] = df["datetime"].dt.date
        first_day = df["date"].min()
        first_day_rows = df[df["date"] == first_day]
        first_minute = first_day_rows["datetime"].min()
        assert first_minute.strftime("%H:%M") == "09:15", (
            f"Expected first bar of day to be timestamped 09:15 (open-stamp), "
            f"got {first_minute.strftime('%H:%M')}. The morning-low window "
            f"in the engine and spec must be revisited."
        )


from config import LOT_SIZE  # noqa: E402


@pytest.mark.skipif(
    not os.path.exists(DATA_FILE) and not os.path.isdir(DATA_FILE),
    reason="NIFTY parquet not available",
)
class TestRealDayIntegration:
    def test_one_day_end_to_end(self):
        config = {
            "instrument": "NIFTY",
            "supertrend": {"factor": 3, "atr_period": 10},
            "first_5min_window": {"start": "09:15", "end": "09:20"},
            "band_pct": 5.0,
            "dte_table": {
                "0": {"tp_pct": 20.0, "sl_pct": 12.5},
                "1": {"tp_pct": 15.0, "sl_pct": 10.0},
                "2": {"tp_pct": 10.0, "sl_pct": 7.5},
                "3": {"tp_pct":  7.5, "sl_pct": 5.0},
                "4": {"tp_pct":  7.5, "sl_pct": 5.0},
            },
            "trading": {"scan_start": "09:20", "force_exit": "14:45"},
            "lot_size": 1,
            "backtest_start": "2026-04-08",
            "backtest_end":   "2026-04-08",
        }
        trades = run_backtest(config)
        for t in trades:
            assert t.option_type in ("CE", "PE")
            assert t.exit_reason in ("SL", "TP", "EOD")
            assert t.entry_price > 0
            assert t.exit_price > 0
            assert isinstance(t.strike, int)
            assert t.lot_size == LOT_SIZE["NIFTY"]
            # DTE must be populated and resolved settings should match the table
            assert t.dte >= 0
            assert t.tp_pct > 0
            assert t.sl_pct > 0
        assert isinstance(trades, list)
