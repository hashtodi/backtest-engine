"""Tests for the IV symmetry naked short straddle backtest engine.

Focus: signal math correctness and lookahead-safe execution semantics
(signal on bar T close -> fill at bar T+1 open, fixed-strike exit tracking).
"""

import pandas as pd
import pytest

from engine.iv_symmetry_straddle_backtest import (
    IVSymmetryStraddleEngine,
    compute_signal_frame,
)

TZ = "Asia/Kolkata"
STEP = 50  # NIFTY strike step


# ---------------------------------------------------------------------------
# Synthetic data builders
# ---------------------------------------------------------------------------

def minute_rows(dt_str, atm, spot, legs):
    """Build long-format rows for one minute.

    legs: list of (option_type, offset, open, close, iv)
    """
    rows = []
    for opt, off, o, c, iv in legs:
        rows.append({
            "datetime": pd.Timestamp(dt_str, tz=TZ),
            "option_type": opt,
            "strike": float(atm + off * STEP),
            "atm_strike": float(atm),
            "strike_offset": off,
            "spot": float(spot),
            "open": float(o),
            "close": float(c),
            "iv": float(iv),
            "expiry_type": "WEEK",
            "expiry_code": 1,
        })
    return rows


def balanced_legs(ce_open, ce_close, pe_open, pe_close, iv=14.0, n_pairs=2):
    """ATM CE/PE legs with given prices + symmetric IV wings (ratio 1.0).

    Produces n_pairs valid pairs on each side, all with identical IV,
    so ce_sym == pe_sym == 1.0 -> signal fires.
    """
    legs = [
        ("CE", 0, ce_open, ce_close, iv),
        ("PE", 0, pe_open, pe_close, iv),
    ]
    for n in range(1, n_pairs + 1):
        for opt in ("CE", "PE"):
            legs.append((opt, n, 10.0, 10.0, iv))
            legs.append((opt, -n, 10.0, 10.0, iv))
    return legs


def skewed_legs(ce_open, ce_close, pe_open, pe_close):
    """Like balanced_legs but PE wings heavily skewed -> no signal."""
    legs = [
        ("CE", 0, ce_open, ce_close, 14.0),
        ("PE", 0, pe_open, pe_close, 14.0),
    ]
    for n in (1, 2):
        legs.append(("CE", n, 10.0, 10.0, 14.0))
        legs.append(("CE", -n, 10.0, 10.0, 14.0))
        legs.append(("PE", -n, 10.0, 10.0, 30.0))  # OTM puts pumped
        legs.append(("PE", n, 10.0, 10.0, 14.0))
    return legs


def to_df(rows):
    return pd.DataFrame(rows)


def make_engine(**kwargs):
    defaults = dict(start_date="2026-01-01", end_date="2026-01-31")
    defaults.update(kwargs)
    return IVSymmetryStraddleEngine(**defaults)


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------

class TestSignalComputation:
    def test_ce_sym_is_exact_average_of_pair_ratios(self):
        # CE pairs: (14 vs 16) = 0.875, (12 vs 15) = 0.8 -> avg 0.8375
        legs = [
            ("CE", 1, 1, 1, 14.0), ("CE", -1, 1, 1, 16.0),
            ("CE", 2, 1, 1, 12.0), ("CE", -2, 1, 1, 15.0),
            ("PE", -1, 1, 1, 10.0), ("PE", 1, 1, 1, 10.0),
            ("PE", -2, 1, 1, 10.0), ("PE", 2, 1, 1, 10.0),
        ]
        df = to_df(minute_rows("2026-01-05 10:00", 23550, 23562, legs))
        sf = compute_signal_frame(df)
        row = sf.iloc[0]
        assert row["ce_sym"] == pytest.approx(0.8375)
        assert row["ce_pairs"] == 2
        assert row["pe_sym"] == pytest.approx(1.0)
        assert row["pe_pairs"] == 2

    def test_signal_true_when_all_conditions_met(self):
        df = to_df(minute_rows("2026-01-05 10:00", 23550, 23562,
                               balanced_legs(96, 96, 104, 104)))
        sf = compute_signal_frame(df)
        assert bool(sf.iloc[0]["signal"]) is True

    def test_signal_false_when_one_side_below_threshold(self):
        df = to_df(minute_rows("2026-01-05 10:00", 23550, 23562,
                               skewed_legs(96, 96, 104, 104)))
        sf = compute_signal_frame(df)
        row = sf.iloc[0]
        assert row["pe_sym"] < 0.80
        assert bool(row["signal"]) is False

    def test_pair_invalid_when_iv_zero_or_missing(self):
        # CE: pair 1 valid; pair 2 has iv=0 on one side; pair 3 missing one side.
        legs = [
            ("CE", 1, 1, 1, 14.0), ("CE", -1, 1, 1, 14.0),
            ("CE", 2, 1, 1, 0.0), ("CE", -2, 1, 1, 14.0),
            ("CE", 3, 1, 1, 14.0),  # no CE -3 row at all
            ("PE", -1, 1, 1, 14.0), ("PE", 1, 1, 1, 14.0),
            ("PE", -2, 1, 1, 14.0), ("PE", 2, 1, 1, 14.0),
        ]
        df = to_df(minute_rows("2026-01-05 10:00", 23550, 23562, legs))
        sf = compute_signal_frame(df)
        row = sf.iloc[0]
        assert row["ce_pairs"] == 1
        # one valid CE pair < min_pairs (2) -> no signal even at perfect symmetry
        assert bool(row["signal"]) is False

    def test_no_pairs_when_only_one_side_of_chain_exists(self):
        legs = [
            ("CE", 1, 1, 1, 14.0), ("CE", 2, 1, 1, 14.0),  # OTM only
            ("PE", -1, 1, 1, 14.0), ("PE", 1, 1, 1, 14.0),
            ("PE", -2, 1, 1, 14.0), ("PE", 2, 1, 1, 14.0),
        ]
        df = to_df(minute_rows("2026-01-05 10:00", 23550, 23562, legs))
        sf = compute_signal_frame(df)
        row = sf.iloc[0]
        assert row["ce_pairs"] == 0
        assert bool(row["signal"]) is False


# ---------------------------------------------------------------------------
# Execution semantics (lookahead safety)
# ---------------------------------------------------------------------------

class TestExecution:
    def day(self, bars):
        """bars: list of (time_str, atm, spot, legs)."""
        rows = []
        for t, atm, spot, legs in bars:
            rows.extend(minute_rows(f"2026-01-05 {t}", atm, spot, legs))
        return to_df(rows)

    def test_entry_fills_at_next_bar_open(self):
        df = self.day([
            ("10:00", 23550, 23562, balanced_legs(100, 98, 100, 102)),
            ("10:01", 23550, 23562, balanced_legs(96, 95, 104, 103)),
            ("10:02", 23550, 23562, balanced_legs(95, 95, 103, 103)),
        ])
        trades = make_engine().run_day(df)
        assert len(trades) == 1
        t = trades[0]
        assert t.signal_time == "10:00"
        assert t.entry_time == "10:01"
        assert t.entry_price == pytest.approx(96 + 104)  # T+1 OPEN, not T close
        assert t.strike == 23550.0

    def test_no_entry_when_signal_on_last_bar(self):
        df = self.day([
            ("10:00", 23550, 23562, skewed_legs(100, 100, 100, 100)),
            ("10:01", 23550, 23562, balanced_legs(96, 95, 104, 103)),
        ])
        trades = make_engine().run_day(df)
        assert trades == []

    def test_sl_exit_at_next_bar_open_captures_gap(self):
        # entry 200 at 10:01; SL threshold 8% -> 216 on close
        bars = [
            ("10:00", 23550, 23562, balanced_legs(100, 100, 100, 100)),
            ("10:01", 23550, 23562, balanced_legs(96, 100, 104, 100)),   # entry 200
            ("10:02", 23550, 23562, balanced_legs(105, 110, 105, 110)),  # close 220 breach
            ("10:03", 23550, 23562, skewed_legs(115, 115, 115, 115)),    # opens 230
            ("10:04", 23550, 23562, skewed_legs(115, 115, 115, 115)),
        ]
        trades = make_engine().run_day(self.day(bars))
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "SL"
        assert t.exit_time == "10:03"
        assert t.exit_price == pytest.approx(230)  # next bar open, gap captured
        assert t.pnl_points == pytest.approx(200 - 230)

    def test_tp_exit_at_next_bar_open(self):
        # entry 200; TP 30% -> close <= 140
        bars = [
            ("10:00", 23550, 23562, balanced_legs(100, 100, 100, 100)),
            ("10:01", 23550, 23562, balanced_legs(96, 100, 104, 100)),  # entry 200
            ("10:02", 23550, 23562, skewed_legs(70, 69, 70, 69)),       # close 138 breach
            ("10:03", 23550, 23562, skewed_legs(68.5, 68, 68.5, 68)),   # opens 137
            ("10:04", 23550, 23562, skewed_legs(68, 68, 68, 68)),
        ]
        trades = make_engine().run_day(self.day(bars))
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "TP"
        assert t.exit_time == "10:03"
        assert t.exit_price == pytest.approx(137)
        assert t.pnl_points == pytest.approx(200 - 137)

    def test_force_exit_at_open_of_force_bar(self):
        bars = [
            ("15:08", 23550, 23562, balanced_legs(100, 100, 100, 100)),
            ("15:09", 23550, 23562, balanced_legs(96, 100, 104, 100)),  # entry 200
            ("15:10", 23550, 23562, skewed_legs(101, 102, 101, 102)),   # force bar
            ("15:11", 23550, 23562, skewed_legs(103, 103, 103, 103)),
        ]
        trades = make_engine().run_day(self.day(bars))
        # entry signal at 15:08 close -> fill 15:09 open (< 15:10 so allowed)
        assert len(trades) == 1
        t = trades[0]
        assert t.entry_time == "15:09"
        assert t.entry_price == pytest.approx(200)
        assert t.exit_reason == "FORCE"
        assert t.exit_time == "15:10"
        assert t.exit_price == pytest.approx(202)  # 15:10 OPEN

    def test_eod_exit_at_last_close_when_no_force_bar(self):
        bars = [
            ("15:00", 23550, 23562, balanced_legs(100, 100, 100, 100)),
            ("15:01", 23550, 23562, balanced_legs(96, 100, 104, 100)),  # entry 200
            ("15:02", 23550, 23562, skewed_legs(100, 101, 100, 101)),   # day ends here
        ]
        trades = make_engine().run_day(self.day(bars))
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "EOD"
        assert t.exit_time == "15:02"
        assert t.exit_price == pytest.approx(202)  # last close

    def test_exit_tracks_entry_strike_not_rolling_atm(self):
        """The notebook bug fix: SL must fire off the SOLD strike's straddle,
        even when the rolling ATM straddle looks calm."""
        atm0 = 23550
        atm1 = 23650  # spot rallied two strikes after entry

        def drifted(entry_strike_straddle, rolling_atm_straddle):
            half_e = entry_strike_straddle / 2
            half_a = rolling_atm_straddle / 2
            legs = []
            # entry strike now at offset -2 of new ATM
            legs.append(("CE", -2, half_e, half_e, 14.0))
            legs.append(("PE", -2, half_e, half_e, 14.0))
            # rolling ATM straddle looks flat at 200
            legs.append(("CE", 0, half_a, half_a, 14.0))
            legs.append(("PE", 0, half_a, half_a, 14.0))
            # wings at +/-1 and +/-3 (offset -2 is the entry strike, placed above)
            for n in (1, 3):
                for opt in ("CE", "PE"):
                    legs.append((opt, n, 10.0, 10.0, 30.0))   # skewed: no re-signal
                    legs.append((opt, -n, 10.0, 10.0, 14.0))
            return legs

        bars = [
            ("10:00", atm0, 23562, balanced_legs(100, 100, 100, 100)),
            ("10:01", atm0, 23562, balanced_legs(96, 100, 104, 100)),    # entry 200 @ 23550
            ("10:02", atm1, 23662, drifted(220, 200)),  # held strike 220 (SL), ATM calm
            ("10:03", atm1, 23662, drifted(232, 200)),  # exit fill bar, opens 232
            ("10:04", atm1, 23662, drifted(232, 200)),
        ]
        trades = make_engine().run_day(self.day(bars))
        assert len(trades) == 1
        t = trades[0]
        assert t.strike == 23550.0
        assert t.exit_reason == "SL"          # fired off the held strike
        assert t.exit_time == "10:03"
        assert t.exit_price == pytest.approx(232)

    def test_reentry_after_exit(self):
        bars = [
            ("10:00", 23550, 23562, balanced_legs(100, 100, 100, 100)),
            ("10:01", 23550, 23562, balanced_legs(96, 100, 104, 100)),   # entry1 200
            ("10:02", 23550, 23562, balanced_legs(70, 69, 70, 69)),      # TP breach (138)
            ("10:03", 23550, 23562, balanced_legs(68.5, 68, 68.5, 68)),  # exit1 @137; flat; signal at close
            ("10:04", 23550, 23562, balanced_legs(68, 67, 68, 67)),      # entry2 @ open 136
            ("10:05", 23550, 23562, balanced_legs(67, 67, 67, 67)),
        ]
        trades = make_engine().run_day(self.day(bars))
        assert len(trades) == 2
        assert trades[0].exit_reason == "TP"
        assert trades[1].entry_time == "10:04"
        assert trades[1].entry_price == pytest.approx(136)

    def test_no_entry_before_entry_window(self):
        bars = [
            ("09:43", 23550, 23562, balanced_legs(100, 100, 100, 100)),
            ("09:44", 23550, 23562, balanced_legs(100, 100, 100, 100)),
            ("09:45", 23550, 23562, balanced_legs(100, 100, 100, 100)),  # first eligible signal
            ("09:46", 23550, 23562, balanced_legs(96, 95, 104, 103)),
            ("09:47", 23550, 23562, balanced_legs(95, 95, 103, 103)),
        ]
        trades = make_engine().run_day(self.day(bars))
        assert len(trades) == 1
        assert trades[0].signal_time == "09:45"
        assert trades[0].entry_time == "09:46"

    def test_entry_cancelled_if_fill_bar_at_force_exit_time(self):
        bars = [
            ("15:09", 23550, 23562, balanced_legs(100, 100, 100, 100)),  # signal
            ("15:10", 23550, 23562, balanced_legs(96, 95, 104, 103)),    # would-be fill >= force
        ]
        trades = make_engine().run_day(self.day(bars))
        assert trades == []

    def test_pnl_inr_uses_lot_size(self):
        bars = [
            ("10:00", 23550, 23562, balanced_legs(100, 100, 100, 100)),
            ("10:01", 23550, 23562, balanced_legs(96, 100, 104, 100)),  # entry 200
            ("10:02", 23550, 23562, skewed_legs(70, 69, 70, 69)),       # TP breach
            ("10:03", 23550, 23562, skewed_legs(68.5, 68, 68.5, 68)),   # exit 137
        ]
        eng = make_engine()
        trades = eng.run_day(self.day(bars))
        t = trades[0]
        assert t.qty == eng.lot_size
        assert t.pnl_inr == pytest.approx((200 - 137) * eng.lot_size)
