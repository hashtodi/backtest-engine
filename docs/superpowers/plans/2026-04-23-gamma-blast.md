# Gamma Blast Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dedicated expiry-day-only backtest engine that buys ATM CE/PE on NIFTY and/or SENSEX after a premium crush-and-reverse pattern, with per-instrument configurable alert/entry/SL/TP levels and Streamlit UI integration.

**Architecture:** A dedicated module `engine/gamma_blast_backtest.py` hosts the whole engine: dataclasses, a per-option-type state machine (IDLE → ARMED → OPEN), a per-day runner that drives four machines in parallel (CE/PE × NIFTY/SENSEX), a backtest loop that iterates expiry days, and output helpers. The state machine is pure (operates on bar-by-bar inputs) and unit-tested with synthetic bars. Data access reuses `engine.data_loader` and the weekly-expiry lists from `config.py`.

**Tech Stack:** Python 3.10+, pandas, pyarrow (parquet), pytest, Streamlit.

**Spec:** `docs/superpowers/specs/2026-04-23-gamma-blast-design.md`

> **⚠ Git policy for the implementer:** the `git add` / `git commit` snippets in each task are **commit message suggestions for the user to run**, not commands to be auto-executed. Per `~/.claude/CLAUDE.md`, Claude does not run git write commands in this repo. After each step's code is green, present the suggested commit message for the user to copy-paste, then proceed to the next step.

---

## File Structure

| File | Purpose | Status |
|---|---|---|
| `engine/gamma_blast_backtest.py` | All engine logic — dataclasses, state machine, runner, backtest driver, output helpers | Create |
| `saved_strategies/gamma_blast.json` | Default strategy config with SENSEX levels prefilled, NIFTY params null | Create |
| `tests/test_gamma_blast.py` | Unit tests (synthetic bars) + one integration test (real parquet) | Create |
| `ui/gamma_blast_backtest_runner.py` | Streamlit form + runner | Create |
| `config.py` | Add SENSEX entry to `SPOT_DATA_PATH` | Modify (1 line) |
| `app.py` | Register `tab_gamma_blast` tab | Modify (3 lines) |

---

## Task 1: Config plumbing — SENSEX spot path + strategy JSON

**Files:**
- Modify: `config.py:68-70`
- Create: `saved_strategies/gamma_blast.json`

- [ ] **Step 1: Add SENSEX entry to `SPOT_DATA_PATH`**

Edit `config.py` to add SENSEX alongside NIFTY:

```python
SPOT_DATA_PATH = {
    'NIFTY': 'data/spot/nifty/NIFTY_1m.parquet',
    'SENSEX': 'data/spot/sensex/SENSEX_1m.parquet',
}
```

- [ ] **Step 2: Verify the config still imports cleanly**

Run: `python3 -c "import config; print(config.SPOT_DATA_PATH)"`
Expected: dict with both NIFTY and SENSEX keys and their paths.

- [ ] **Step 3: Create the strategy JSON**

Write `saved_strategies/gamma_blast.json`:

```json
{
  "name": "gamma_blast",
  "strategy_type": "gamma_blast",
  "instruments": ["NIFTY", "SENSEX"],
  "params": {
    "NIFTY":  { "alert_price": null, "entry_price": null, "sl": null, "tp": null },
    "SENSEX": { "alert_price": 20,   "entry_price": 40,   "sl": 15,   "tp": 80   }
  },
  "timing": {
    "arm_start":      "10:00",
    "arm_deadline":   "15:00",
    "entry_deadline": "15:05",
    "force_exit":     "15:15"
  },
  "lot_size": 1,
  "backtest_start": "2025-01-01",
  "backtest_end":   "2026-04-13"
}
```

- [ ] **Step 4: Commit**

```bash
git add config.py saved_strategies/gamma_blast.json
git commit -m "feat(gamma-blast): add SENSEX spot path + strategy JSON"
```

---

## Task 2: Trade dataclass + CSV serialization

**Files:**
- Create: `engine/gamma_blast_backtest.py`
- Create: `tests/test_gamma_blast.py`

- [ ] **Step 1: Write the failing test for the dataclass**

Create `tests/test_gamma_blast.py`:

```python
"""Tests for Gamma Blast backtest engine."""
import pandas as pd
import pytest

from engine.gamma_blast_backtest import (
    GammaBlastTrade,
    trades_to_dataframe,
)


def make_trade(**overrides) -> GammaBlastTrade:
    defaults = dict(
        date="2026-02-26",
        instrument="SENSEX",
        expiry_date="2026-02-26",
        option_type="CE",
        strike=81000,
        spot_at_arm=80950.0,
        arm_time="11:00",
        arm_premium=18.0,
        spot_at_entry=81150.0,
        entry_time="12:31",
        entry_price=47.0,
        entry_trigger_close=45.0,
        spot_at_exit=81380.0,
        exit_time="13:10",
        exit_price=80.0,
        exit_reason="TP",
        pnl_points=33.0,
        pnl_inr=660.0,
        lot_size=20,
    )
    defaults.update(overrides)
    return GammaBlastTrade(**defaults)


class TestGammaBlastTradeDataclass:
    def test_all_fields_present(self):
        t = make_trade()
        assert t.date == "2026-02-26"
        assert t.instrument == "SENSEX"
        assert t.option_type == "CE"
        assert t.strike == 81000
        assert t.arm_premium == 18.0
        assert t.entry_price == 47.0
        assert t.exit_price == 80.0
        assert t.exit_reason == "TP"
        assert t.pnl_points == 33.0
        assert t.pnl_inr == 660.0

    def test_trades_to_dataframe_empty(self):
        df = trades_to_dataframe([])
        assert df.empty

    def test_trades_to_dataframe_roundtrip(self):
        trades = [make_trade(), make_trade(option_type="PE", exit_reason="SL")]
        df = trades_to_dataframe(trades)
        assert len(df) == 2
        assert set(df.columns) >= {
            "date", "instrument", "expiry_date", "option_type", "strike",
            "spot_at_arm", "arm_time", "arm_premium",
            "spot_at_entry", "entry_time", "entry_price", "entry_trigger_close",
            "spot_at_exit", "exit_time", "exit_price", "exit_reason",
            "pnl_points", "pnl_inr", "lot_size",
        }
        assert df.iloc[1]["option_type"] == "PE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gamma_blast.py::TestGammaBlastTradeDataclass -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.gamma_blast_backtest'`.

- [ ] **Step 3: Implement the dataclass and serializer**

Create `engine/gamma_blast_backtest.py`:

```python
"""
Gamma Blast Backtest Engine.

Strategy:
  Expiry-day only. Buy ATM CE or PE on NIFTY/SENSEX after its premium
  is crushed below a configurable alert level, then recovers above an
  entry level. Fixed-absolute SL and TP. Independent CE/PE machines,
  independent per-instrument P&L. See design spec at
  docs/superpowers/specs/2026-04-23-gamma-blast-design.md.
"""

import logging
from dataclasses import asdict, dataclass
from typing import List

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class GammaBlastTrade:
    date: str
    instrument: str
    expiry_date: str
    option_type: str           # "CE" or "PE"
    strike: int

    spot_at_arm: float
    arm_time: str              # "HH:MM"
    arm_premium: float

    spot_at_entry: float
    entry_time: str
    entry_price: float
    entry_trigger_close: float

    spot_at_exit: float
    exit_time: str
    exit_price: float
    exit_reason: str           # "SL" | "TP" | "EOD"

    pnl_points: float
    pnl_inr: float
    lot_size: int


def trades_to_dataframe(trades: List[GammaBlastTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_gamma_blast.py::TestGammaBlastTradeDataclass -v`
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add engine/gamma_blast_backtest.py tests/test_gamma_blast.py
git commit -m "feat(gamma-blast): add GammaBlastTrade dataclass + trades_to_dataframe"
```

---

## Task 3: State machine primitives (IDLE → ARMED transition)

**Files:**
- Modify: `engine/gamma_blast_backtest.py`
- Modify: `tests/test_gamma_blast.py`

- [ ] **Step 1: Add failing tests for `should_arm` helper**

Append to `tests/test_gamma_blast.py`:

```python
from datetime import time

from engine.gamma_blast_backtest import should_arm


class TestShouldArm:
    """Arm condition: close < alert_price AND time in [arm_start, arm_deadline]."""

    def test_armed_when_close_below_alert(self):
        assert should_arm(atm_close=19.0, alert_price=20, bar_time=time(11, 0),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is True

    def test_not_armed_at_boundary_equal(self):
        # Strict < alert_price
        assert should_arm(atm_close=20.0, alert_price=20, bar_time=time(11, 0),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is False

    def test_not_armed_above_alert(self):
        assert should_arm(atm_close=21.0, alert_price=20, bar_time=time(11, 0),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is False

    def test_not_armed_before_arm_start(self):
        assert should_arm(atm_close=10.0, alert_price=20, bar_time=time(9, 59),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is False

    def test_armed_at_arm_start_boundary(self):
        assert should_arm(atm_close=10.0, alert_price=20, bar_time=time(10, 0),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is True

    def test_armed_at_arm_deadline_boundary(self):
        assert should_arm(atm_close=10.0, alert_price=20, bar_time=time(15, 0),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is True

    def test_not_armed_after_arm_deadline(self):
        assert should_arm(atm_close=10.0, alert_price=20, bar_time=time(15, 1),
                          arm_start=time(10, 0), arm_deadline=time(15, 0)) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gamma_blast.py::TestShouldArm -v`
Expected: FAIL with `ImportError: cannot import name 'should_arm'`.

- [ ] **Step 3: Implement `should_arm`**

Append to `engine/gamma_blast_backtest.py`:

```python
from datetime import time as _time


def should_arm(atm_close: float, alert_price: float, bar_time: _time,
               arm_start: _time, arm_deadline: _time) -> bool:
    """True iff this bar's ATM close triggers an arm transition.

    Arm condition is strict `<` alert_price; time window is inclusive on both ends.
    """
    if atm_close >= alert_price:
        return False
    return arm_start <= bar_time <= arm_deadline
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_gamma_blast.py::TestShouldArm -v`
Expected: 7 PASSED.

- [ ] **Step 5: Commit**

```bash
git add engine/gamma_blast_backtest.py tests/test_gamma_blast.py
git commit -m "feat(gamma-blast): add should_arm helper with strict < boundary"
```

---

## Task 4: Entry trigger logic (including same-bar whip + gap protection)

**Files:**
- Modify: `engine/gamma_blast_backtest.py`
- Modify: `tests/test_gamma_blast.py`

- [ ] **Step 1: Add failing tests for `evaluate_entry_trigger`**

Append to `tests/test_gamma_blast.py`:

```python
from engine.gamma_blast_backtest import evaluate_entry_trigger


class TestEvaluateEntryTrigger:
    """Given a trigger bar's data + next bar's open, decide entry action.

    Returns: ("enter", next_open) | ("skip", reason) | ("no_trigger", None)
    """

    def test_enter_when_close_above_entry_price(self):
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=45.0, bar_low=35.0,
            next_open=47.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=True,
        )
        assert result == ("enter", 47.0)

    def test_no_trigger_when_close_equal_entry_price(self):
        # Strictly greater
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=40.0, bar_low=35.0,
            next_open=42.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=True,
        )
        assert result == ("no_trigger", None)

    def test_no_trigger_when_close_below_entry_price(self):
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=39.0, bar_low=35.0,
            next_open=41.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=True,
        )
        assert result == ("no_trigger", None)

    def test_gap_beyond_tp_skip(self):
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=45.0, bar_low=35.0,
            next_open=85.0,  # > tp=80
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=True,
        )
        assert result == ("skip", "gap_above_tp")

    def test_gap_below_sl_skip(self):
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=45.0, bar_low=35.0,
            next_open=10.0,  # < sl=15
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=True,
        )
        assert result == ("skip", "gap_below_sl")

    def test_same_bar_whip_green_enters(self):
        # low <= alert_price AND close > entry_price AND green (close > open)
        result = evaluate_entry_trigger(
            bar_open=18.0, bar_close=45.0, bar_low=12.0,
            next_open=47.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=False,  # not yet armed — same-bar whip path
        )
        assert result == ("enter", 47.0)

    def test_same_bar_whip_red_skipped(self):
        # low <= alert AND close > entry but RED (close < open)
        result = evaluate_entry_trigger(
            bar_open=50.0, bar_close=45.0, bar_low=12.0,
            next_open=46.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=False,
        )
        assert result == ("no_trigger", None)

    def test_same_bar_whip_doji_skipped(self):
        result = evaluate_entry_trigger(
            bar_open=45.0, bar_close=45.0, bar_low=12.0,
            next_open=45.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=False,
        )
        assert result == ("no_trigger", None)

    def test_unarmed_no_whip_no_trigger(self):
        # close > entry but low NOT <= alert, and not already armed
        result = evaluate_entry_trigger(
            bar_open=38.0, bar_close=45.0, bar_low=30.0,
            next_open=47.0,
            alert_price=20, entry_price=40, sl=15, tp=80,
            already_armed=False,
        )
        assert result == ("no_trigger", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gamma_blast.py::TestEvaluateEntryTrigger -v`
Expected: FAIL with `ImportError: cannot import name 'evaluate_entry_trigger'`.

- [ ] **Step 3: Implement `evaluate_entry_trigger`**

Append to `engine/gamma_blast_backtest.py`:

```python
from typing import Optional, Tuple


def evaluate_entry_trigger(
    bar_open: float, bar_close: float, bar_low: float,
    next_open: float,
    alert_price: float, entry_price: float, sl: float, tp: float,
    already_armed: bool,
) -> Tuple[str, Optional[float]]:
    """Decide what to do at the close of a potential trigger bar.

    Returns one of:
      ("enter", next_open)           — transition to OPEN at next_open
      ("skip",  "gap_above_tp")      — trigger valid but next_open > tp
      ("skip",  "gap_below_sl")      — trigger valid but next_open < sl
      ("no_trigger", None)           — conditions not met
    """
    # Trigger requires close strictly > entry_price
    if bar_close <= entry_price:
        return ("no_trigger", None)

    if already_armed:
        trigger_valid = True
    else:
        # Same-bar whip: need dip (low <= alert_price) AND green candle
        whip_dip = bar_low <= alert_price
        green = bar_close > bar_open
        trigger_valid = whip_dip and green

    if not trigger_valid:
        return ("no_trigger", None)

    if next_open > tp:
        return ("skip", "gap_above_tp")
    if next_open < sl:
        return ("skip", "gap_below_sl")
    return ("enter", next_open)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gamma_blast.py::TestEvaluateEntryTrigger -v`
Expected: 9 PASSED.

- [ ] **Step 5: Commit**

```bash
git add engine/gamma_blast_backtest.py tests/test_gamma_blast.py
git commit -m "feat(gamma-blast): add evaluate_entry_trigger with whip + gap protection"
```

---

## Task 5: Exit logic (SL / TP / EOD with tie-break)

**Files:**
- Modify: `engine/gamma_blast_backtest.py`
- Modify: `tests/test_gamma_blast.py`

- [ ] **Step 1: Add failing tests for `evaluate_exit`**

Append to `tests/test_gamma_blast.py`:

```python
from engine.gamma_blast_backtest import evaluate_exit


class TestEvaluateExit:
    """Given a bar's HLC + is_force_exit_bar, decide exit action.

    Returns: (exit_price, exit_reason) | None
    """

    def test_sl_hit(self):
        # low <= sl
        result = evaluate_exit(bar_high=55.0, bar_low=14.0, bar_close=30.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result == (15.0, "SL")

    def test_tp_hit(self):
        # high >= tp
        result = evaluate_exit(bar_high=82.0, bar_low=60.0, bar_close=75.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result == (80.0, "TP")

    def test_sl_wins_tie(self):
        # low <= sl AND high >= tp same bar → SL takes precedence
        result = evaluate_exit(bar_high=85.0, bar_low=12.0, bar_close=40.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result == (15.0, "SL")

    def test_no_exit_mid_range(self):
        result = evaluate_exit(bar_high=60.0, bar_low=35.0, bar_close=50.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result is None

    def test_boundary_exact_sl(self):
        # low == sl counts as SL hit (<=)
        result = evaluate_exit(bar_high=60.0, bar_low=15.0, bar_close=30.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result == (15.0, "SL")

    def test_boundary_exact_tp(self):
        # high == tp counts as TP hit (>=)
        result = evaluate_exit(bar_high=80.0, bar_low=30.0, bar_close=70.0,
                               sl=15, tp=80, is_force_exit_bar=False)
        assert result == (80.0, "TP")

    def test_force_exit_at_close(self):
        result = evaluate_exit(bar_high=60.0, bar_low=30.0, bar_close=50.0,
                               sl=15, tp=80, is_force_exit_bar=True)
        assert result == (50.0, "EOD")

    def test_sl_wins_over_eod(self):
        # Even if this is the force-exit bar, SL still wins if touched
        result = evaluate_exit(bar_high=60.0, bar_low=10.0, bar_close=50.0,
                               sl=15, tp=80, is_force_exit_bar=True)
        assert result == (15.0, "SL")

    def test_tp_wins_over_eod(self):
        result = evaluate_exit(bar_high=90.0, bar_low=40.0, bar_close=70.0,
                               sl=15, tp=80, is_force_exit_bar=True)
        assert result == (80.0, "TP")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gamma_blast.py::TestEvaluateExit -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `evaluate_exit`**

Append to `engine/gamma_blast_backtest.py`:

```python
def evaluate_exit(
    bar_high: float, bar_low: float, bar_close: float,
    sl: float, tp: float, is_force_exit_bar: bool,
) -> Optional[Tuple[float, str]]:
    """Return (exit_price, exit_reason) if this bar exits the position, else None.

    Priority: SL > TP > EOD. SL/TP fills assume exact-level fills (the
    realism approximation is acknowledged in the spec).
    """
    sl_hit = bar_low <= sl
    tp_hit = bar_high >= tp
    if sl_hit:
        return (float(sl), "SL")
    if tp_hit:
        return (float(tp), "TP")
    if is_force_exit_bar:
        return (float(bar_close), "EOD")
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gamma_blast.py::TestEvaluateExit -v`
Expected: 9 PASSED.

- [ ] **Step 5: Commit**

```bash
git add engine/gamma_blast_backtest.py tests/test_gamma_blast.py
git commit -m "feat(gamma-blast): add evaluate_exit with SL/TP/EOD tie-break"
```

---

## Task 6: Per-day state-machine runner (single option_type)

**Files:**
- Modify: `engine/gamma_blast_backtest.py`
- Modify: `tests/test_gamma_blast.py`

- [ ] **Step 1: Add failing tests for `run_machine_for_day`**

This function takes a per-option-type DataFrame for one trading day and returns a list of `GammaBlastTrade`s. The DataFrame has one row per minute with these columns (after caller pre-filters): `datetime, strike, moneyness, open, high, low, close, spot`. The caller guarantees:
- rows are sorted ascending by `datetime`
- one row per minute
- only one `option_type` (caller passes either CE slice or PE slice)
- `expiry_code == 1` only

Append to `tests/test_gamma_blast.py`:

```python
from datetime import date, datetime, timedelta

from engine.gamma_blast_backtest import run_machine_for_day


def _make_day_df(rows, day=date(2026, 2, 26)):
    """Build a minute-level DataFrame from a list of (HH, MM, strike, moneyness,
    open, high, low, close, spot) tuples."""
    recs = []
    for hh, mm, strike, mon, o, h, lo, c, sp in rows:
        recs.append({
            "datetime": datetime(day.year, day.month, day.day, hh, mm),
            "strike": strike,
            "moneyness": mon,
            "open": o, "high": h, "low": lo, "close": c,
            "spot": sp,
        })
    return pd.DataFrame(recs)


DEFAULT_PARAMS = dict(alert_price=20, entry_price=40, sl=15, tp=80)
DEFAULT_TIMING = dict(
    arm_start=time(10, 0),
    arm_deadline=time(15, 0),
    entry_deadline=time(15, 5),
    force_exit=time(15, 15),
)


class TestRunMachineForDay:
    def test_winning_tp_trade(self):
        # 11:00 ATM close 18 → arm on strike 81000
        # 12:30 close 45 (> 40) → trigger, enter at 12:31 open = 47
        # 13:10 bar high 82 → exit at tp=80
        day = date(2026, 2, 26)
        rows = [
            (10, 30, 81000, "ATM", 25, 30, 24, 28, 80950),
            (11, 0,  81000, "ATM", 22, 22, 17, 18, 80910),  # arm
            (12, 30, 81000, "ATM", 40, 46, 38, 45, 81200),  # trigger
            (12, 31, 81000, "ATM", 47, 58, 46, 55, 81215),  # enter @ 47, no exit (high 58 < 80)
            (13, 10, 81000, "ATM", 70, 82, 68, 75, 81390),  # TP hit at 80
            (15, 15, 81000, "ATM", 90, 90, 88, 89, 81400),  # would be force-exit but already closed
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "TP"
        assert t.entry_price == 47
        assert t.exit_price == 80
        assert t.pnl_points == 33
        assert t.pnl_inr == 33 * 20 * 1
        assert t.strike == 81000
        assert t.option_type == "CE"

    def test_losing_sl_trade(self):
        day = date(2026, 2, 26)
        rows = [
            (10, 45, 81000, "ATM", 22, 22, 18, 19, 81000),   # arm
            (12, 0,  81000, "ATM", 40, 46, 39, 43, 80950),   # trigger
            (12, 1,  81000, "ATM", 42, 45, 14, 20, 80900),   # enter @ 42, SL hit (low 14 <= 15)
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "SL"
        assert trades[0].exit_price == 15
        assert trades[0].pnl_points == -27  # 15 - 42

    def test_force_exit_eod(self):
        day = date(2026, 2, 26)
        rows = [
            (10, 30, 81000, "ATM", 22, 22, 18, 19, 81000),   # arm
            (11, 0,  81000, "ATM", 40, 46, 38, 45, 81050),   # trigger
            (11, 1,  81000, "ATM", 46, 60, 45, 50, 81060),   # enter @ 46
            # Premium drifts sideways between 30 and 70 the rest of the day (never SL/TP)
            (12, 0,  81000, "ATM", 50, 55, 45, 52, 81060),
            (15, 15, 81000, "ATM", 35, 40, 30, 38, 81050),   # force exit at close=38
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        assert trades[0].exit_reason == "EOD"
        assert trades[0].exit_price == 38

    def test_armed_but_deadline_expires(self):
        day = date(2026, 2, 26)
        rows = [
            (14, 30, 81000, "ATM", 22, 22, 18, 19, 81000),   # arm
            (14, 59, 81000, "ATM", 30, 35, 28, 32, 81020),   # close <= entry_price
            (15, 5,  81000, "ATM", 35, 38, 33, 36, 81010),   # past entry_deadline tail
            (15, 15, 81000, "ATM", 40, 42, 38, 41, 81005),   # no position, no force exit
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert trades == []

    def test_reentry_after_sl(self):
        day = date(2026, 2, 26)
        rows = [
            (10, 30, 81000, "ATM", 22, 22, 18, 19, 81000),   # arm
            (11, 0,  81000, "ATM", 40, 46, 38, 45, 81050),   # trigger
            (11, 1,  81000, "ATM", 46, 50, 14, 20, 81060),   # SL at 15
            # After exit, current ATM at 11:01 is still 81000 with close=20 — not < 20 → no instant re-arm
            (11, 30, 81000, "ATM", 19, 19, 15, 17, 81040),   # re-arm (close 17 < 20)
            (12, 0,  81000, "ATM", 38, 45, 35, 43, 81050),   # re-trigger
            (12, 1,  81000, "ATM", 44, 85, 42, 80, 81080),   # enter @ 44, TP hit at 80
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 2
        assert trades[0].exit_reason == "SL"
        assert trades[1].exit_reason == "TP"

    def test_strike_locked_after_arm(self):
        # 81000 is ATM at 11:00 with close=18 → arm locks 81000.
        # At 11:30 ATM shifts to 81100 (spot moved). 81000 is now ITM/OTM.
        # But we keep tracking 81000, not 81100.
        day = date(2026, 2, 26)
        rows = [
            # 11:00 ATM=81000, close 18
            (11, 0,  81000, "ATM", 22, 22, 17, 18, 81000),
            (11, 0,  81100, "OTM", 15, 18, 13, 14, 81000),
            # 11:30 spot moved, now 81100 is ATM
            (11, 30, 81000, "ITM", 35, 40, 32, 38, 81120),
            (11, 30, 81100, "ATM", 20, 25, 18, 22, 81120),
            # 12:00 on 81000 close=45 → trigger on LOCKED strike
            (12, 0,  81000, "ITM", 40, 46, 38, 45, 81150),
            (12, 0,  81100, "ATM", 24, 28, 22, 26, 81150),
            # Entry on 81000 at 12:01 open = 47
            (12, 1,  81000, "ITM", 47, 85, 45, 82, 81180),
            (12, 1,  81100, "ATM", 27, 32, 25, 30, 81180),
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        assert trades[0].strike == 81000  # locked, not 81100
        assert trades[0].entry_price == 47

    def test_arm_ignored_before_arm_start(self):
        day = date(2026, 2, 26)
        rows = [
            (9, 30, 81000, "ATM", 22, 22, 18, 19, 81000),   # before 10:00 — no arm
            (11, 0, 81000, "ATM", 25, 30, 24, 28, 81020),   # no arm (close >= alert)
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert trades == []

    def test_same_bar_green_whip_enters(self):
        # One bar at 11:00 has low=12 (dip) and close=45 (> entry) AND green.
        # Next bar at 11:01 opens at 50, no SL/TP; later TP at 12:00.
        day = date(2026, 2, 26)
        rows = [
            (11, 0,  81000, "ATM", 30, 48, 12, 45, 81000),   # green whip
            (11, 1,  81000, "ATM", 50, 58, 48, 55, 81020),   # entry @ 50, no exit
            (12, 0,  81000, "ATM", 60, 85, 55, 78, 81080),   # TP hit at 80
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        assert trades[0].entry_price == 50
        assert trades[0].exit_price == 80
        assert trades[0].exit_reason == "TP"
        assert trades[0].arm_time == "11:00"
        assert trades[0].entry_time == "11:01"

    def test_same_bar_red_whip_skipped(self):
        # Red bar with low=12 and close=45 but close < open → no arm, no entry.
        # Follow-up bar at 11:01 has a normal arm (close=18), ensuring the red whip
        # didn't poison later behavior.
        day = date(2026, 2, 26)
        rows = [
            (11, 0,  81000, "ATM", 50, 55, 12, 45, 81000),   # red whip → skipped
            (11, 10, 81000, "ATM", 20, 22, 17, 18, 81000),   # plain arm
            (12, 0,  81000, "ATM", 40, 46, 38, 45, 81060),   # trigger
            (12, 1,  81000, "ATM", 47, 82, 45, 78, 81080),   # enter @ 47, TP at 80
        ]
        df = _make_day_df(rows, day=day)
        trades = run_machine_for_day(
            df, instrument="SENSEX", option_type="CE", day=day,
            expiry_date=day, lot_size=20, lot_multiplier=1,
            params=DEFAULT_PARAMS, timing=DEFAULT_TIMING,
        )
        assert len(trades) == 1
        assert trades[0].arm_time == "11:10"  # NOT 11:00
        assert trades[0].exit_reason == "TP"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gamma_blast.py::TestRunMachineForDay -v`
Expected: FAIL with `ImportError: cannot import name 'run_machine_for_day'`.

- [ ] **Step 3: Implement `run_machine_for_day`**

Append to `engine/gamma_blast_backtest.py`:

```python
from datetime import date as _date, datetime as _datetime


def _format_hhmm(dt) -> str:
    return pd.Timestamp(dt).strftime("%H:%M")


def run_machine_for_day(
    df: pd.DataFrame,
    *,
    instrument: str,
    option_type: str,
    day: _date,
    expiry_date: _date,
    lot_size: int,
    lot_multiplier: int,
    params: dict,
    timing: dict,
) -> List[GammaBlastTrade]:
    """Run the state machine for a single day, one option_type.

    Expects df to be pre-filtered to (expiry_code == 1, option_type == given).
    Minutely bars sorted ascending by datetime. Returns list of completed trades.
    """
    trades: List[GammaBlastTrade] = []
    if df.empty:
        return trades

    # Build a per-minute index for the "current ATM" lookup.
    atm_by_minute = (
        df[df["moneyness"] == "ATM"]
        .set_index("datetime")
        [["strike", "open", "high", "low", "close", "spot"]]
    )
    # Build a per-strike per-minute index for the locked-strike lookup.
    by_strike_minute = df.set_index(["strike", "datetime"])

    alert_price = float(params["alert_price"])
    entry_price = float(params["entry_price"])
    sl = float(params["sl"])
    tp = float(params["tp"])
    arm_start = timing["arm_start"]
    arm_deadline = timing["arm_deadline"]
    entry_deadline = timing["entry_deadline"]
    force_exit = timing["force_exit"]

    # Ordered list of minutes that have at least one row in this day
    minutes = sorted(set(df["datetime"].tolist()))

    # State: None=IDLE, dict=ARMED/OPEN
    state = None  # {"status": "ARMED"|"OPEN", "strike": int, "arm_*": ..., "entry_*": ...}

    def atm_row_at(minute):
        try:
            return atm_by_minute.loc[minute]
        except KeyError:
            return None

    def locked_row_at(strike, minute):
        try:
            return by_strike_minute.loc[(strike, minute)]
        except KeyError:
            return None

    for idx, minute in enumerate(minutes):
        minute_time = minute.time()
        next_minute = minutes[idx + 1] if idx + 1 < len(minutes) else None

        # ---- Handle OPEN position: check exits this minute on locked strike ----
        if state is not None and state["status"] == "OPEN":
            row = locked_row_at(state["strike"], minute)
            if row is not None:
                is_force = minute_time >= force_exit
                result = evaluate_exit(
                    bar_high=float(row["high"]),
                    bar_low=float(row["low"]),
                    bar_close=float(row["close"]),
                    sl=sl, tp=tp,
                    is_force_exit_bar=is_force,
                )
                if result is not None:
                    exit_price, reason = result
                    pnl_points = exit_price - state["entry_price"]
                    trade = GammaBlastTrade(
                        date=str(day),
                        instrument=instrument,
                        expiry_date=str(expiry_date),
                        option_type=option_type,
                        strike=int(state["strike"]),
                        spot_at_arm=state["spot_at_arm"],
                        arm_time=state["arm_time"],
                        arm_premium=state["arm_premium"],
                        spot_at_entry=state["spot_at_entry"],
                        entry_time=state["entry_time"],
                        entry_price=state["entry_price"],
                        entry_trigger_close=state["entry_trigger_close"],
                        spot_at_exit=float(row["spot"]),
                        exit_time=_format_hhmm(minute),
                        exit_price=float(exit_price),
                        exit_reason=reason,
                        pnl_points=float(pnl_points),
                        pnl_inr=float(pnl_points) * lot_size * lot_multiplier,
                        lot_size=lot_size * lot_multiplier,
                    )
                    trades.append(trade)
                    state = None  # fall through to same-minute re-arm below

        # ---- Handle ARMED: check entry trigger on locked strike ----
        if state is not None and state["status"] == "ARMED":
            row = locked_row_at(state["strike"], minute)
            if row is not None and next_minute is not None:
                # entry can only fire if the next bar's OPEN time <= entry_deadline
                if next_minute.time() <= entry_deadline:
                    next_row = locked_row_at(state["strike"], next_minute)
                    if next_row is not None:
                        result = evaluate_entry_trigger(
                            bar_open=float(row["open"]),
                            bar_close=float(row["close"]),
                            bar_low=float(row["low"]),
                            next_open=float(next_row["open"]),
                            alert_price=alert_price, entry_price=entry_price,
                            sl=sl, tp=tp,
                            already_armed=True,
                        )
                        action, payload = result
                        if action == "enter":
                            state = {
                                "status": "OPEN",
                                "strike": state["strike"],
                                "spot_at_arm": state["spot_at_arm"],
                                "arm_time": state["arm_time"],
                                "arm_premium": state["arm_premium"],
                                "spot_at_entry": float(next_row["spot"]),
                                "entry_time": _format_hhmm(next_minute),
                                "entry_price": float(payload),
                                "entry_trigger_close": float(row["close"]),
                            }
                            continue
                        elif action == "skip":
                            state = None  # fall through to rearm check
            # If we're still ARMED but the trigger deadline has passed, release.
            if state is not None and state["status"] == "ARMED" and minute_time > entry_deadline:
                state = None

        # ---- IDLE: check same-bar whip first, then plain arm ----
        if state is None:
            atm = atm_row_at(minute)
            in_arm_window = arm_start <= minute_time <= arm_deadline
            if atm is not None and in_arm_window:
                a_open = float(atm["open"])
                a_close = float(atm["close"])
                a_low = float(atm["low"])

                # Same-bar whip candidate: low dipped AND close recovered past entry_price
                whip_candidate = (a_low <= alert_price) and (a_close > entry_price)
                green = a_close > a_open
                red_or_doji = a_close <= a_open

                if whip_candidate and green:
                    # Try to enter at next bar open via the unarmed whip path
                    if next_minute is not None and next_minute.time() <= entry_deadline:
                        next_row = locked_row_at(int(atm["strike"]), next_minute)
                        if next_row is not None:
                            result = evaluate_entry_trigger(
                                bar_open=a_open, bar_close=a_close, bar_low=a_low,
                                next_open=float(next_row["open"]),
                                alert_price=alert_price, entry_price=entry_price,
                                sl=sl, tp=tp,
                                already_armed=False,
                            )
                            action, payload = result
                            if action == "enter":
                                state = {
                                    "status": "OPEN",
                                    "strike": int(atm["strike"]),
                                    "spot_at_arm": float(atm["spot"]),
                                    "arm_time": _format_hhmm(minute),
                                    "arm_premium": a_close,
                                    "spot_at_entry": float(next_row["spot"]),
                                    "entry_time": _format_hhmm(next_minute),
                                    "entry_price": float(payload),
                                    "entry_trigger_close": a_close,
                                }
                                continue
                            # "skip" (gap) → no arm either; stay IDLE for this minute
                elif whip_candidate and red_or_doji:
                    # Red whip: skip entirely, do NOT arm
                    pass
                elif should_arm(
                    atm_close=a_close, alert_price=alert_price,
                    bar_time=minute_time, arm_start=arm_start, arm_deadline=arm_deadline,
                ):
                    # Plain arm (strict <)
                    state = {
                        "status": "ARMED",
                        "strike": int(atm["strike"]),
                        "spot_at_arm": float(atm["spot"]),
                        "arm_time": _format_hhmm(minute),
                        "arm_premium": a_close,
                    }

    # End-of-day safety: force exit any still-OPEN position at the last available bar.
    # Covers the rare case where the locked strike has no row at exactly the force_exit
    # minute but has bars earlier in the day.
    if state is not None and state["status"] == "OPEN":
        last_minute = minutes[-1]
        last_row = locked_row_at(state["strike"], last_minute)
        if last_row is not None:
            exit_price = float(last_row["close"])
            pnl_points = exit_price - state["entry_price"]
            trade = GammaBlastTrade(
                date=str(day),
                instrument=instrument,
                expiry_date=str(expiry_date),
                option_type=option_type,
                strike=int(state["strike"]),
                spot_at_arm=state["spot_at_arm"],
                arm_time=state["arm_time"],
                arm_premium=state["arm_premium"],
                spot_at_entry=state["spot_at_entry"],
                entry_time=state["entry_time"],
                entry_price=state["entry_price"],
                entry_trigger_close=state["entry_trigger_close"],
                spot_at_exit=float(last_row["spot"]),
                exit_time=_format_hhmm(last_minute),
                exit_price=exit_price,
                exit_reason="EOD",
                pnl_points=float(pnl_points),
                pnl_inr=float(pnl_points) * lot_size * lot_multiplier,
                lot_size=lot_size * lot_multiplier,
            )
            trades.append(trade)

    return trades
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gamma_blast.py::TestRunMachineForDay -v`
Expected: 9 PASSED.

- [ ] **Step 5: Commit**

```bash
git add engine/gamma_blast_backtest.py tests/test_gamma_blast.py
git commit -m "feat(gamma-blast): add run_machine_for_day with arm/entry/exit loop"
```

---

## Task 7: Backtest driver (iterate expiry days, both instruments, both option types)

**Files:**
- Modify: `engine/gamma_blast_backtest.py`
- Modify: `tests/test_gamma_blast.py`

- [ ] **Step 1: Add failing tests for `run_backtest`**

Append to `tests/test_gamma_blast.py`:

```python
from engine.gamma_blast_backtest import run_backtest


def _stub_loader(df_by_instrument_and_day):
    """Returns a callable mimicking a parquet loader.

    df_by_instrument_and_day: {(instrument, day): DataFrame}
    """
    def _load(instrument, day):
        return df_by_instrument_and_day.get((instrument, day), pd.DataFrame())
    return _load


class TestRunBacktest:
    def test_only_expiry_days_processed(self):
        # Feb 26, 2026 is a SENSEX Thursday expiry; Feb 27 is not.
        feb26 = date(2026, 2, 26)
        feb27 = date(2026, 2, 27)

        rows = [
            (10, 30, 81000, "ATM", 22, 22, 18, 19, 81000),
            (11, 0,  81000, "ATM", 40, 46, 38, 45, 81050),
            (11, 1,  81000, "ATM", 47, 82, 45, 78, 81060),  # TP hit at 80
        ]

        df_feb26_ce = _make_day_df(rows, day=feb26).assign(option_type="CE", expiry_code=1)
        df_feb27_ce = _make_day_df(rows, day=feb27).assign(option_type="CE", expiry_code=1)

        loader = _stub_loader({
            ("SENSEX", feb26): df_feb26_ce,
            ("SENSEX", feb27): df_feb27_ce,  # should never be called
        })

        config = {
            "instruments": ["SENSEX"],
            "params": {"SENSEX": DEFAULT_PARAMS},
            "timing": {k: v.strftime("%H:%M") for k, v in DEFAULT_TIMING.items()},
            "lot_size": 1,
            "backtest_start": "2026-02-26",
            "backtest_end":   "2026-02-27",
        }
        trades = run_backtest(config, loader=loader)
        assert len(trades) == 1
        assert trades[0].date == "2026-02-26"

    def test_instruments_filter_respected(self):
        feb26 = date(2026, 2, 26)
        rows = [
            (10, 30, 81000, "ATM", 22, 22, 18, 19, 81000),
            (11, 0,  81000, "ATM", 40, 46, 38, 45, 81050),
            (11, 1,  81000, "ATM", 47, 82, 45, 78, 81060),
        ]
        df_ce = _make_day_df(rows, day=feb26).assign(option_type="CE", expiry_code=1)
        loader = _stub_loader({("SENSEX", feb26): df_ce})

        # NIFTY has null params → must be skipped even if listed
        config = {
            "instruments": ["NIFTY", "SENSEX"],
            "params": {
                "NIFTY": {"alert_price": None, "entry_price": None, "sl": None, "tp": None},
                "SENSEX": DEFAULT_PARAMS,
            },
            "timing": {k: v.strftime("%H:%M") for k, v in DEFAULT_TIMING.items()},
            "lot_size": 1,
            "backtest_start": "2026-02-26",
            "backtest_end":   "2026-02-26",
        }
        trades = run_backtest(config, loader=loader)
        assert all(t.instrument == "SENSEX" for t in trades)

    def test_ce_and_pe_both_run(self):
        feb26 = date(2026, 2, 26)
        ce_rows = [
            (10, 30, 81000, "ATM", 22, 22, 18, 19, 81000),
            (11, 0,  81000, "ATM", 40, 46, 38, 45, 81050),
            (11, 1,  81000, "ATM", 47, 82, 45, 78, 81060),
        ]
        pe_rows = [
            (10, 45, 81000, "ATM", 22, 22, 18, 19, 81000),
            (11, 30, 81000, "ATM", 40, 46, 38, 45, 80950),
            (11, 31, 81000, "ATM", 46, 50, 14, 20, 80930),  # SL
        ]
        df_ce = _make_day_df(ce_rows, day=feb26).assign(option_type="CE", expiry_code=1)
        df_pe = _make_day_df(pe_rows, day=feb26).assign(option_type="PE", expiry_code=1)
        df_day = pd.concat([df_ce, df_pe], ignore_index=True)
        loader = _stub_loader({("SENSEX", feb26): df_day})

        config = {
            "instruments": ["SENSEX"],
            "params": {"SENSEX": DEFAULT_PARAMS},
            "timing": {k: v.strftime("%H:%M") for k, v in DEFAULT_TIMING.items()},
            "lot_size": 1,
            "backtest_start": "2026-02-26",
            "backtest_end":   "2026-02-26",
        }
        trades = run_backtest(config, loader=loader)
        assert len(trades) == 2
        types = sorted([t.option_type for t in trades])
        assert types == ["CE", "PE"]
        reasons = sorted([t.exit_reason for t in trades])
        assert reasons == ["SL", "TP"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gamma_blast.py::TestRunBacktest -v`
Expected: FAIL with `ImportError: cannot import name 'run_backtest'`.

- [ ] **Step 3: Implement `run_backtest`**

Append to `engine/gamma_blast_backtest.py`:

```python
from typing import Callable

from config import (
    DATA_PATH,
    LOT_SIZE,
    NIFTY_WEEKLY_EXPIRY_DATES,
    SENSEX_WEEKLY_EXPIRY_DATES,
)


def _parse_hhmm(s: str) -> _time:
    hh, mm = s.split(":")
    return _time(int(hh), int(mm))


def _expiry_list_for(instrument: str) -> List[_date]:
    key = instrument.upper()
    if key == "NIFTY":
        return list(NIFTY_WEEKLY_EXPIRY_DATES)
    if key == "SENSEX":
        return list(SENSEX_WEEKLY_EXPIRY_DATES)
    raise ValueError(f"Unsupported instrument: {instrument}")


def _params_are_valid(p: dict) -> bool:
    required = ("alert_price", "entry_price", "sl", "tp")
    return all(p.get(k) is not None for k in required)


def _default_loader(instrument: str, day: _date) -> pd.DataFrame:
    """Load one day's option rows for an instrument from the parquet file.

    Returns a DataFrame filtered to that date and expiry_code=1. Uses
    engine.data_loader under the hood if available; otherwise falls back to
    pandas.read_parquet with filters.
    """
    path = DATA_PATH[instrument.upper()]
    # Pre-filter by date using pyarrow filter if possible
    start = pd.Timestamp(day).tz_localize("Asia/Kolkata")
    end = start + pd.Timedelta(days=1)
    df = pd.read_parquet(
        path,
        filters=[
            ("datetime", ">=", start),
            ("datetime", "<", end),
            ("expiry_code", "==", 1),
        ],
    )
    return df


def run_backtest(config: dict, loader: Callable[[str, _date], pd.DataFrame] = None) -> List[GammaBlastTrade]:
    """Run the Gamma Blast backtest across expiry days.

    Args:
        config: parsed JSON strategy config.
        loader: optional override for data loading (for tests). Defaults to parquet.
    """
    if loader is None:
        loader = _default_loader

    timing = {
        "arm_start": _parse_hhmm(config["timing"]["arm_start"]),
        "arm_deadline": _parse_hhmm(config["timing"]["arm_deadline"]),
        "entry_deadline": _parse_hhmm(config["timing"]["entry_deadline"]),
        "force_exit": _parse_hhmm(config["timing"]["force_exit"]),
    }

    start = pd.Timestamp(config["backtest_start"]).date()
    end = pd.Timestamp(config["backtest_end"]).date()
    lot_multiplier = int(config.get("lot_size", 1))

    all_trades: List[GammaBlastTrade] = []
    for instrument in config["instruments"]:
        params = config["params"].get(instrument, {})
        if not _params_are_valid(params):
            logger.info("Skipping %s: params incomplete", instrument)
            continue
        expiries = [d for d in _expiry_list_for(instrument) if start <= d <= end]
        lot_size = LOT_SIZE[instrument.upper()]

        for day in expiries:
            day_df = loader(instrument, day)
            if day_df.empty:
                logger.debug("No data for %s %s", instrument, day)
                continue

            for option_type in ("CE", "PE"):
                ot_df = day_df[
                    (day_df["option_type"] == option_type) &
                    (day_df["expiry_code"] == 1)
                ].copy()
                if ot_df.empty:
                    continue
                ot_df = ot_df.sort_values("datetime").reset_index(drop=True)

                trades = run_machine_for_day(
                    ot_df,
                    instrument=instrument,
                    option_type=option_type,
                    day=day,
                    expiry_date=day,  # this trade's expiry IS today for weekly expiry-day play
                    lot_size=lot_size,
                    lot_multiplier=lot_multiplier,
                    params=params,
                    timing=timing,
                )
                all_trades.extend(trades)

    return all_trades
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gamma_blast.py::TestRunBacktest -v`
Expected: 3 PASSED.

- [ ] **Step 5: Run full suite**

Run: `pytest tests/test_gamma_blast.py -v`
Expected: all tests so far PASS.

- [ ] **Step 6: Commit**

```bash
git add engine/gamma_blast_backtest.py tests/test_gamma_blast.py
git commit -m "feat(gamma-blast): add run_backtest driver iterating expiry days"
```

---

## Task 8: Summary stats + CSV writer

**Files:**
- Modify: `engine/gamma_blast_backtest.py`
- Modify: `tests/test_gamma_blast.py`

- [ ] **Step 1: Add failing tests for `summarize_trades` and `write_trades_csv`**

Append to `tests/test_gamma_blast.py`:

```python
import os
import tempfile

from engine.gamma_blast_backtest import summarize_trades, write_trades_csv


class TestSummarize:
    def test_empty(self):
        s = summarize_trades([])
        assert s["total_trades"] == 0
        assert s["total_pnl_inr"] == 0

    def test_mixed_trades(self):
        trades = [
            make_trade(exit_reason="TP", pnl_points=33, pnl_inr=660),
            make_trade(exit_reason="SL", pnl_points=-27, pnl_inr=-540),
            make_trade(exit_reason="TP", pnl_points=40, pnl_inr=800, option_type="PE"),
        ]
        s = summarize_trades(trades)
        assert s["total_trades"] == 3
        assert s["wins"] == 2
        assert s["losses"] == 1
        assert round(s["win_rate"] * 100, 2) == 66.67
        assert s["total_pnl_points"] == 46
        assert s["total_pnl_inr"] == 920

    def test_per_instrument_breakdown(self):
        trades = [
            make_trade(instrument="SENSEX", pnl_inr=100),
            make_trade(instrument="SENSEX", pnl_inr=-200),
            make_trade(instrument="NIFTY", pnl_inr=500),
        ]
        s = summarize_trades(trades)
        assert s["by_instrument"]["SENSEX"]["trades"] == 2
        assert s["by_instrument"]["SENSEX"]["pnl_inr"] == -100
        assert s["by_instrument"]["NIFTY"]["trades"] == 1
        assert s["by_instrument"]["NIFTY"]["pnl_inr"] == 500


class TestWriteCsv:
    def test_write_roundtrip(self):
        trades = [make_trade(), make_trade(option_type="PE")]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trades.csv")
            write_trades_csv(trades, path)
            df = pd.read_csv(path)
            assert len(df) == 2
            assert list(df["option_type"]) == ["CE", "PE"]

    def test_write_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trades.csv")
            write_trades_csv([], path)
            # File created but header-only
            df = pd.read_csv(path)
            assert df.empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gamma_blast.py::TestSummarize tests/test_gamma_blast.py::TestWriteCsv -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement summarizer and CSV writer**

Append to `engine/gamma_blast_backtest.py`:

```python
from collections import defaultdict


def summarize_trades(trades: List[GammaBlastTrade]) -> dict:
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "total_pnl_points": 0.0, "total_pnl_inr": 0.0,
            "by_instrument": {},
        }

    wins = sum(1 for t in trades if t.pnl_points > 0)
    losses = sum(1 for t in trades if t.pnl_points <= 0)
    total_points = sum(t.pnl_points for t in trades)
    total_inr = sum(t.pnl_inr for t in trades)

    by_inst = defaultdict(lambda: {"trades": 0, "pnl_inr": 0.0, "pnl_points": 0.0})
    for t in trades:
        by_inst[t.instrument]["trades"] += 1
        by_inst[t.instrument]["pnl_inr"] += t.pnl_inr
        by_inst[t.instrument]["pnl_points"] += t.pnl_points

    return {
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades),
        "total_pnl_points": total_points,
        "total_pnl_inr": total_inr,
        "by_instrument": dict(by_inst),
    }


def write_trades_csv(trades: List[GammaBlastTrade], path: str) -> None:
    """Write trades to a CSV at `path`. Creates header-only file if no trades."""
    if trades:
        df = trades_to_dataframe(trades)
    else:
        # Header-only (use field names from dataclass)
        from dataclasses import fields
        df = pd.DataFrame(columns=[f.name for f in fields(GammaBlastTrade)])
    df.to_csv(path, index=False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gamma_blast.py::TestSummarize tests/test_gamma_blast.py::TestWriteCsv -v`
Expected: 5 PASSED.

- [ ] **Step 5: Commit**

```bash
git add engine/gamma_blast_backtest.py tests/test_gamma_blast.py
git commit -m "feat(gamma-blast): add summarize_trades + write_trades_csv"
```

---

## Task 9: Integration test with real SENSEX parquet data

**Files:**
- Modify: `tests/test_gamma_blast.py`

- [ ] **Step 1: Add integration test**

Append to `tests/test_gamma_blast.py`:

```python
import json

from engine.gamma_blast_backtest import _default_loader


@pytest.mark.integration
class TestIntegrationRealData:
    """Smoke test against real SENSEX parquet data. Skipped if data missing."""

    def test_one_sensex_expiry_day_runs(self):
        # Pick a known SENSEX Thursday expiry in 2026
        day = date(2026, 2, 26)
        try:
            df = _default_loader("SENSEX", day)
        except Exception as e:
            pytest.skip(f"SENSEX data not available: {e}")
        if df.empty:
            pytest.skip("No data for 2026-02-26")

        config = {
            "instruments": ["SENSEX"],
            "params": {"SENSEX": {"alert_price": 20, "entry_price": 40, "sl": 15, "tp": 80}},
            "timing": {"arm_start": "10:00", "arm_deadline": "15:00",
                       "entry_deadline": "15:05", "force_exit": "15:15"},
            "lot_size": 1,
            "backtest_start": "2026-02-26",
            "backtest_end":   "2026-02-26",
        }
        trades = run_backtest(config)
        # We only assert it runs without error and returns a list.
        # Number of trades is data-dependent, so no hard numeric assertion.
        assert isinstance(trades, list)
        for t in trades:
            assert t.instrument == "SENSEX"
            assert t.option_type in ("CE", "PE")
            assert t.date == "2026-02-26"
            assert t.exit_reason in ("SL", "TP", "EOD")
            assert t.entry_price > 0
            assert t.exit_price > 0
```

- [ ] **Step 2: Run integration test**

Run: `pytest tests/test_gamma_blast.py::TestIntegrationRealData -v`
Expected: PASS (or SKIP if data missing, but in this repo SENSEX data runs through 2026-03-09 so Feb 26 should be present).

- [ ] **Step 3: Commit**

```bash
git add tests/test_gamma_blast.py
git commit -m "test(gamma-blast): integration test on real SENSEX Feb 26 expiry"
```

---

## Task 10: Streamlit UI integration

**Files:**
- Create: `ui/gamma_blast_backtest_runner.py`
- Modify: `app.py`

- [ ] **Step 1: Create the Streamlit runner**

Create `ui/gamma_blast_backtest_runner.py`:

```python
"""Streamlit form + runner for the Gamma Blast backtest."""
import json
import os
from datetime import date
from io import StringIO

import streamlit as st

from engine.gamma_blast_backtest import (
    run_backtest,
    summarize_trades,
    trades_to_dataframe,
)


DEFAULT_CONFIG_PATH = "saved_strategies/gamma_blast.json"


def _load_default_config() -> dict:
    if os.path.exists(DEFAULT_CONFIG_PATH):
        with open(DEFAULT_CONFIG_PATH) as f:
            return json.load(f)
    return {
        "instruments": ["SENSEX"],
        "params": {
            "NIFTY":  {"alert_price": None, "entry_price": None, "sl": None, "tp": None},
            "SENSEX": {"alert_price": 20,   "entry_price": 40,   "sl": 15,   "tp": 80},
        },
        "timing": {"arm_start": "10:00", "arm_deadline": "15:00",
                   "entry_deadline": "15:05", "force_exit": "15:15"},
        "lot_size": 1,
        "backtest_start": "2025-01-01",
        "backtest_end":   "2026-04-13",
    }


def _param_inputs(instrument: str, defaults: dict, key_prefix: str) -> dict:
    c1, c2, c3, c4 = st.columns(4)
    alert = c1.number_input(
        f"{instrument} alert_price", min_value=0.0,
        value=float(defaults.get("alert_price") or 0.0),
        key=f"{key_prefix}_alert",
    )
    entry = c2.number_input(
        f"{instrument} entry_price", min_value=0.0,
        value=float(defaults.get("entry_price") or 0.0),
        key=f"{key_prefix}_entry",
    )
    sl = c3.number_input(
        f"{instrument} sl", min_value=0.0,
        value=float(defaults.get("sl") or 0.0),
        key=f"{key_prefix}_sl",
    )
    tp = c4.number_input(
        f"{instrument} tp", min_value=0.0,
        value=float(defaults.get("tp") or 0.0),
        key=f"{key_prefix}_tp",
    )
    # A zero value means "unset" → pass None so the engine skips the instrument
    def _none_if_zero(x):
        return None if x == 0 else float(x)
    return {
        "alert_price": _none_if_zero(alert),
        "entry_price": _none_if_zero(entry),
        "sl": _none_if_zero(sl),
        "tp": _none_if_zero(tp),
    }


def render_gamma_blast_backtest() -> None:
    st.header("Gamma Blast — Expiry-Day ATM Reversal")
    st.caption(
        "Buys ATM CE/PE after premium is crushed below alert_price then "
        "recovers above entry_price. Fixed-absolute SL and TP."
    )

    cfg = _load_default_config()

    col_n, col_s = st.columns(2)
    use_nifty = col_n.checkbox("NIFTY", value="NIFTY" in cfg.get("instruments", []))
    use_sensex = col_s.checkbox("SENSEX", value="SENSEX" in cfg.get("instruments", []))

    if use_nifty:
        st.subheader("NIFTY levels")
        nifty_params = _param_inputs("NIFTY", cfg["params"].get("NIFTY", {}), "nifty")
    else:
        nifty_params = {"alert_price": None, "entry_price": None, "sl": None, "tp": None}

    if use_sensex:
        st.subheader("SENSEX levels")
        sensex_params = _param_inputs("SENSEX", cfg["params"].get("SENSEX", {}), "sensex")
    else:
        sensex_params = {"alert_price": None, "entry_price": None, "sl": None, "tp": None}

    col_a, col_b, col_c = st.columns(3)
    start_date = col_a.date_input("Start", value=date.fromisoformat(cfg["backtest_start"]))
    end_date = col_b.date_input("End", value=date.fromisoformat(cfg["backtest_end"]))
    lot_multiplier = col_c.number_input("Lots (multiplier)", min_value=1, value=int(cfg.get("lot_size", 1)))

    if st.button("Run backtest", type="primary"):
        instruments = [i for i, used in [("NIFTY", use_nifty), ("SENSEX", use_sensex)] if used]
        if not instruments:
            st.error("Select at least one instrument.")
            return

        run_config = {
            "instruments": instruments,
            "params": {"NIFTY": nifty_params, "SENSEX": sensex_params},
            "timing": cfg["timing"],
            "lot_size": int(lot_multiplier),
            "backtest_start": start_date.isoformat(),
            "backtest_end":   end_date.isoformat(),
        }

        with st.spinner("Running…"):
            trades = run_backtest(run_config)

        summary = summarize_trades(trades)
        st.subheader("Summary")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Trades", summary["total_trades"])
        c2.metric("Win rate", f"{summary['win_rate'] * 100:.1f}%")
        c3.metric("Total points", f"{summary['total_pnl_points']:.2f}")
        c4.metric("Total INR", f"{summary['total_pnl_inr']:,.0f}")
        if summary["by_instrument"]:
            st.write("By instrument:", summary["by_instrument"])

        if trades:
            df = trades_to_dataframe(trades)
            st.subheader("Trades")
            st.dataframe(df, use_container_width=True)
            csv_buf = StringIO()
            df.to_csv(csv_buf, index=False)
            st.download_button(
                "Download CSV",
                data=csv_buf.getvalue(),
                file_name=f"gamma_blast_{start_date}_{end_date}.csv",
                mime="text/csv",
            )
        else:
            st.info("No trades generated in this window.")
```

- [ ] **Step 2: Register tab in `app.py`**

Edit `app.py` to add the Gamma Blast tab. Add the import alongside the other strategy imports (near line 26-28):

```python
from ui.gamma_blast_backtest_runner import render_gamma_blast_backtest
```

Add `"💥 Gamma Blast"` to the `st.tabs([...])` list and `tab_gamma_blast` to the tuple on the LHS. Then add at the bottom with the other `with tab_*:` blocks:

```python
with tab_gamma_blast:
    render_gamma_blast_backtest()
```

The exact edits depend on the file's current line numbers — use Edit tool with surrounding context for uniqueness.

- [ ] **Step 3: Smoke-test the UI**

Run: `streamlit run app.py`
Expected: Streamlit app opens. Find the "💥 Gamma Blast" tab, check both instrument checkboxes appear, SENSEX levels are pre-populated (20/40/15/80), date range defaults are within coverage, "Run backtest" executes without errors and renders a trades table + summary.

This is manual verification (the user's CLAUDE.md says "For frontend changes, use `claude --chrome` to visually verify"). Note in the commit message that this is manually tested.

- [ ] **Step 4: Commit**

```bash
git add ui/gamma_blast_backtest_runner.py app.py
git commit -m "feat(gamma-blast): Streamlit UI integration"
```

---

## Task 11: End-to-end smoke check + final sweep

**Files:** none

- [ ] **Step 1: Run the full gamma blast test suite**

Run: `pytest tests/test_gamma_blast.py -v`
Expected: ~30+ PASSED, 0 FAILED.

- [ ] **Step 2: Run the full repo test suite to ensure nothing else broke**

Run: `pytest tests/ -v`
Expected: all pre-existing tests still PASS; gamma blast tests PASS.

- [ ] **Step 3: Manual backtest sanity check via CLI**

Run:

```bash
python3 -c "
import json
from engine.gamma_blast_backtest import run_backtest, summarize_trades, write_trades_csv
with open('saved_strategies/gamma_blast.json') as f:
    cfg = json.load(f)
cfg['backtest_start'] = '2025-09-01'
cfg['backtest_end']   = '2026-03-09'
trades = run_backtest(cfg)
print(summarize_trades(trades))
write_trades_csv(trades, 'gamma_blast_sensex_2025-09-2026-03.csv')
"
```

Expected: runs without error, prints a summary dict, writes the CSV.
Eyeball check: SL/TP exits split should be reasonable (not 100% one side), trades happen only on Thursdays in that range, all strikes ATM-ish relative to SENSEX spot on those days.

- [ ] **Step 4: Final commit of CSV artifact (optional)**

If the user wants to keep the sample CSV in the repo, commit it. Otherwise delete it. By default, DO NOT commit the CSV.

---

## Definition of Done

- [ ] All unit tests pass
- [ ] Integration test passes on real parquet data
- [ ] Streamlit UI renders and runs a backtest end-to-end
- [ ] Spec requirements all implemented (see `docs/superpowers/specs/2026-04-23-gamma-blast-design.md`)
- [ ] No changes to `engine/backtest.py`, `engine/expiry_calendar.py`, or any other pre-existing engine
- [ ] Strategy JSON loads and validates (no exception thrown by `_load_default_config` path)

---

## Notes for the implementer

- **The state machine is the brittle part.** The test suite in Task 6 is the most important — if `run_machine_for_day` passes all 7 of those scenarios, you're very likely correct on the edge cases.
- **Trust the `moneyness` column.** Don't compute ATM from spot.
- **Keep per-minute lookups O(1).** `atm_by_minute` and `by_strike_minute` are pre-indexed. Don't add `df[df.datetime == x]` inside the inner loop.
- **Commit after every green test.** Revert to the last green state if you get stuck.
- **If a test in Task 6 fails on the `test_strike_locked_after_arm` case**, double-check that once `state["status"] == "ARMED"`, the code uses `locked_row_at(state["strike"], minute)` and NOT `atm_row_at(minute)`. The strike lock is the whole point.
