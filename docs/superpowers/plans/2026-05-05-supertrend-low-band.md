# SuperTrend Low-Band Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline execution per user preference). Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **User preference:** Do NOT run `git` write commands. Each task's "Suggest commit" step provides the message; the user runs the commit manually.

**Goal:** Build a backtest engine for a daily intraday options strategy that buys NIFTY weekly ATM CE/PE when its continuous SuperTrend(3,10) is bullish AND its value falls within ±5% of that contract's 9:15-9:19 morning low. TP 10%, SL 7.5%, force-exit 14:45.

**Architecture:** Dedicated engine `engine/supertrend_low_band_backtest.py` modeled on `engine/gamma_blast_backtest.py`. Pure helper functions for unit-testability + per-day per-side state machine + top-level `run_backtest(config)` driver. Streamlit form mirrors `ui/gamma_blast_backtest_runner.py`.

**Tech Stack:** Python 3, pandas, pyarrow, pytest, Streamlit. Reuses existing `indicators/supertrend.py` and `engine/data_loader.py`.

**Spec:** `docs/superpowers/specs/2026-05-05-supertrend-low-band-design.md`

---

## File Structure

| File | Purpose |
|------|---------|
| `engine/supertrend_low_band_backtest.py` (new) | Trade dataclass, pure helpers, state machine, `run_backtest`, output helpers |
| `saved_strategies/supertrend_low_band.json` (new) | Default config |
| `ui/supertrend_low_band_backtest_runner.py` (new) | Streamlit form + results display |
| `tests/test_supertrend_low_band.py` (new) | Pytest unit + state machine + integration tests |
| `app.py` (modify) | Add new tab `tab_st_low_band` |

**Boundaries:**
- Engine module is pure logic. No Streamlit imports. Loader is parametrizable (default reads parquet) so tests can pass synthetic data.
- All decision points are extracted as small pure functions (`is_in_band`, `evaluate_entry`, `evaluate_exit`) for direct unit testing — same pattern as `engine/gamma_blast_backtest.py`.
- State machine is a single function `run_machine_for_day_side` taking a precomputed day DataFrame plus indices.

---

## Task 1: Scaffold — JSON config + module skeleton + dataclass

**Files:**
- Create: `saved_strategies/supertrend_low_band.json`
- Create: `engine/supertrend_low_band_backtest.py`
- Create: `tests/test_supertrend_low_band.py`

- [ ] **Step 1.1: Create JSON config**

Write `saved_strategies/supertrend_low_band.json`:

```json
{
  "name": "supertrend_low_band",
  "instrument": "NIFTY",
  "supertrend": { "factor": 3, "atr_period": 10 },
  "first_5min_window": { "start": "09:15", "end": "09:20" },
  "band_pct": 5.0,
  "sl_pct": 7.5,
  "tp_pct": 10.0,
  "trading": {
    "scan_start": "09:20",
    "force_exit": "14:45"
  },
  "lot_size": 1,
  "backtest_start": "2025-01-01",
  "backtest_end":   "2026-04-30"
}
```

- [ ] **Step 1.2: Create engine module skeleton**

Write `engine/supertrend_low_band_backtest.py`:

```python
"""
SuperTrend Low-Band Backtest Engine.

Strategy:
  Daily intraday on NIFTY weekly ATM CE/PE. Buy when continuous
  SuperTrend(3,10) on the option is bullish AND its value is within
  ±band_pct of the contract's 9:15-9:19 morning low. TP/SL as % of
  entry; force-exit at 14:45. CE and PE run as fully independent
  state machines with unbounded same-day re-entry.

  Spec: docs/superpowers/specs/2026-05-05-supertrend-low-band-design.md
"""

import logging
import os
from dataclasses import asdict, dataclass, fields
from datetime import date as _date, time as _time
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from config import DATA_PATH, LOT_SIZE
from indicators.supertrend import SuperTrend

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.dirname(__file__))

ST_BULLISH = -1.0  # SuperTrend.calculate() returns -1 for bullish, +1 for bearish


@dataclass
class StLowBandTrade:
    """Single completed trade record."""
    date: str                 # "YYYY-MM-DD"
    instrument: str           # "NIFTY"
    expiry_date: str          # "YYYY-MM-DD" of nearest weekly expiry
    option_type: str          # "CE" | "PE"
    strike: int

    morning_low: float        # 9:15-9:19 min(low) for this contract
    band_low: float           # morning_low * (1 - band_pct/100)
    band_high: float          # morning_low * (1 + band_pct/100)

    spot_at_entry: float
    entry_time: str           # "HH:MM"
    entry_price: float        # next-bar open of locked strike
    entry_st_value: float     # ST value at trigger bar T close
    entry_trigger_close: float  # option close at trigger bar T

    spot_at_exit: float
    exit_time: str
    exit_price: float         # exact SL/TP level OR EOD close
    exit_reason: str          # "SL" | "TP" | "EOD"

    pnl_points: float         # exit_price - entry_price
    pnl_inr: float            # pnl_points * lot_size_total
    lot_size: int             # LOT_SIZE * json.lot_size


def trades_to_dataframe(trades: List[StLowBandTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
```

- [ ] **Step 1.3: Create test module skeleton**

Write `tests/test_supertrend_low_band.py`:

```python
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
        band_low=95.0,
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
        pnl_points=10.0,
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
            "morning_low", "band_low", "band_high",
            "spot_at_entry", "entry_time", "entry_price",
            "entry_st_value", "entry_trigger_close",
            "spot_at_exit", "exit_time", "exit_price", "exit_reason",
            "pnl_points", "pnl_inr", "lot_size",
        }
        assert df.iloc[1]["option_type"] == "PE"
```

- [ ] **Step 1.4: Run tests — verify scaffold passes**

Run: `pytest tests/test_supertrend_low_band.py -v`
Expected: 3 tests pass.

- [ ] **Step 1.5: Suggest commit**

```
feat(st-low-band): scaffold — JSON config, trade dataclass, test skeleton

Adds engine/supertrend_low_band_backtest.py with the StLowBandTrade
dataclass and trades_to_dataframe helper, plus the saved_strategies
JSON config and a baseline pytest module.
```

---

## Task 2: `is_in_band` pure helper

**Files:**
- Modify: `engine/supertrend_low_band_backtest.py`
- Modify: `tests/test_supertrend_low_band.py`

- [ ] **Step 2.1: Write failing tests**

Add this class to `tests/test_supertrend_low_band.py`:

```python
from engine.supertrend_low_band_backtest import is_in_band  # noqa: E402


class TestIsInBand:
    def test_value_at_lower_edge_inclusive(self):
        assert is_in_band(value=95.0, low=100.0, band_pct=5.0) is True

    def test_value_at_upper_edge_inclusive(self):
        assert is_in_band(value=105.0, low=100.0, band_pct=5.0) is True

    def test_value_inside_band(self):
        assert is_in_band(value=100.0, low=100.0, band_pct=5.0) is True

    def test_value_just_below_band(self):
        assert is_in_band(value=94.99, low=100.0, band_pct=5.0) is False

    def test_value_just_above_band(self):
        assert is_in_band(value=105.01, low=100.0, band_pct=5.0) is False

    def test_nan_low_returns_false(self):
        assert is_in_band(value=100.0, low=float("nan"), band_pct=5.0) is False

    def test_nan_value_returns_false(self):
        assert is_in_band(value=float("nan"), low=100.0, band_pct=5.0) is False

    def test_band_pct_3_narrower(self):
        assert is_in_band(value=96.5, low=100.0, band_pct=3.0) is False
        assert is_in_band(value=97.0, low=100.0, band_pct=3.0) is True
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `pytest tests/test_supertrend_low_band.py::TestIsInBand -v`
Expected: ImportError on `is_in_band` (function not defined).

- [ ] **Step 2.3: Implement `is_in_band`**

Add to `engine/supertrend_low_band_backtest.py` (after the dataclass):

```python
import math


def is_in_band(value: float, low: float, band_pct: float) -> bool:
    """True iff `value` is within ±band_pct% of `low`, inclusive on both edges.

    NaN on either input returns False (skip-side semantics).
    """
    if value is None or low is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    if isinstance(low, float) and math.isnan(low):
        return False
    delta = abs(low) * (band_pct / 100.0)
    return (low - delta) <= value <= (low + delta)
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `pytest tests/test_supertrend_low_band.py::TestIsInBand -v`
Expected: 8 tests pass.

- [ ] **Step 2.5: Suggest commit**

```
feat(st-low-band): is_in_band helper with NaN-safe band check
```

---

## Task 3: `evaluate_entry` pure helper

**Files:**
- Modify: `engine/supertrend_low_band_backtest.py`
- Modify: `tests/test_supertrend_low_band.py`

- [ ] **Step 3.1: Write failing tests**

Add to `tests/test_supertrend_low_band.py`:

```python
from engine.supertrend_low_band_backtest import evaluate_entry  # noqa: E402


class TestEvaluateEntry:
    """Entry condition: ST bullish AND ST value in ±band_pct of morning_low."""

    def test_enter_when_bullish_and_in_band(self):
        assert evaluate_entry(
            st_value=98.0, st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is True

    def test_no_enter_when_bullish_but_outside_band(self):
        assert evaluate_entry(
            st_value=80.0, st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is False

    def test_no_enter_when_in_band_but_bearish(self):
        assert evaluate_entry(
            st_value=98.0, st_dir=1.0,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is False

    def test_enter_at_lower_edge(self):
        assert evaluate_entry(
            st_value=95.0, st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is True

    def test_enter_at_upper_edge(self):
        assert evaluate_entry(
            st_value=105.0, st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is True

    def test_no_enter_when_low_is_nan(self):
        assert evaluate_entry(
            st_value=98.0, st_dir=ST_BULLISH,
            morning_low=float("nan"), band_pct=5.0, bullish_required=True,
        ) is False

    def test_no_enter_when_st_value_is_nan(self):
        assert evaluate_entry(
            st_value=float("nan"), st_dir=ST_BULLISH,
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is False

    def test_no_enter_when_st_dir_is_nan(self):
        assert evaluate_entry(
            st_value=98.0, st_dir=float("nan"),
            morning_low=100.0, band_pct=5.0, bullish_required=True,
        ) is False

    def test_bullish_filter_off_ignores_direction(self):
        # When bullish_required=False, entry can fire on bearish bars too
        assert evaluate_entry(
            st_value=98.0, st_dir=1.0,
            morning_low=100.0, band_pct=5.0, bullish_required=False,
        ) is True
```

- [ ] **Step 3.2: Run test to verify it fails**

Run: `pytest tests/test_supertrend_low_band.py::TestEvaluateEntry -v`
Expected: ImportError on `evaluate_entry`.

- [ ] **Step 3.3: Implement `evaluate_entry`**

Add to `engine/supertrend_low_band_backtest.py`:

```python
def evaluate_entry(
    st_value: float,
    st_dir: float,
    morning_low: float,
    band_pct: float,
    bullish_required: bool = True,
) -> bool:
    """True iff entry conditions met at this bar's close.

    Conditions:
      1. (if bullish_required) st_dir == ST_BULLISH
      2. st_value is within ±band_pct% of morning_low (inclusive)

    NaN on any input returns False (skip-side semantics).
    """
    if st_dir is None or (isinstance(st_dir, float) and math.isnan(st_dir)):
        return False
    if bullish_required and st_dir != ST_BULLISH:
        return False
    return is_in_band(st_value, morning_low, band_pct)
```

- [ ] **Step 3.4: Run tests to verify they pass**

Run: `pytest tests/test_supertrend_low_band.py::TestEvaluateEntry -v`
Expected: 9 tests pass.

- [ ] **Step 3.5: Suggest commit**

```
feat(st-low-band): evaluate_entry helper with bullish + band check
```

---

## Task 4: `evaluate_exit` pure helper

**Files:**
- Modify: `engine/supertrend_low_band_backtest.py`
- Modify: `tests/test_supertrend_low_band.py`

- [ ] **Step 4.1: Write failing tests**

Add to `tests/test_supertrend_low_band.py`:

```python
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
        # If SL hits on the force-exit bar, take SL price not EOD close
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
        # bar.low == sl → SL fires
        result = evaluate_exit(
            bar_high=105.0, bar_low=92.5, bar_close=95.0,
            sl=92.5, tp=110.0, is_force_exit_bar=False,
        )
        assert result == (92.5, "SL")

    def test_tp_at_exact_high(self):
        # bar.high == tp → TP fires
        result = evaluate_exit(
            bar_high=110.0, bar_low=99.0, bar_close=108.0,
            sl=92.5, tp=110.0, is_force_exit_bar=False,
        )
        assert result == (110.0, "TP")
```

- [ ] **Step 4.2: Run test to verify it fails**

Run: `pytest tests/test_supertrend_low_band.py::TestEvaluateExit -v`
Expected: ImportError on `evaluate_exit`.

- [ ] **Step 4.3: Implement `evaluate_exit`**

Add to `engine/supertrend_low_band_backtest.py`:

```python
def evaluate_exit(
    bar_high: float, bar_low: float, bar_close: float,
    sl: float, tp: float, is_force_exit_bar: bool,
) -> Optional[Tuple[float, str]]:
    """Return (exit_price, exit_reason) if this bar exits, else None.

    Priority: SL > TP > EOD. SL/TP fills assume exact-level fills when
    the wick touches; this is the same convention as Gamma Blast.
    """
    if bar_low <= sl:
        return (float(sl), "SL")
    if bar_high >= tp:
        return (float(tp), "TP")
    if is_force_exit_bar:
        return (float(bar_close), "EOD")
    return None
```

- [ ] **Step 4.4: Run tests to verify they pass**

Run: `pytest tests/test_supertrend_low_band.py::TestEvaluateExit -v`
Expected: 9 tests pass.

- [ ] **Step 4.5: Suggest commit**

```
feat(st-low-band): evaluate_exit with SL>TP>EOD precedence
```

---

## Task 5: `compute_first_5min_low_table` helper

**Files:**
- Modify: `engine/supertrend_low_band_backtest.py`
- Modify: `tests/test_supertrend_low_band.py`

Builds a dict `{(date, strike, option_type, expiry_type, expiry_code): morning_low}` from a DataFrame of 1-min option bars. The "morning low" is `min(low)` across bars whose `time_only` falls within `[window_start, window_end)` (half-open: 09:15 inclusive, 09:20 exclusive).

- [ ] **Step 5.1: Write failing tests**

Add to `tests/test_supertrend_low_band.py`:

```python
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
            ("09:17",  95.0),  # min
            ("09:18",  98.0),
            ("09:19", 102.0),
            ("09:20",  90.0),  # OUTSIDE window — must be ignored
        ])
        table = compute_first_5min_low_table(
            df, window_start=time(9, 15), window_end=time(9, 20),
        )
        key = (pd.Timestamp("2026-04-08").date(), 23850, "CE", "WEEK", 1)
        assert table[key] == 95.0

    def test_window_is_half_open_excludes_end(self):
        df = _bars("2026-04-08", 23850, "CE", [
            ("09:20",  50.0),  # at window_end — must be excluded
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
            ("09:25", 100.0),  # outside window
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
```

- [ ] **Step 5.2: Run test to verify it fails**

Run: `pytest tests/test_supertrend_low_band.py::TestComputeFirst5MinLowTable -v`
Expected: ImportError on `compute_first_5min_low_table`.

- [ ] **Step 5.3: Implement `compute_first_5min_low_table`**

Add to `engine/supertrend_low_band_backtest.py`:

```python
def compute_first_5min_low_table(
    df: pd.DataFrame, window_start: _time, window_end: _time,
) -> Dict[Tuple, float]:
    """Per-(date, contract) min(low) across bars in [window_start, window_end).

    Window is half-open: window_start inclusive, window_end exclusive. The
    9:15-9:20 candle covers bars timestamped 09:15, 09:16, 09:17, 09:18, 09:19.

    Args:
        df: 1-min option bars with columns date, time_only, strike,
            option_type, expiry_type, expiry_code, low.
        window_start: e.g. time(9, 15)
        window_end:   e.g. time(9, 20)

    Returns:
        Dict keyed by (date, strike, option_type, expiry_type, expiry_code)
        → float (min low). Contracts with no bars in the window are absent.
    """
    in_window = df[(df["time_only"] >= window_start) & (df["time_only"] < window_end)]
    if in_window.empty:
        return {}
    grouped = in_window.groupby(
        ["date", "strike", "option_type", "expiry_type", "expiry_code"],
    )["low"].min()
    return grouped.to_dict()
```

- [ ] **Step 5.4: Run tests to verify they pass**

Run: `pytest tests/test_supertrend_low_band.py::TestComputeFirst5MinLowTable -v`
Expected: 5 tests pass.

- [ ] **Step 5.5: Suggest commit**

```
feat(st-low-band): compute_first_5min_low_table per-(date, contract)
```

---

## Task 6: `compute_continuous_supertrend_per_contract` helper

**Files:**
- Modify: `engine/supertrend_low_band_backtest.py`
- Modify: `tests/test_supertrend_low_band.py`

Wraps `indicators.supertrend.SuperTrend` to add `st_value` and `st_dir` columns to the input DataFrame. Computed continuously per contract (no daily reset) — matches the convention in `engine/data_loader.py:213-219`.

- [ ] **Step 6.1: Write failing tests**

Add to `tests/test_supertrend_low_band.py`:

```python
from engine.supertrend_low_band_backtest import (  # noqa: E402
    compute_continuous_supertrend_per_contract,
)


def _ohlc_bars(strike, option_type, n_bars, base_price=100.0, drift=0.0):
    """Build n synthetic 1-min OHLC bars for one contract starting 09:15."""
    rows = []
    for i in range(n_bars):
        ts = pd.Timestamp("2026-04-08") + pd.Timedelta(minutes=i)
        ts = pd.Timestamp.combine(pd.Timestamp("2026-04-08").date(),
                                  pd.Timestamp(f"09:{15 + i // 60:02d}:{i % 60:02d}").time())
        ts = ts.tz_localize("Asia/Kolkata") if ts.tzinfo is None else ts
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
    def test_adds_columns(self):
        df = _ohlc_bars(23850, "CE", n_bars=30, base_price=100.0, drift=0.5)
        out = compute_continuous_supertrend_per_contract(df, factor=3, atr_period=10)
        assert "st_value" in out.columns
        assert "st_dir" in out.columns

    def test_first_atr_period_bars_are_nan(self):
        df = _ohlc_bars(23850, "CE", n_bars=30, base_price=100.0, drift=0.5)
        out = compute_continuous_supertrend_per_contract(df, factor=3, atr_period=10)
        out = out.sort_values("datetime").reset_index(drop=True)
        # First atr_period bars (indices 0..9) should have NaN st_value
        assert out["st_value"].iloc[:10].isna().all()
        # From index 10 onwards we should have values
        assert out["st_value"].iloc[10:].notna().all()

    def test_separate_contracts_compute_independently(self):
        df1 = _ohlc_bars(23850, "CE", n_bars=30, base_price=100.0, drift=0.5)
        df2 = _ohlc_bars(23900, "CE", n_bars=30, base_price=200.0, drift=-0.5)
        df = pd.concat([df1, df2], ignore_index=True)

        out = compute_continuous_supertrend_per_contract(df, factor=3, atr_period=10)
        ce_23850 = out[out["strike"] == 23850].sort_values("datetime")
        ce_23900 = out[out["strike"] == 23900].sort_values("datetime")
        # ST values should differ between contracts (different price levels)
        assert not (ce_23850["st_value"].iloc[15] == ce_23900["st_value"].iloc[15])

    def test_directions_are_minus_one_or_plus_one(self):
        df = _ohlc_bars(23850, "CE", n_bars=30, base_price=100.0, drift=0.5)
        out = compute_continuous_supertrend_per_contract(df, factor=3, atr_period=10)
        valid_dirs = out["st_dir"].dropna().unique()
        for d in valid_dirs:
            assert d in (-1.0, 1.0)
```

- [ ] **Step 6.2: Run test to verify it fails**

Run: `pytest tests/test_supertrend_low_band.py::TestComputeContinuousSupertrend -v`
Expected: ImportError on `compute_continuous_supertrend_per_contract`.

- [ ] **Step 6.3: Implement the helper**

Add to `engine/supertrend_low_band_backtest.py`:

```python
CONTRACT_COLS = ["strike", "option_type", "expiry_type", "expiry_code"]


def compute_continuous_supertrend_per_contract(
    df: pd.DataFrame, factor: int, atr_period: int,
) -> pd.DataFrame:
    """Add `st_value` and `st_dir` columns to `df`, computed per contract.

    Each unique (strike, option_type, expiry_type, expiry_code) is treated as
    one contract; ST is computed continuously across the contract's lifetime
    (no daily reset). Matches engine/data_loader.py convention for
    option-source indicators.

    Returns the DataFrame with two new columns added.
    """
    st_ind = SuperTrend(name="st", factor=factor, atr_period=atr_period)
    parts = []
    for _, group in df.groupby(CONTRACT_COLS, sort=False):
        group = group.sort_values("datetime").copy()
        result = st_ind.calculate(
            group["close"], high=group["high"], low=group["low"],
        )
        group["st_value"] = result["value"].values
        group["st_dir"] = result["direction"].values
        parts.append(group)
    if not parts:
        df = df.copy()
        df["st_value"] = float("nan")
        df["st_dir"] = float("nan")
        return df
    return pd.concat(parts, ignore_index=True)
```

- [ ] **Step 6.4: Run tests to verify they pass**

Run: `pytest tests/test_supertrend_low_band.py::TestComputeContinuousSupertrend -v`
Expected: 4 tests pass.

- [ ] **Step 6.5: Suggest commit**

```
feat(st-low-band): continuous SuperTrend per option contract
```

---

## Task 7: `build_atm_index` helper

**Files:**
- Modify: `engine/supertrend_low_band_backtest.py`
- Modify: `tests/test_supertrend_low_band.py`

Builds a dict `{(datetime, option_type): strike}` from rows where `moneyness == 'ATM'`.

- [ ] **Step 7.1: Write failing tests**

Add to `tests/test_supertrend_low_band.py`:

```python
from engine.supertrend_low_band_backtest import build_atm_index  # noqa: E402


class TestBuildAtmIndex:
    def test_picks_only_atm_rows(self):
        rows = []
        for strike, money in [(23800, "OTM"), (23850, "ATM"), (23900, "OTM")]:
            ts = pd.Timestamp("2026-04-08 09:20", tz="Asia/Kolkata")
            rows.append({
                "datetime": ts, "strike": strike, "option_type": "CE",
                "expiry_type": "WEEK", "expiry_code": 1, "moneyness": money,
            })
        df = pd.DataFrame(rows)
        idx = build_atm_index(df)
        ts = pd.Timestamp("2026-04-08 09:20", tz="Asia/Kolkata")
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
```

- [ ] **Step 7.2: Run test to verify it fails**

Run: `pytest tests/test_supertrend_low_band.py::TestBuildAtmIndex -v`
Expected: ImportError on `build_atm_index`.

- [ ] **Step 7.3: Implement `build_atm_index`**

Add to `engine/supertrend_low_band_backtest.py`:

```python
def build_atm_index(df: pd.DataFrame) -> Dict[Tuple, int]:
    """Return {(datetime, option_type): strike} for rows where moneyness == 'ATM'.

    Filters to expiry_type == 'WEEK' and expiry_code == 1 (nearest weekly).
    Caller is responsible for any further filtering.
    """
    atm = df[
        (df["moneyness"] == "ATM")
        & (df["expiry_type"] == "WEEK")
        & (df["expiry_code"] == 1)
    ]
    if atm.empty:
        return {}
    out: Dict[Tuple, int] = {}
    for row in atm.itertuples(index=False):
        out[(row.datetime, row.option_type)] = int(row.strike)
    return out
```

- [ ] **Step 7.4: Run tests to verify they pass**

Run: `pytest tests/test_supertrend_low_band.py::TestBuildAtmIndex -v`
Expected: 4 tests pass.

- [ ] **Step 7.5: Suggest commit**

```
feat(st-low-band): build_atm_index for per-minute ATM lookup
```

---

## Task 8: `run_machine_for_day_side` — entry, SL/TP/EOD exits, force-exit safety

**Files:**
- Modify: `engine/supertrend_low_band_backtest.py`
- Modify: `tests/test_supertrend_low_band.py`

Single-day, single-side state machine. Inputs: DataFrame for one day with ST columns added, indices, params. Output: list of `StLowBandTrade`.

- [ ] **Step 8.1: Write a synthetic-bars test helper**

Add at the top of `tests/test_supertrend_low_band.py` (helper for state-machine tests):

```python
def _build_day_bars(strike, option_type, minute_bars, atm_strike=None,
                    spot_at_each=None, date_str="2026-04-08"):
    """Build a day's worth of synthetic option bars with all required columns.

    minute_bars: list of dicts with keys time (HH:MM), open, high, low, close
    atm_strike:  if provided, mark this strike as ATM. Otherwise derive from `strike` parameter.
    spot_at_each: optional dict {time_str: spot}. Defaults to 23850.0.
    """
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
```

- [ ] **Step 8.2: Write the entry-fires test (failing)**

Add to `tests/test_supertrend_low_band.py`:

```python
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
        atm_index = {(b["datetime"], "CE"): 23850 for _, b in df.iterrows()}

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(),
            morning_low_table=morning_low_table,
            atm_index=atm_index,
            instrument="NIFTY",
            **self._params(),
        )

        # Entry: 9:20 close triggers (ST=98 in [95,105], bullish), buy at 9:21 open
        assert len(trades) == 1
        t = trades[0]
        assert t.entry_time == "09:21"
        assert t.entry_price == 101.0
        assert t.strike == 23850
        # Final position closes at 14:45 close (EOD)
        assert t.exit_reason == "EOD"
        assert t.exit_time == "14:45"
```

- [ ] **Step 8.3: Run test to verify it fails**

Run: `pytest tests/test_supertrend_low_band.py::TestRunMachineForDaySide::test_entry_fires_when_st_in_band_and_bullish -v`
Expected: ImportError on `run_machine_for_day_side`.

- [ ] **Step 8.4: Implement `run_machine_for_day_side` (full state machine)**

Add to `engine/supertrend_low_band_backtest.py`:

```python
def _format_hhmm(dt) -> str:
    return pd.Timestamp(dt).strftime("%H:%M")


def run_machine_for_day_side(
    df: pd.DataFrame,
    *,
    side: str,
    date: _date,
    expiry_date: _date,
    morning_low_table: Dict[Tuple, float],
    atm_index: Dict[Tuple, int],
    instrument: str,
    band_pct: float,
    sl_pct: float,
    tp_pct: float,
    scan_start: _time,
    force_exit: _time,
    bullish_required: bool,
    lot_size_total: int,
) -> List[StLowBandTrade]:
    """Run the state machine for one day, one side.

    `df` must already contain st_value / st_dir columns and rows for ALL
    contracts of this side that traded during the day (used for ATM lookup
    + locked-strike row lookup).
    """
    trades: List[StLowBandTrade] = []
    side_df = df[df["option_type"] == side]
    if side_df.empty:
        return trades

    # Multi-index for (strike, datetime) lookup of locked-strike rows
    by_strike_minute = side_df.set_index(["strike", "datetime"]).sort_index()

    minutes = sorted(set(side_df["datetime"].tolist()))
    if not minutes:
        return trades

    state: Optional[dict] = None  # None = IDLE; dict with status="OPEN" otherwise

    def row_at(strike, minute):
        try:
            return by_strike_minute.loc[(strike, minute)]
        except KeyError:
            return None

    for idx, minute in enumerate(minutes):
        minute_time = minute.time()
        next_minute = minutes[idx + 1] if idx + 1 < len(minutes) else None

        # ---- OPEN: check exit on locked strike ----
        if state is not None and state["status"] == "OPEN":
            row = row_at(state["strike"], minute)
            if row is not None:
                is_force = minute_time >= force_exit
                result = evaluate_exit(
                    bar_high=float(row["high"]),
                    bar_low=float(row["low"]),
                    bar_close=float(row["close"]),
                    sl=state["sl"], tp=state["tp"],
                    is_force_exit_bar=is_force,
                )
                if result is not None:
                    exit_price, reason = result
                    pnl_points = exit_price - state["entry_price"]
                    trades.append(StLowBandTrade(
                        date=str(date),
                        instrument=instrument,
                        expiry_date=str(expiry_date),
                        option_type=side,
                        strike=int(state["strike"]),
                        morning_low=state["morning_low"],
                        band_low=state["band_low"],
                        band_high=state["band_high"],
                        spot_at_entry=state["spot_at_entry"],
                        entry_time=state["entry_time"],
                        entry_price=state["entry_price"],
                        entry_st_value=state["entry_st_value"],
                        entry_trigger_close=state["entry_trigger_close"],
                        spot_at_exit=float(row.get("spot", 0.0)),
                        exit_time=_format_hhmm(minute),
                        exit_price=float(exit_price),
                        exit_reason=reason,
                        pnl_points=float(pnl_points),
                        pnl_inr=float(pnl_points) * lot_size_total,
                        lot_size=int(lot_size_total),
                    ))
                    state = None  # IDLE — fall through to scan this same minute

        # ---- IDLE: scan for entry ----
        if state is None and scan_start <= minute_time < force_exit:
            atm_strike = atm_index.get((minute, side))
            if atm_strike is None:
                continue
            morning_low_key = (date, atm_strike, side, "WEEK", 1)
            morning_low = morning_low_table.get(morning_low_key, float("nan"))
            if math.isnan(morning_low) if isinstance(morning_low, float) else False:
                continue
            row = row_at(atm_strike, minute)
            if row is None:
                continue
            st_val = float(row.get("st_value", float("nan")))
            st_dir = float(row.get("st_dir", float("nan")))

            if not evaluate_entry(
                st_value=st_val, st_dir=st_dir,
                morning_low=morning_low, band_pct=band_pct,
                bullish_required=bullish_required,
            ):
                continue

            # Need next-bar open for the SAME atm_strike to enter
            if next_minute is None:
                continue
            next_row = row_at(atm_strike, next_minute)
            if next_row is None:
                continue
            entry_price = float(next_row["open"])
            if math.isnan(entry_price):
                continue

            band_low = morning_low * (1 - band_pct / 100.0)
            band_high = morning_low * (1 + band_pct / 100.0)
            sl = entry_price * (1 - sl_pct / 100.0)
            tp = entry_price * (1 + tp_pct / 100.0)

            state = {
                "status": "OPEN",
                "strike": int(atm_strike),
                "morning_low": float(morning_low),
                "band_low": float(band_low),
                "band_high": float(band_high),
                "sl": float(sl),
                "tp": float(tp),
                "entry_time": _format_hhmm(next_minute),
                "entry_price": entry_price,
                "entry_st_value": st_val,
                "entry_trigger_close": float(row["close"]),
                "spot_at_entry": float(next_row.get("spot", 0.0)),
            }

    # End-of-day safety net: any position still OPEN gets force-closed at the last bar's close
    if state is not None and state["status"] == "OPEN":
        last_minute = minutes[-1]
        row = row_at(state["strike"], last_minute)
        if row is not None:
            exit_price = float(row["close"])
            pnl_points = exit_price - state["entry_price"]
            trades.append(StLowBandTrade(
                date=str(date),
                instrument=instrument,
                expiry_date=str(expiry_date),
                option_type=side,
                strike=int(state["strike"]),
                morning_low=state["morning_low"],
                band_low=state["band_low"],
                band_high=state["band_high"],
                spot_at_entry=state["spot_at_entry"],
                entry_time=state["entry_time"],
                entry_price=state["entry_price"],
                entry_st_value=state["entry_st_value"],
                entry_trigger_close=state["entry_trigger_close"],
                spot_at_exit=float(row.get("spot", 0.0)),
                exit_time=_format_hhmm(last_minute),
                exit_price=exit_price,
                exit_reason="EOD",
                pnl_points=float(pnl_points),
                pnl_inr=float(pnl_points) * lot_size_total,
                lot_size=int(lot_size_total),
            ))

    return trades
```

- [ ] **Step 8.5: Run the entry test to verify it passes**

Run: `pytest tests/test_supertrend_low_band.py::TestRunMachineForDaySide::test_entry_fires_when_st_in_band_and_bullish -v`
Expected: PASS.

- [ ] **Step 8.6: Add SL exit, TP exit, no-entry, force-exit tests**

Append to `TestRunMachineForDaySide`:

```python
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
        atm_index = {(b["datetime"], "CE"): 23850 for _, b in df.iterrows()}

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(),
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert trades == []

    def test_no_entry_when_outside_band(self):
        bars = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": 80.0, "st_dir": ST_BULLISH},  # outside [95,105]
            {"time": "09:21", "open": 101, "high": 103, "low": 100, "close": 102,
             "st_value": 80.0, "st_dir": ST_BULLISH},
            {"time": "14:45", "open": 102, "high": 102, "low": 101, "close": 102,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {(df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0}
        atm_index = {(b["datetime"], "CE"): 23850 for _, b in df.iterrows()}

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(),
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
        atm_index = {(b["datetime"], "CE"): 23850 for _, b in df.iterrows()}

        # Disable bullish filter at force-exit so we don't re-enter
        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(),
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
        atm_index = {(b["datetime"], "CE"): 23850 for _, b in df.iterrows()}

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(),
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
        atm_index = {(b["datetime"], "CE"): 23850 for _, b in df.iterrows()}

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(),
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert trades[0].exit_reason == "EOD"
        assert trades[0].exit_time == "14:45"
        assert trades[0].exit_price == 100.0  # the 14:45 bar's close
```

- [ ] **Step 8.7: Run all state-machine tests**

Run: `pytest tests/test_supertrend_low_band.py::TestRunMachineForDaySide -v`
Expected: 5 tests pass (test_entry_fires + 4 new ones).

- [ ] **Step 8.8: Suggest commit**

```
feat(st-low-band): per-day per-side state machine with SL/TP/EOD exits
```

---

## Task 9: State machine — re-entry, strike lock, edge cases

**Files:**
- Modify: `tests/test_supertrend_low_band.py`

The state machine in Task 8 already supports same-day re-entry (the `state = None` reset after exit allows the same-minute IDLE branch to fire) and strike lock (the OPEN branch reads `state["strike"]` and never re-derives ATM during OPEN). This task adds tests to verify those behaviors and the skip-side edge cases.

- [ ] **Step 9.1: Add re-entry test**

Append to `TestRunMachineForDaySide` in `tests/test_supertrend_low_band.py`:

```python
    def test_same_day_re_entry_after_sl(self):
        # First trade: enter at 9:21 (open=101), SL at 9:22 (low=93)
        # Re-arm: at 9:22 close ST is back in band & bullish, enter at 9:23 open=95
        # Second trade: SL again or TP — for this test, run to EOD
        bars = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": 98.0, "st_dir": ST_BULLISH},
            {"time": "09:21", "open": 101, "high": 103, "low": 100, "close": 102,
             "st_value": 99.0, "st_dir": ST_BULLISH},
            # SL hit on 9:22 wick; close lands at 95, ST still in band & bullish → re-arm
            {"time": "09:22", "open": 102, "high": 103, "low": 93.0, "close": 95,
             "st_value": 100.0, "st_dir": ST_BULLISH},
            {"time": "09:23", "open": 95, "high": 96, "low": 95, "close": 96,
             "st_value": 100.0, "st_dir": ST_BULLISH},
            {"time": "14:45", "open": 96, "high": 96, "low": 96, "close": 96,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {(df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0}
        atm_index = {(b["datetime"], "CE"): 23850 for _, b in df.iterrows()}

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(),
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert len(trades) == 2
        assert trades[0].exit_reason == "SL"
        assert trades[1].entry_time == "09:23"
        assert trades[1].entry_price == 95.0
        assert trades[1].exit_reason == "EOD"

    def test_strike_lock_when_atm_shifts(self):
        # Position opens on strike 23850 at 9:21.
        # At 9:23 spot moves and ATM shifts to 23900, but the open trade stays on 23850.
        # 23850 doesn't hit SL/TP → exits at 14:45 EOD.
        # First build 23850 bars (the locked strike)
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
        # Bars for 23900 (becomes ATM at 9:23)
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
        # Mark 23900 as ATM from 09:23 onwards by mutating moneyness
        for i, row in df.iterrows():
            if row["strike"] == 23850 and row["time_only"] >= time(9, 23):
                df.at[i, "moneyness"] = "OTM"
            if row["strike"] == 23900 and row["time_only"] >= time(9, 23):
                df.at[i, "moneyness"] = "ATM"

        morning_low_table = {
            (df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0,
            (df.iloc[0]["date"], 23900, "CE", "WEEK", 1):  60.0,
        }
        atm_index = {}
        for _, b in df.iterrows():
            if b["moneyness"] == "ATM":
                atm_index[(b["datetime"], "CE")] = int(b["strike"])

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(),
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        # The single open position is on 23850 (locked); exits at EOD on 23850's bar
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
        morning_low_table = {}  # no morning low for this contract
        atm_index = {(b["datetime"], "CE"): 23850 for _, b in df.iterrows()}

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(),
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert trades == []

    def test_skip_when_st_value_nan(self):
        bars = [
            {"time": "09:20", "open": 100, "high": 102, "low": 99, "close": 101,
             "st_value": float("nan"), "st_dir": float("nan")},  # ST not warm yet
            {"time": "14:45", "open": 100, "high": 100, "low": 100, "close": 100,
             "st_value": float("nan"), "st_dir": float("nan")},
        ]
        df = _build_day_bars(23850, "CE", bars)
        morning_low_table = {(df.iloc[0]["date"], 23850, "CE", "WEEK", 1): 100.0}
        atm_index = {(b["datetime"], "CE"): 23850 for _, b in df.iterrows()}

        trades = run_machine_for_day_side(
            df, side="CE", date=df.iloc[0]["date"],
            expiry_date=pd.Timestamp("2026-04-13").date(),
            morning_low_table=morning_low_table, atm_index=atm_index,
            instrument="NIFTY", **self._params(),
        )
        assert trades == []
```

- [ ] **Step 9.2: Run all state-machine tests**

Run: `pytest tests/test_supertrend_low_band.py::TestRunMachineForDaySide -v`
Expected: 9 tests pass (5 from Task 8 + 4 new).

- [ ] **Step 9.3: Suggest commit**

```
test(st-low-band): re-entry, strike lock, and skip-side edge cases
```

---

## Task 10: `run_backtest` top-level driver

**Files:**
- Modify: `engine/supertrend_low_band_backtest.py`
- Modify: `tests/test_supertrend_low_band.py`

Top-level driver that loads data, computes the precomputation pipeline, then iterates trading days × sides and aggregates trades. Loader is parametrizable for testing.

- [ ] **Step 10.1: Write failing integration test with synthetic loader**

Add to `tests/test_supertrend_low_band.py`:

```python
from engine.supertrend_low_band_backtest import run_backtest  # noqa: E402


class TestRunBacktest:
    def test_runs_with_synthetic_loader_and_emits_trade(self):
        # Build a single day where ST is in band & bullish at 9:25 → entry at 9:26
        # Day price walks up to TP
        date_str = "2026-04-08"
        bars = []
        # Need at least 11 bars before 9:25 for ST to warm up. Add 9:15-9:24 with stable price.
        for i in range(10):
            t = f"09:{15 + i:02d}"
            bars.append({
                "time": t, "open": 100, "high": 100.5, "low": 99.5, "close": 100,
            })
        # Trigger bar at 9:25: ST in band, bullish (will be computed by pipeline)
        bars.append({"time": "09:25", "open": 100, "high": 100.5, "low": 99.5, "close": 100})
        # Entry at 9:26 open
        bars.append({"time": "09:26", "open": 100, "high": 101.0, "low": 99.0, "close": 100})
        # TP wick at 9:30
        bars.append({"time": "09:30", "open": 100, "high": 115.0, "low": 100,  "close": 110})
        # End-of-day filler
        bars.append({"time": "14:45", "open": 110, "high": 110, "low": 110, "close": 110})

        df = _build_day_bars(23850, "CE", bars)
        df_pe = _build_day_bars(23850, "PE", bars, atm_strike=23850)
        all_df = pd.concat([df, df_pe], ignore_index=True)

        def synthetic_loader(start, end):
            return all_df

        config = {
            "instrument": "NIFTY",
            "supertrend": {"factor": 3, "atr_period": 10},
            "first_5min_window": {"start": "09:15", "end": "09:20"},
            "band_pct": 5.0,
            "sl_pct": 7.5,
            "tp_pct": 10.0,
            "trading": {"scan_start": "09:20", "force_exit": "14:45"},
            "lot_size": 1,
            "backtest_start": date_str,
            "backtest_end":   date_str,
        }
        trades = run_backtest(config, loader=synthetic_loader)
        # Both CE and PE should produce a TP trade (price walks up identically)
        assert len(trades) >= 1
        assert all(t.exit_reason in ("TP", "SL", "EOD") for t in trades)
        assert all(t.instrument == "NIFTY" for t in trades)
```

- [ ] **Step 10.2: Run test to verify it fails**

Run: `pytest tests/test_supertrend_low_band.py::TestRunBacktest -v`
Expected: ImportError on `run_backtest`.

- [ ] **Step 10.3: Implement `run_backtest`**

Add to `engine/supertrend_low_band_backtest.py`:

```python
from engine.data_loader import load_data


def _parse_hhmm(s: str) -> _time:
    hh, mm = s.split(":")
    return _time(int(hh), int(mm))


def _default_loader(start: str, end: str) -> pd.DataFrame:
    """Default loader: read NIFTY weekly options 1-min parquet via load_data."""
    path = os.path.join(BASE_DIR, DATA_PATH["NIFTY"])
    df = load_data(path, start, end, "weekly")
    # load_data already adds `date` and `time_only` columns
    return df


def run_backtest(
    config: dict,
    loader: Optional[Callable[[str, str], pd.DataFrame]] = None,
) -> List[StLowBandTrade]:
    """Run the SuperTrend Low-Band backtest for the given config.

    `loader(start, end)` returns a DataFrame of 1-min option bars filtered to
    weekly nearest expiry, with columns: datetime, date, time_only, strike,
    option_type, expiry_type, expiry_code, moneyness, open, high, low, close,
    spot, atm_strike. Default loader uses config.DATA_PATH['NIFTY'].
    """
    if loader is None:
        loader = _default_loader

    instrument = config["instrument"]
    if instrument != "NIFTY":
        raise ValueError(f"v1 supports only NIFTY; got {instrument!r}")

    st_cfg = config["supertrend"]
    factor = int(st_cfg["factor"])
    atr_period = int(st_cfg["atr_period"])

    win_cfg = config["first_5min_window"]
    window_start = _parse_hhmm(win_cfg["start"])
    window_end = _parse_hhmm(win_cfg["end"])

    band_pct = float(config["band_pct"])
    sl_pct = float(config["sl_pct"])
    tp_pct = float(config["tp_pct"])

    trading = config["trading"]
    scan_start = _parse_hhmm(trading["scan_start"])
    force_exit = _parse_hhmm(trading["force_exit"])

    lot_multiplier = int(config.get("lot_size", 1))
    lot_size_total = LOT_SIZE[instrument] * lot_multiplier

    start = config["backtest_start"]
    end = config["backtest_end"]

    df = loader(start, end)
    if df.empty:
        return []

    # Pipeline: continuous ST → first-5min low table → ATM index
    df = compute_continuous_supertrend_per_contract(df, factor=factor, atr_period=atr_period)
    morning_low_table = compute_first_5min_low_table(df, window_start, window_end)
    # Build atm_index across the whole loaded df once (saves work per day)
    atm_index = build_atm_index(df)

    all_trades: List[StLowBandTrade] = []
    for trading_date, day_df in df.groupby("date"):
        # Pick the day's expiry_date (whatever weekly is loaded). The data is already
        # filtered to expiry_code==1, so just take the most common expiry seen.
        if "expiry_date" in day_df.columns:
            expiry_date = day_df["expiry_date"].iloc[0]
            if isinstance(expiry_date, str):
                expiry_date = pd.Timestamp(expiry_date).date()
            elif hasattr(expiry_date, "date"):
                expiry_date = expiry_date.date() if not isinstance(expiry_date, _date) else expiry_date
        else:
            expiry_date = trading_date  # fallback

        for side in ("CE", "PE"):
            trades = run_machine_for_day_side(
                day_df, side=side,
                date=trading_date, expiry_date=expiry_date,
                morning_low_table=morning_low_table,
                atm_index=atm_index,
                instrument=instrument,
                band_pct=band_pct, sl_pct=sl_pct, tp_pct=tp_pct,
                scan_start=scan_start, force_exit=force_exit,
                bullish_required=True,
                lot_size_total=lot_size_total,
            )
            all_trades.extend(trades)

    return all_trades
```

- [ ] **Step 10.4: Run test to verify it passes**

Run: `pytest tests/test_supertrend_low_band.py::TestRunBacktest -v`
Expected: PASS.

- [ ] **Step 10.5: Suggest commit**

```
feat(st-low-band): run_backtest top-level driver
```

---

## Task 11: `summarize_trades` + output helpers

**Files:**
- Modify: `engine/supertrend_low_band_backtest.py`
- Modify: `tests/test_supertrend_low_band.py`

- [ ] **Step 11.1: Write failing tests**

Add to `tests/test_supertrend_low_band.py`:

```python
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
```

- [ ] **Step 11.2: Run test to verify it fails**

Run: `pytest tests/test_supertrend_low_band.py::TestSummarizeTrades tests/test_supertrend_low_band.py::TestWriteTradesCsv -v`
Expected: ImportError on `summarize_trades` and `write_trades_csv`.

- [ ] **Step 11.3: Implement helpers**

Add to `engine/supertrend_low_band_backtest.py`:

```python
from collections import defaultdict


def summarize_trades(trades: List[StLowBandTrade]) -> dict:
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "total_pnl_points": 0.0, "total_pnl_inr": 0.0,
            "by_side": {},
        }
    wins = sum(1 for t in trades if t.pnl_points > 0)
    losses = sum(1 for t in trades if t.pnl_points <= 0)
    total_points = sum(t.pnl_points for t in trades)
    total_inr = sum(t.pnl_inr for t in trades)

    by_side = defaultdict(lambda: {"trades": 0, "pnl_inr": 0.0, "pnl_points": 0.0})
    for t in trades:
        by_side[t.option_type]["trades"] += 1
        by_side[t.option_type]["pnl_inr"] += t.pnl_inr
        by_side[t.option_type]["pnl_points"] += t.pnl_points

    return {
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades),
        "total_pnl_points": total_points,
        "total_pnl_inr": total_inr,
        "by_side": dict(by_side),
    }


def write_trades_csv(trades: List[StLowBandTrade], path: str) -> None:
    """Write trades to CSV. Creates header-only file when empty."""
    if trades:
        df = trades_to_dataframe(trades)
    else:
        df = pd.DataFrame(columns=[f.name for f in fields(StLowBandTrade)])
    df.to_csv(path, index=False)
```

- [ ] **Step 11.4: Run tests to verify they pass**

Run: `pytest tests/test_supertrend_low_band.py::TestSummarizeTrades tests/test_supertrend_low_band.py::TestWriteTradesCsv -v`
Expected: 4 tests pass.

- [ ] **Step 11.5: Suggest commit**

```
feat(st-low-band): summarize_trades + write_trades_csv
```

---

## Task 12: Bar-timestamp convention assertion (integration test)

**Files:**
- Modify: `tests/test_supertrend_low_band.py`

This is a guardrail. If the parquet ever switches to close-stamped bars, the entire morning-low computation would shift by one minute. Fail loud.

- [ ] **Step 12.1: Write the assertion test**

Append to `tests/test_supertrend_low_band.py`:

```python
import os  # noqa: E402

from config import DATA_PATH  # noqa: E402

DATA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    DATA_PATH["NIFTY"],
)


@pytest.mark.skipif(not os.path.exists(DATA_FILE), reason="NIFTY parquet not available")
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
        # Take the first trading day in data
        df["date"] = df["datetime"].dt.date
        first_day = df["date"].min()
        first_day_rows = df[df["date"] == first_day]
        first_minute = first_day_rows["datetime"].min()
        assert first_minute.strftime("%H:%M") == "09:15", (
            f"Expected first bar of day to be timestamped 09:15 (open-stamp), "
            f"got {first_minute.strftime('%H:%M')}. The morning-low window "
            f"in the engine and spec must be revisited."
        )
```

- [ ] **Step 12.2: Run the test**

Run: `pytest tests/test_supertrend_low_band.py::TestBarTimestampConvention -v`
Expected: PASS (assuming the parquet data is present locally).

- [ ] **Step 12.3: Suggest commit**

```
test(st-low-band): assert bar timestamp is open-stamped (guardrail)
```

---

## Task 13: Real-day integration test

**Files:**
- Modify: `tests/test_supertrend_low_band.py`

Run `run_backtest` for one real NIFTY trading day. This catches integration issues (column names, dtypes, expiry alignment) that synthetic tests miss. Don't hardcode trade outputs — just assert that the run completes and the trade schema is intact.

- [ ] **Step 13.1: Write the integration test**

Append to `tests/test_supertrend_low_band.py`:

```python
@pytest.mark.skipif(not os.path.exists(DATA_FILE), reason="NIFTY parquet not available")
class TestRealDayIntegration:
    def test_one_day_end_to_end(self):
        # Pick a known weekly expiry day
        config = {
            "instrument": "NIFTY",
            "supertrend": {"factor": 3, "atr_period": 10},
            "first_5min_window": {"start": "09:15", "end": "09:20"},
            "band_pct": 5.0,
            "sl_pct": 7.5,
            "tp_pct": 10.0,
            "trading": {"scan_start": "09:20", "force_exit": "14:45"},
            "lot_size": 1,
            "backtest_start": "2026-04-08",
            "backtest_end":   "2026-04-08",
        }
        trades = run_backtest(config)
        # Schema check
        for t in trades:
            assert t.option_type in ("CE", "PE")
            assert t.exit_reason in ("SL", "TP", "EOD")
            assert t.entry_price > 0
            assert t.exit_price > 0
            assert isinstance(t.strike, int)
            assert t.lot_size == LOT_SIZE["NIFTY"]
        # Sanity: the engine should at minimum complete; we don't assert trade count
        assert isinstance(trades, list)
```

- [ ] **Step 13.2: Run the test**

Run: `pytest tests/test_supertrend_low_band.py::TestRealDayIntegration -v`
Expected: PASS. Watch the log output for any data shape surprises.

- [ ] **Step 13.3: Suggest commit**

```
test(st-low-band): real-day end-to-end integration test
```

---

## Task 14: Streamlit UI runner

**Files:**
- Create: `ui/supertrend_low_band_backtest_runner.py`

- [ ] **Step 14.1: Create the Streamlit module**

Write `ui/supertrend_low_band_backtest_runner.py`:

```python
"""Streamlit form + runner for the SuperTrend Low-Band backtest."""
import json
import os
from datetime import date
from io import StringIO

import pandas as pd
import streamlit as st

from engine.supertrend_low_band_backtest import (
    run_backtest,
    summarize_trades,
    trades_to_dataframe,
)


DEFAULT_CONFIG_PATH = "saved_strategies/supertrend_low_band.json"


def _load_default_config() -> dict:
    if os.path.exists(DEFAULT_CONFIG_PATH):
        with open(DEFAULT_CONFIG_PATH) as f:
            return json.load(f)
    return {
        "instrument": "NIFTY",
        "supertrend": {"factor": 3, "atr_period": 10},
        "first_5min_window": {"start": "09:15", "end": "09:20"},
        "band_pct": 5.0,
        "sl_pct": 7.5,
        "tp_pct": 10.0,
        "trading": {"scan_start": "09:20", "force_exit": "14:45"},
        "lot_size": 1,
        "backtest_start": "2025-01-01",
        "backtest_end":   "2026-04-30",
    }


def render_supertrend_low_band_backtest() -> None:
    st.header("SuperTrend Low-Band — Daily ATM Reversal")
    st.caption(
        "Buys NIFTY weekly ATM CE/PE when continuous SuperTrend(3,10) is "
        "bullish AND its value is within ±5% of the contract's 9:15-9:19 "
        "morning low. TP 10%, SL 7.5%, force-exit 14:45. CE/PE run as "
        "fully independent state machines."
    )

    cfg = _load_default_config()

    st.subheader("Indicator")
    c1, c2 = st.columns(2)
    factor = c1.number_input(
        "SuperTrend factor", min_value=1, max_value=20,
        value=int(cfg["supertrend"]["factor"]), key="stlb_factor",
    )
    atr_period = c2.number_input(
        "SuperTrend atr_period", min_value=2, max_value=50,
        value=int(cfg["supertrend"]["atr_period"]), key="stlb_atr",
    )

    st.subheader("Levels")
    c1, c2, c3 = st.columns(3)
    band_pct = c1.number_input(
        "Band % around morning low", min_value=0.5, max_value=20.0,
        value=float(cfg["band_pct"]), step=0.5, key="stlb_band",
    )
    sl_pct = c2.number_input(
        "SL %", min_value=0.5, max_value=50.0,
        value=float(cfg["sl_pct"]), step=0.5, key="stlb_sl",
    )
    tp_pct = c3.number_input(
        "TP %", min_value=0.5, max_value=100.0,
        value=float(cfg["tp_pct"]), step=0.5, key="stlb_tp",
    )

    st.subheader("Times (HH:MM)")
    c1, c2, c3, c4 = st.columns(4)
    win_start = c1.text_input(
        "First-5min start", value=cfg["first_5min_window"]["start"], key="stlb_winstart",
    )
    win_end = c2.text_input(
        "First-5min end (excl.)", value=cfg["first_5min_window"]["end"], key="stlb_winend",
    )
    scan_start = c3.text_input(
        "Scan start", value=cfg["trading"]["scan_start"], key="stlb_scan",
    )
    force_exit = c4.text_input(
        "Force exit", value=cfg["trading"]["force_exit"], key="stlb_force",
    )

    st.subheader("Run window")
    c1, c2, c3 = st.columns(3)
    start_date = c1.date_input(
        "Start", value=date.fromisoformat(cfg["backtest_start"]), key="stlb_start",
    )
    end_date = c2.date_input(
        "End", value=date.fromisoformat(cfg["backtest_end"]), key="stlb_end",
    )
    lot_multiplier = c3.number_input(
        "Lots (multiplier)", min_value=1, value=int(cfg.get("lot_size", 1)), key="stlb_lots",
    )

    if not st.button("Run backtest", type="primary"):
        return

    run_config = {
        "instrument": "NIFTY",
        "supertrend": {"factor": int(factor), "atr_period": int(atr_period)},
        "first_5min_window": {"start": win_start, "end": win_end},
        "band_pct": float(band_pct),
        "sl_pct": float(sl_pct),
        "tp_pct": float(tp_pct),
        "trading": {"scan_start": scan_start, "force_exit": force_exit},
        "lot_size": int(lot_multiplier),
        "backtest_start": start_date.isoformat(),
        "backtest_end":   end_date.isoformat(),
    }

    with st.spinner("Running..."):
        trades = run_backtest(run_config)

    summary = summarize_trades(trades)
    st.subheader("Summary")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Trades", summary["total_trades"])
    c2.metric("Win rate", f"{summary['win_rate'] * 100:.1f}%")
    c3.metric("Total points", f"{summary['total_pnl_points']:.2f}")
    c4.metric("Total INR", f"{summary['total_pnl_inr']:,.0f}")
    if summary["by_side"]:
        st.write("By side:", summary["by_side"])

    if not trades:
        st.info("No trades generated in this window.")
        return

    df = trades_to_dataframe(trades)

    # Equity curve
    st.subheader("Equity curve")
    equity = df["pnl_inr"].cumsum()
    chart_df = pd.DataFrame(
        {"Cumulative P&L (₹)": equity.values},
        index=range(1, len(equity) + 1),
    )
    chart_df.index.name = "Trade #"
    st.line_chart(chart_df)

    # Trades table
    st.subheader("Trades")
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        filter_type = st.multiselect(
            "Filter by option type", options=["CE", "PE"], key="stlb_ftype",
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason", options=["TP", "SL", "EOD"], key="stlb_freason",
        )
    with fc3:
        filter_date = st.text_input(
            "Filter by date (YYYY-MM-DD)", value="", key="stlb_fdate",
        )
    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]
    if filter_date:
        filtered = filtered[filtered["date"] == filter_date]
    st.dataframe(filtered, use_container_width=True)

    # CSV download
    csv_buf = StringIO()
    df.to_csv(csv_buf, index=False)
    st.download_button(
        "Download CSV",
        data=csv_buf.getvalue(),
        file_name=f"supertrend_low_band_{start_date}_{end_date}.csv",
        mime="text/csv",
    )
```

- [ ] **Step 14.2: Smoke-import the module**

Run: `python -c "from ui.supertrend_low_band_backtest_runner import render_supertrend_low_band_backtest; print('ok')"`
Expected: `ok`.

- [ ] **Step 14.3: Suggest commit**

```
feat(st-low-band): Streamlit form runner with equity curve + trades table
```

---

## Task 15: Wire into `app.py`

**Files:**
- Modify: `app.py`

- [ ] **Step 15.1: Add the import**

Edit `app.py` — find the line at `app.py:32`:

```python
from ui.gamma_blast_backtest_runner import render_gamma_blast_backtest
```

Insert AFTER it:

```python
from ui.supertrend_low_band_backtest_runner import render_supertrend_low_band_backtest
```

- [ ] **Step 15.2: Add the tab to the tab tuple**

Edit `app.py` — at line 49, the `st.tabs([...])` call. Replace:

```python
tab_dashboard, tab_trades, tab_backtest, tab_forward, tab_dema_st, tab_st_ema, tab_straddle_vwap, tab_boom, tab_boom_st, tab_vwap_ema_rsi, tab_bb_reversal, tab_bb_reversal_pine, tab_bb_reversal_pine_exit, tab_prt, tab_ha_nr7, tab_ema5_fut, tab_gamma_blast = st.tabs([
    "📊 Dashboard",
    "📋 Trade Explorer",
    "🚀 Run Backtest",
    "📡 Forward Test",
    "🔄 DEMA-ST Pullback",
    "🎯 ST+EMA Pullback",
    "📉 Straddle VWAP",
    "💥 Boom SMA",
    "💥 Boom ST",
    "📈 VWAP-EMA-RSI",
    "🎯 BB Reversal PE",
    "📌 BB Reversal PE-pinescript",
    "📌 BB Reversal PE-pinescript-exit",
    "📊 PRT Strategy",
    "🎲 HA-NR7",
    "📈 EMA5 Futures",
    "💥 Gamma Blast",
])
```

With:

```python
tab_dashboard, tab_trades, tab_backtest, tab_forward, tab_dema_st, tab_st_ema, tab_straddle_vwap, tab_boom, tab_boom_st, tab_vwap_ema_rsi, tab_bb_reversal, tab_bb_reversal_pine, tab_bb_reversal_pine_exit, tab_prt, tab_ha_nr7, tab_ema5_fut, tab_gamma_blast, tab_st_low_band = st.tabs([
    "📊 Dashboard",
    "📋 Trade Explorer",
    "🚀 Run Backtest",
    "📡 Forward Test",
    "🔄 DEMA-ST Pullback",
    "🎯 ST+EMA Pullback",
    "📉 Straddle VWAP",
    "💥 Boom SMA",
    "💥 Boom ST",
    "📈 VWAP-EMA-RSI",
    "🎯 BB Reversal PE",
    "📌 BB Reversal PE-pinescript",
    "📌 BB Reversal PE-pinescript-exit",
    "📊 PRT Strategy",
    "🎲 HA-NR7",
    "📈 EMA5 Futures",
    "💥 Gamma Blast",
    "🎯 ST Low-Band",
])
```

- [ ] **Step 15.3: Add the tab body**

Edit `app.py` — at the bottom of the file (after the existing `with tab_gamma_blast:` block at line 117-118), append:

```python

with tab_st_low_band:
    render_supertrend_low_band_backtest()
```

- [ ] **Step 15.4: Smoke-test the app starts**

Run: `python -c "import app" 2>&1 | head -20`
Expected: No import errors. (Streamlit will warn about missing context if run outside `streamlit run`, that's fine.)

- [ ] **Step 15.5: Manual UI smoke test**

Run: `streamlit run app.py --server.headless true --server.port 8888 &` then in another terminal `curl -s http://localhost:8888/ -o /dev/null && echo ok`.
Expected: `ok`. (Or open the URL in a browser, click the new tab, run a 1-day backtest.)

Stop the server when done: `pkill -f 'streamlit run app.py'`.

- [ ] **Step 15.6: Suggest commit**

```
feat(st-low-band): wire SuperTrend Low-Band tab into app.py
```

---

## Task 16: Run full test suite + quick sanity backtest

**Files:** none modified.

- [ ] **Step 16.1: Run the full test module**

Run: `pytest tests/test_supertrend_low_band.py -v`
Expected: All tests pass. Note the count of state-machine and integration tests.

- [ ] **Step 16.2: Run a one-week backtest from the CLI**

Run:

```bash
python -c "
from engine.supertrend_low_band_backtest import run_backtest, summarize_trades, write_trades_csv
config = {
  'instrument': 'NIFTY',
  'supertrend': {'factor': 3, 'atr_period': 10},
  'first_5min_window': {'start': '09:15', 'end': '09:20'},
  'band_pct': 5.0, 'sl_pct': 7.5, 'tp_pct': 10.0,
  'trading': {'scan_start': '09:20', 'force_exit': '14:45'},
  'lot_size': 1,
  'backtest_start': '2026-04-01',
  'backtest_end':   '2026-04-08',
}
trades = run_backtest(config)
print(summarize_trades(trades))
write_trades_csv(trades, 'st_low_band_smoke.csv')
print(f'Wrote {len(trades)} trades to st_low_band_smoke.csv')
"
```

Expected: a summary dict prints; `st_low_band_smoke.csv` is written. Number of trades is data-dependent — the test is just that the run completes and the CSV is well-formed.

- [ ] **Step 16.3: Eyeball the CSV**

Run: `head -3 st_low_band_smoke.csv`
Expected: header row matches the spec's trade schema; trade rows have realistic numbers.

- [ ] **Step 16.4: Clean up smoke artifact**

Run: `rm st_low_band_smoke.csv`

---

## Self-Review

**Spec coverage check** (each spec section ↔ task that implements it):

| Spec section | Task |
|---|---|
| Trade dataclass + JSON config | Task 1 |
| `is_in_band` | Task 2 |
| Bullish + band entry rule | Task 3 |
| SL/TP/EOD precedence | Task 4 |
| Per-(date, contract) morning low | Task 5 |
| Continuous SuperTrend per contract | Task 6 |
| ATM index per minute | Task 7 |
| Per-side state machine (entry, exits, force-exit safety) | Tasks 8-9 |
| Strike lock | Task 9 |
| Same-day re-entry | Task 9 |
| Skip-side edge cases | Task 9 |
| `run_backtest` driver | Task 10 |
| Output helpers (`summarize_trades`, `write_trades_csv`) | Task 11 |
| Bar-timestamp guardrail | Task 12 |
| Real-day integration | Task 13 |
| Streamlit UI form + equity curve + filters + CSV download | Task 14 |
| `app.py` tab wiring | Task 15 |
| Full smoke test | Task 16 |

**Placeholder scan:** No "TBD", no "implement later", every code block is complete. Each test asserts specific values.

**Type consistency:**
- `StLowBandTrade` field names match the schema in the spec, the test fixtures, the dataclass definition, and the CSV writer (which uses `dataclass.fields()`).
- `ST_BULLISH = -1.0` defined once and reused in `evaluate_entry`, the state machine, and tests.
- `evaluate_exit` returns `Optional[Tuple[float, str]]` consistently.
- `run_machine_for_day_side` parameter names (`band_pct`, `sl_pct`, `tp_pct`, `scan_start`, `force_exit`, `bullish_required`, `lot_size_total`) match the call site in `run_backtest`.
- Helper-function output shapes (dict for `compute_first_5min_low_table`, dict for `build_atm_index`, DataFrame for `compute_continuous_supertrend_per_contract`) are documented in their docstrings and match consumer code.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-05-supertrend-low-band.md`.

Per the user's memory ("No subagents for plan execution — do the work inline"), the recommended execution mode is:

**Inline Execution** — REQUIRED SUB-SKILL: superpowers:executing-plans. Tasks are run end-to-end in this session with checkpoints between major sections (after Task 4 / 7 / 11 / 13). User runs the suggested commit after each task; assistant does not run `git`.
