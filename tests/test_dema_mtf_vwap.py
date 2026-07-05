"""Tests for the DEMA MTF VWAP backtest engine.

All tests use synthetic data injected directly into the engine
(no parquet files needed).
"""
from datetime import date, datetime, time

import numpy as np
import pandas as pd
import pytest

from engine.dema_mtf_vwap_backtest import (
    DemaMtfVwapBacktestEngine,
    trades_to_dataframe,
)

IST = "Asia/Kolkata"
TEST_DATE = date(2026, 4, 8)    # normal trading day (nearest expiry 2026-04-13)
EXPIRY_DAY = date(2026, 4, 7)   # weekly expiry day in the calendar


def ts(hhmm: str, d: date = TEST_DATE) -> pd.Timestamp:
    h, m = map(int, hhmm.split(":"))
    return pd.Timestamp(datetime.combine(d, time(h, m)), tz=IST)


def make_engine(**kwargs) -> DemaMtfVwapBacktestEngine:
    defaults = dict(start_date="2026-04-08", end_date="2026-04-08")
    defaults.update(kwargs)
    return DemaMtfVwapBacktestEngine(**defaults)


def make_spot_5m(rows, d: date = TEST_DATE) -> pd.DataFrame:
    """rows: list of (hhmm, close, dema, mtf_close, mtf_fast, mtf_slow)."""
    idx = pd.DatetimeIndex([ts(r[0], d) for r in rows], name="datetime")
    df = pd.DataFrame(
        {
            "open": [r[1] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[1] for r in rows],
            "close": [r[1] for r in rows],
            "volume": [100] * len(rows),
            "dema": [r[2] for r in rows],
            "mtf_close": [r[3] for r in rows],
            "mtf_ema_fast": [r[4] for r in rows],
            "mtf_ema_slow": [r[5] for r in rows],
        },
        index=idx,
    )
    df["date"] = [i.date() for i in idx]
    return df


def make_option_1m(
    rows, strike: float, otype: str, d: date = TEST_DATE, expiry_code: int = 1
) -> pd.DataFrame:
    """rows: list of (hhmm, open, high, low, close, volume)."""
    recs = []
    for hhmm, o, h, l, c, v in rows:
        recs.append(
            {
                "datetime": ts(hhmm, d),
                "date": d,
                "strike": float(strike),
                "option_type": otype,
                "expiry_type": "WEEK",
                "expiry_code": expiry_code,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v),
                "spot": 0.0,
            }
        )
    return pd.DataFrame(recs)


def make_spot_1m(d: date, start: str, end: str, close: float = 100.0) -> pd.DataFrame:
    """Flat 1-min spot rows from start to end inclusive."""
    idx = pd.date_range(ts(start, d), ts(end, d), freq="1min")
    return pd.DataFrame(
        {
            "datetime": idx,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1.0,
        }
    )


# Bullish/bearish MTF shorthand (mtf_close, mtf_fast, mtf_slow)
MTF_BULL = (23005.0, 22950.0, 22900.0)
MTF_BEAR = (22800.0, 22850.0, 22900.0)
MTF_MIXED = (22875.0, 22850.0, 22900.0)  # above fast, below slow


# ---------------------------------------------------------------------------
# Data preparation units
# ---------------------------------------------------------------------------

class TestResample1H:
    def test_buckets_anchored_at_0915(self):
        engine = make_engine()
        spot_1m = make_spot_1m(TEST_DATE, "09:15", "15:29")
        out = engine._resample_1h(spot_1m)
        times = [t.strftime("%H:%M") for t in out.index]
        assert times == ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]

    def test_bucket_close_is_last_minute_close(self):
        engine = make_engine()
        spot_1m = make_spot_1m(TEST_DATE, "09:15", "15:29")
        # make the 10:14 minute (last of the 09:15 bucket) close at 123
        spot_1m.loc[spot_1m["datetime"] == ts("10:14"), "close"] = 123.0
        out = engine._resample_1h(spot_1m)
        assert out.loc[ts("09:15"), "close"] == 123.0


class TestMtfAttach:
    def test_1h_values_available_only_after_candle_completes(self):
        engine = make_engine()
        mtf_1h = pd.DataFrame(
            {"close": [100.0, 110.0], "ema_fast": [90.0, 91.0], "ema_slow": [80.0, 81.0]},
            index=pd.DatetimeIndex([ts("09:15"), ts("10:15")]),
        )
        spot_rows = [(t, 0, 0, 0, 0, 0) for t in ["09:20", "10:05", "10:10", "10:15", "11:10"]]
        spot_5m = make_spot_5m(spot_rows).drop(
            columns=["mtf_close", "mtf_ema_fast", "mtf_ema_slow"]
        )
        out = engine._attach_mtf(spot_5m, mtf_1h)

        # 09:15-10:15 candle completes at 10:15 -> first usable on the
        # 10:10 bucket (decision at its close, 10:15)
        assert np.isnan(out.loc[ts("09:20"), "mtf_close"])
        assert np.isnan(out.loc[ts("10:05"), "mtf_close"])
        assert out.loc[ts("10:10"), "mtf_close"] == 100.0
        assert out.loc[ts("10:15"), "mtf_close"] == 100.0
        # 10:15-11:15 candle first usable on the 11:10 bucket
        assert out.loc[ts("11:10"), "mtf_close"] == 110.0
        assert out.loc[ts("10:10"), "mtf_ema_fast"] == 90.0
        assert out.loc[ts("10:10"), "mtf_ema_slow"] == 80.0


class TestContractVwap:
    def test_cumulative_vwap_per_5m_bucket(self):
        engine = make_engine()
        contract = make_option_1m(
            [
                ("09:15", 100, 100, 100, 100, 10),
                ("09:16", 102, 102, 102, 102, 20),
                ("09:20", 104, 104, 104, 104, 10),
            ],
            strike=23000,
            otype="CE",
        )
        out = engine._build_contract_5m(contract)
        assert out.loc[ts("09:15"), "close"] == 102.0
        assert out.loc[ts("09:15"), "vwap"] == pytest.approx((100 * 10 + 102 * 20) / 30)
        assert out.loc[ts("09:20"), "open"] == 104.0
        assert out.loc[ts("09:20"), "vwap"] == pytest.approx((3040 + 1040) / 40)


# ---------------------------------------------------------------------------
# Signal logic (_process_day with injected data)
# ---------------------------------------------------------------------------

def run_day(engine, spot_5m, options_1m, d: date = TEST_DATE):
    engine._spot_5m = spot_5m
    engine._options_1m = options_1m
    return engine._process_day(d)


class TestEntrySignals:
    def test_fresh_vwap_crossover_enters_ce_next_candle_open(self):
        engine = make_engine()
        spot = make_spot_5m([
            ("09:25", 23010, 22900, *MTF_BULL),
            ("09:30", 23010, 22900, *MTF_BULL),
            ("09:35", 23010, 22900, *MTF_BULL),
        ])
        opts = make_option_1m([
            ("09:25", 100, 100, 100, 100, 1),   # close == vwap (at/below)
            ("09:30", 110, 110, 110, 110, 1),   # vwap 105, close 110 -> crossover
            ("09:35", 112, 113, 111, 113, 1),   # entry candle
        ], strike=23000, otype="CE")

        trades = run_day(engine, spot, opts)

        assert len(trades) == 1
        t = trades[0]
        assert t.option_type == "CE"
        assert t.strike == 23000.0
        assert t.signal_time == "09:35"   # signal candle close
        assert t.entry_time == "09:35"    # next candle opens at the same instant
        assert t.entry_price == 112.0
        assert t.exit_reason == "EOD"     # safety net at end of data
        assert t.expiry_date == "2026-04-13"  # nearest weekly

    def test_no_entry_when_option_already_above_vwap(self):
        engine = make_engine()
        spot = make_spot_5m([
            ("09:25", 23010, 22900, *MTF_BULL),
            ("09:30", 23010, 22900, *MTF_BULL),
            ("09:35", 23010, 22900, *MTF_BULL),
        ])
        # bucket 1 already closes above its vwap -> no fresh crossover ever
        opts = make_option_1m([
            ("09:25", 100, 100, 100, 100, 1),
            ("09:26", 120, 120, 120, 120, 1),   # bucket vwap 110, close 120 above
            ("09:30", 125, 125, 125, 125, 1),   # still above
            ("09:35", 130, 130, 130, 130, 1),
        ], strike=23000, otype="CE")

        trades = run_day(engine, spot, opts)
        assert trades == []

    def test_mixed_mtf_blocks_trade(self):
        engine = make_engine()
        spot = make_spot_5m([
            ("09:25", 23010, 22900, *MTF_MIXED),
            ("09:30", 23010, 22900, *MTF_MIXED),
            ("09:35", 23010, 22900, *MTF_MIXED),
        ])
        opts = make_option_1m([
            ("09:25", 100, 100, 100, 100, 1),
            ("09:30", 110, 110, 110, 110, 1),
            ("09:35", 112, 113, 111, 113, 1),
        ], strike=23000, otype="CE")

        trades = run_day(engine, spot, opts)
        assert trades == []

    def test_spot_below_dema_takes_pe(self):
        engine = make_engine()
        # spot 22890 -> ATM 22900, below DEMA -> PE bias, MTF bearish
        spot = make_spot_5m([
            ("09:25", 22890, 22950, *MTF_BEAR),
            ("09:30", 22890, 22950, *MTF_BEAR),
            ("09:35", 22890, 22950, *MTF_BEAR),
        ])
        opts = make_option_1m([
            ("09:25", 100, 100, 100, 100, 1),
            ("09:30", 110, 110, 110, 110, 1),
            ("09:35", 112, 113, 111, 113, 1),
        ], strike=22900, otype="PE")

        trades = run_day(engine, spot, opts)
        assert len(trades) == 1
        assert trades[0].option_type == "PE"
        assert trades[0].strike == 22900.0

    def test_signal_with_entry_at_force_exit_is_skipped(self):
        engine = make_engine(entry_end="15:20", force_exit_time="15:15")
        spot = make_spot_5m([
            ("15:05", 23010, 22900, *MTF_BULL),
            ("15:10", 23010, 22900, *MTF_BULL),
            ("15:15", 23010, 22900, *MTF_BULL),
        ])
        # crossover signal at 15:10 bucket (close 15:15) -> entry would be
        # the 15:15 bucket open, which is at/after force exit -> skip
        opts = make_option_1m([
            ("15:05", 100, 100, 100, 100, 1),
            ("15:10", 110, 110, 110, 110, 1),
            ("15:15", 112, 113, 111, 113, 1),
        ], strike=23000, otype="CE")

        trades = run_day(engine, spot, opts)
        assert trades == []


class TestExits:
    def _entry_setup_spot(self, extra_rows):
        rows = [
            ("09:25", 23010, 22900, *MTF_BULL),
            ("09:30", 23010, 22900, *MTF_BULL),
            ("09:35", 23010, 22900, *MTF_BULL),
        ] + extra_rows
        return make_spot_5m(rows)

    def _entry_setup_opts(self, extra_rows):
        rows = [
            ("09:25", 100, 100, 100, 100, 1),
            ("09:30", 110, 110, 110, 110, 1),
            ("09:35", 112, 113, 111, 113, 1),  # entry at 112: SL 78.4, TP 168
        ] + extra_rows
        return make_option_1m(rows, strike=23000, otype="CE")

    def test_sl_exit_exact_fill(self):
        engine = make_engine()  # sl 30% -> 78.4
        spot = self._entry_setup_spot([("09:40", 23010, 22900, *MTF_BULL)])
        opts = self._entry_setup_opts([("09:40", 111, 111, 70, 80, 1)])

        trades = run_day(engine, spot, opts)
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "SL"
        assert t.exit_time == "09:40"
        assert t.exit_price == pytest.approx(78.4)
        assert t.pnl_points == pytest.approx(78.4 - 112.0)
        assert t.pnl_inr == pytest.approx((78.4 - 112.0) * engine.lot_size)

    def test_tp_exit_exact_fill(self):
        engine = make_engine()  # tp 50% -> 168
        spot = self._entry_setup_spot([("09:40", 23010, 22900, *MTF_BULL)])
        opts = self._entry_setup_opts([("09:40", 115, 200, 114, 190, 1)])

        trades = run_day(engine, spot, opts)
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "TP"
        assert t.exit_price == pytest.approx(168.0)
        assert t.pnl_points == pytest.approx(56.0)

    def test_eod_force_exit_at_close(self):
        engine = make_engine()  # force exit 15:15
        spot = self._entry_setup_spot([
            ("09:40", 23010, 22900, *MTF_BULL),
            ("15:15", 23010, 22900, *MTF_BULL),
        ])
        opts = self._entry_setup_opts([
            ("09:40", 111, 112, 110, 111, 1),
            ("15:15", 95, 96, 94, 95, 1),
        ])

        trades = run_day(engine, spot, opts)
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "EOD"
        assert t.exit_time == "15:15"
        assert t.exit_price == 95.0

    def test_reentry_after_exit(self):
        engine = make_engine()
        spot = self._entry_setup_spot([
            ("09:40", 23010, 22900, *MTF_BULL),
            ("09:45", 23010, 22900, *MTF_BULL),
            ("09:50", 23010, 22900, *MTF_BULL),
            ("09:55", 23010, 22900, *MTF_BULL),
        ])
        opts = self._entry_setup_opts([
            ("09:40", 150, 200, 149, 150, 1),  # TP exit at 168
            ("09:45", 90, 90, 89, 90, 1),      # back below vwap
            ("09:50", 130, 131, 129, 130, 1),  # fresh crossover again
            ("09:55", 132, 133, 131, 132, 1),  # second entry
        ])

        trades = run_day(engine, spot, opts)
        assert len(trades) == 2
        assert trades[0].exit_reason == "TP"
        assert trades[1].entry_time == "09:55"
        assert trades[1].entry_price == 132.0


class TestExpirySelection:
    """On expiry days the engine must roll to the NEXT weekly expiry
    (expiry_code 2); on normal days it trades the nearest (code 1)."""

    def _spot(self, d):
        return make_spot_5m([
            ("09:25", 23010, 22900, *MTF_BULL),
            ("09:30", 23010, 22900, *MTF_BULL),
            ("09:35", 23010, 22900, *MTF_BULL),
        ], d=d)

    def _opts(self, d, code):
        return make_option_1m([
            ("09:25", 100, 100, 100, 100, 1),
            ("09:30", 110, 110, 110, 110, 1),
            ("09:35", 112, 113, 111, 113, 1),
        ], strike=23000, otype="CE", d=d, expiry_code=code)

    def test_expiry_day_rolls_to_next_weekly(self):
        engine = make_engine(start_date="2026-04-07", end_date="2026-04-07")
        trades = run_day(
            engine, self._spot(EXPIRY_DAY), self._opts(EXPIRY_DAY, code=2), d=EXPIRY_DAY
        )
        assert len(trades) == 1
        assert trades[0].expiry_date == "2026-04-13"

    def test_expiry_day_ignores_expiring_code1_contracts(self):
        engine = make_engine(start_date="2026-04-07", end_date="2026-04-07")
        trades = run_day(
            engine, self._spot(EXPIRY_DAY), self._opts(EXPIRY_DAY, code=1), d=EXPIRY_DAY
        )
        assert trades == []

    def test_normal_day_ignores_code2_contracts(self):
        engine = make_engine()
        trades = run_day(engine, self._spot(TEST_DATE), self._opts(TEST_DATE, code=2))
        assert trades == []


class TestTradesDataframe:
    def test_empty_trades_gives_empty_dataframe(self):
        assert trades_to_dataframe([]).empty
