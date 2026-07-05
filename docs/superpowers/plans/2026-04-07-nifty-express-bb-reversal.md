# Nifty Express BB Reversal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a backtest engine for the "Nifty Express" strategy — BB mean-reversion on NIFTY spot that buys ATM weekly PE options.

**Architecture:** Standalone engine (`engine/bb_reversal_backtest.py`) with its own dataclass trade, data loading, BB calculation on spot 1-min, 3-step signal detection (breakout → red candle → break below red low), and PE option entry/exit tracking. Streamlit UI tab (`ui/bb_reversal_backtest_runner.py`) for parameter input and results display. Follows the existing pattern established by `engine/straddle_vwap_backtest.py` + `ui/straddle_vwap_backtest_runner.py`.

**Tech Stack:** Python, pandas, Streamlit, existing `indicators/bollinger.py`, existing data loading from `engine/data_loader.py` and `config.py`.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `engine/bb_reversal_backtest.py` | Create | Engine: load data, calculate BB on spot, run day-by-day signal detection, track PE option trades |
| `ui/bb_reversal_backtest_runner.py` | Create | Streamlit UI: parameter inputs, run button, results display, CSV download |
| `app.py` | Modify (line 42-53) | Add new tab for "BB Reversal" |
| `tests/test_bb_reversal.py` | Create | Unit tests for signal logic and trade management |

---

### Task 1: Trade Dataclass and Helper

**Files:**
- Create: `engine/bb_reversal_backtest.py`
- Test: `tests/test_bb_reversal.py`

- [ ] **Step 1: Write the failing test for trade dataclass**

```python
# tests/test_bb_reversal.py
"""Tests for BB Reversal (Nifty Express) backtest engine."""

import pytest
from engine.bb_reversal_backtest import BBReversalTrade, trades_to_dataframe


def test_trade_dataclass_fields():
    """Trade dataclass stores all required fields."""
    trade = BBReversalTrade(
        date="2025-06-10",
        spot_strike=24000.0,
        pe_strike=24000.0,
        expiry_date="2025-06-12",
        signal_step="break_red_low",
        entry_time="10:30",
        entry_price=190.0,
        spot_at_entry=23955.0,
        qty=75,
        tp_level=205.0,
        sl_level=175.0,
        exit_time="10:45",
        exit_price=205.0,
        exit_reason="TP",
        pnl_points=15.0,
        pnl_pct=7.895,
        pnl_inr=1125.0,
    )
    assert trade.entry_price == 190.0
    assert trade.tp_level == 205.0
    assert trade.sl_level == 175.0
    assert trade.exit_reason == "TP"


def test_trades_to_dataframe_empty():
    """Empty trade list returns empty DataFrame."""
    df = trades_to_dataframe([])
    assert len(df) == 0


def test_trades_to_dataframe_single():
    """Single trade converts to 1-row DataFrame."""
    trade = BBReversalTrade(
        date="2025-06-10",
        spot_strike=24000.0,
        pe_strike=24000.0,
        expiry_date="2025-06-12",
        signal_step="break_red_low",
        entry_time="10:30",
        entry_price=190.0,
        spot_at_entry=23955.0,
        qty=75,
        tp_level=205.0,
        sl_level=175.0,
        exit_time="10:45",
        exit_price=205.0,
        exit_reason="TP",
        pnl_points=15.0,
        pnl_pct=7.895,
        pnl_inr=1125.0,
    )
    df = trades_to_dataframe([trade])
    assert len(df) == 1
    assert df.iloc[0]["exit_reason"] == "TP"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bb_reversal.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.bb_reversal_backtest'`

- [ ] **Step 3: Write the trade dataclass and helper**

```python
# engine/bb_reversal_backtest.py
"""
Nifty Express — BB Reversal Backtest Engine.

Strategy:
  Signal on NIFTY spot 1-min candles using Bollinger Bands(20, 2):
    1. Spot close > BB upper → watching mode
    2. Red candle (close < open) → record its low ("redLow")
    3. Later candle close < redLow → BUY ATM PE (weekly expiry)

  Exit on PE option price:
    - TP: PE high >= entry + 15 → exit at entry + 15
    - SL: PE low <= entry - 15 → exit at entry - 15
    - If both SL & TP hit same candle → take SL
    - EOD: force exit at 15:20 at PE close

  Rules:
    - Entry window: 09:18 - 15:19
    - Force exit: 15:20
    - One trade at a time
    - Setup resets end of day (BB carries across days)
    - After exit, if spot still > BB upper → immediately watching again
    - Spot dropping below BB upper after setup starts → doesn't cancel setup
    - First red candle's low is used (subsequent reds ignored)
"""

import logging
import os
from dataclasses import asdict, dataclass
from typing import List, Optional

import pandas as pd

from config import (
    DATA_PATH,
    LOT_SIZE,
    SPOT_DATA_PATH,
    STRIKE_ROUNDING,
    get_nearest_weekly_expiry,
)
from engine.data_loader import load_data

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------

@dataclass
class BBReversalTrade:
    date: str                    # "YYYY-MM-DD"
    spot_strike: float           # ATM strike based on spot
    pe_strike: float             # PE strike traded
    expiry_date: str

    signal_step: str             # "break_red_low" always for this strategy
    entry_time: str              # "HH:MM"
    entry_price: float           # PE close at signal candle
    spot_at_entry: float         # spot price at entry
    qty: int

    tp_level: float              # entry + 15
    sl_level: float              # entry - 15

    exit_time: str
    exit_price: float
    exit_reason: str             # "TP" / "SL" / "EOD"

    pnl_points: float            # exit - entry (buying PE)
    pnl_pct: float
    pnl_inr: float               # pnl_points x qty


def trades_to_dataframe(trades: List[BBReversalTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_bb_reversal.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add engine/bb_reversal_backtest.py tests/test_bb_reversal.py
git commit -m "feat(bb-reversal): add trade dataclass and helpers"
```

---

### Task 2: Signal Detection Logic

**Files:**
- Modify: `engine/bb_reversal_backtest.py`
- Modify: `tests/test_bb_reversal.py`

The signal state machine has 3 states: IDLE → WATCHING → RED_FOUND.
Transitions:
- IDLE: spot close > BB upper → WATCHING
- WATCHING: red candle (close < open) → RED_FOUND (record redLow). Also: spot close > BB upper stays in WATCHING.
- RED_FOUND: candle close < redLow → SIGNAL (entry). Also: spot close > BB upper → reset to WATCHING (new breakout cancels old redLow).
- After trade exit: if spot > BB upper → WATCHING, else → IDLE.
- End of day: → IDLE (reset).

- [ ] **Step 1: Write failing tests for signal state machine**

```python
# Add to tests/test_bb_reversal.py

from engine.bb_reversal_backtest import SignalState, check_signal_state


def test_idle_to_watching_on_breakout():
    """Close > BB upper transitions from IDLE to WATCHING."""
    state = SignalState()
    check_signal_state(state, spot_close=24000, spot_open=23950,
                       spot_high=24010, spot_low=23940, bb_upper=23990)
    assert state.phase == "WATCHING"


def test_watching_to_red_found():
    """Red candle in WATCHING records redLow."""
    state = SignalState()
    state.phase = "WATCHING"
    # Red candle: close < open
    check_signal_state(state, spot_close=23960, spot_open=23980,
                       spot_high=23985, spot_low=23950, bb_upper=23970)
    assert state.phase == "RED_FOUND"
    assert state.red_low == 23950


def test_red_found_to_signal():
    """Close < redLow triggers signal."""
    state = SignalState()
    state.phase = "RED_FOUND"
    state.red_low = 23950
    fired = check_signal_state(state, spot_close=23940, spot_open=23955,
                               spot_high=23960, spot_low=23935, bb_upper=23970)
    assert fired is True


def test_red_found_no_signal_above_red_low():
    """Close >= redLow does not trigger."""
    state = SignalState()
    state.phase = "RED_FOUND"
    state.red_low = 23950
    fired = check_signal_state(state, spot_close=23955, spot_open=23960,
                               spot_high=23965, spot_low=23948, bb_upper=23970)
    assert fired is False
    assert state.phase == "RED_FOUND"


def test_green_candle_in_watching_stays():
    """Green candle in WATCHING doesn't change state."""
    state = SignalState()
    state.phase = "WATCHING"
    check_signal_state(state, spot_close=24010, spot_open=24000,
                       spot_high=24015, spot_low=23995, bb_upper=23990)
    assert state.phase == "WATCHING"


def test_red_found_new_breakout_resets():
    """New close > BB upper in RED_FOUND resets to WATCHING."""
    state = SignalState()
    state.phase = "RED_FOUND"
    state.red_low = 23950
    check_signal_state(state, spot_close=24010, spot_open=24000,
                       spot_high=24015, spot_low=23995, bb_upper=24005)
    # close(24010) > upper(24005) and close > open → green breakout
    # This resets: new breakout, WATCHING again, old redLow cleared
    assert state.phase == "WATCHING"
    assert state.red_low is None


def test_idle_stays_when_below_bb():
    """Close below BB upper stays IDLE."""
    state = SignalState()
    fired = check_signal_state(state, spot_close=23900, spot_open=23910,
                               spot_high=23920, spot_low=23890, bb_upper=23950)
    assert fired is False
    assert state.phase == "IDLE"


def test_reset_clears_state():
    """Reset returns to IDLE."""
    state = SignalState()
    state.phase = "RED_FOUND"
    state.red_low = 23950
    state.reset()
    assert state.phase == "IDLE"
    assert state.red_low is None


def test_only_first_red_candle_used():
    """Second red candle doesn't update redLow."""
    state = SignalState()
    state.phase = "WATCHING"
    # First red candle
    check_signal_state(state, spot_close=23960, spot_open=23980,
                       spot_high=23985, spot_low=23950, bb_upper=23970)
    assert state.red_low == 23950
    # Second red candle (lower low)
    check_signal_state(state, spot_close=23940, spot_open=23955,
                       spot_high=23958, spot_low=23930, bb_upper=23970)
    # But close(23940) < redLow(23950) → this is actually a SIGNAL, not a second red candle
    # Let me fix the test: second red candle that doesn't break redLow
    pass


def test_only_first_red_candle_used_v2():
    """Second red candle in RED_FOUND doesn't update redLow."""
    state = SignalState()
    state.phase = "RED_FOUND"
    state.red_low = 23950
    # Another red candle with higher low (doesn't break redLow)
    check_signal_state(state, spot_close=23955, spot_open=23965,
                       spot_high=23970, spot_low=23952, bb_upper=23970)
    # close(23955) > redLow(23950) so no signal, and already RED_FOUND so no update
    assert state.red_low == 23950  # unchanged
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bb_reversal.py -v`
Expected: FAIL with `ImportError: cannot import name 'SignalState'`

- [ ] **Step 3: Implement SignalState and check_signal_state**

Add to `engine/bb_reversal_backtest.py` after the `trades_to_dataframe` function:

```python
# ---------------------------------------------------------------------------
# Signal state machine
# ---------------------------------------------------------------------------

class SignalState:
    """Tracks the 3-step signal progression within a trading day.

    Phases:
        IDLE      → spot close > BB upper → WATCHING
        WATCHING  → red candle (close < open) → RED_FOUND (record redLow)
        RED_FOUND → candle close < redLow → SIGNAL fires (returns True)

    A new close > BB upper while in RED_FOUND resets to WATCHING.
    Only the first red candle's low is used.
    """

    def __init__(self):
        self.phase = "IDLE"
        self.red_low: Optional[float] = None

    def reset(self):
        self.phase = "IDLE"
        self.red_low = None


def check_signal_state(
    state: SignalState,
    spot_close: float,
    spot_open: float,
    spot_high: float,
    spot_low: float,
    bb_upper: float,
) -> bool:
    """Advance the signal state machine by one candle.

    Returns True if a sell signal fires (close < redLow).
    """
    is_above_bb = spot_close > bb_upper
    is_red = spot_close < spot_open

    if state.phase == "IDLE":
        if is_above_bb:
            state.phase = "WATCHING"
        return False

    elif state.phase == "WATCHING":
        if is_red:
            state.phase = "RED_FOUND"
            state.red_low = spot_low
        # Stay in WATCHING if green and above/below BB — doesn't matter
        return False

    elif state.phase == "RED_FOUND":
        # New breakout resets the setup
        if is_above_bb and not is_red:
            state.phase = "WATCHING"
            state.red_low = None
            return False

        # Check confirmation: close < redLow
        if spot_close < state.red_low:
            return True

        return False

    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_bb_reversal.py -v`
Expected: All tests PASS

- [ ] **Step 5: Remove the broken test_only_first_red_candle_used**

Remove the `test_only_first_red_candle_used` function (the one with `pass` at the end) from `tests/test_bb_reversal.py`. The `_v2` version covers the intent correctly.

- [ ] **Step 6: Run tests again to verify clean**

Run: `python -m pytest tests/test_bb_reversal.py -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add engine/bb_reversal_backtest.py tests/test_bb_reversal.py
git commit -m "feat(bb-reversal): add signal state machine with tests"
```

---

### Task 3: Backtest Engine Core

**Files:**
- Modify: `engine/bb_reversal_backtest.py`
- Modify: `tests/test_bb_reversal.py`

- [ ] **Step 1: Write integration test for the engine**

```python
# Add to tests/test_bb_reversal.py

import numpy as np

from engine.bb_reversal_backtest import BBReversalBacktestEngine


def test_engine_init():
    """Engine initializes with default parameters."""
    engine = BBReversalBacktestEngine(
        start_date="2025-06-01",
        end_date="2025-06-30",
    )
    assert engine.tp_points == 15
    assert engine.sl_points == 15
    assert engine.entry_start == "09:18"
    assert engine.entry_end == "15:19"
    assert engine.force_exit_time == "15:20"
    assert engine.bb_period == 20
    assert engine.bb_std == 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bb_reversal.py::test_engine_init -v`
Expected: FAIL with `ImportError: cannot import name 'BBReversalBacktestEngine'`

- [ ] **Step 3: Implement the engine class**

Add to `engine/bb_reversal_backtest.py`:

```python
# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BBReversalBacktestEngine:
    """Nifty Express — BB reversal PE buy strategy backtest engine."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        tp_points: float = 15.0,
        sl_points: float = 15.0,
        bb_period: int = 20,
        bb_std: float = 2.0,
        entry_start: str = "09:18",
        entry_end: str = "15:19",
        force_exit_time: str = "15:20",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.tp_points = tp_points
        self.sl_points = sl_points
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.entry_start = entry_start
        self.entry_end = entry_end
        self.force_exit_time = force_exit_time
        self.instrument = "NIFTY"
        self.lot_size = LOT_SIZE.get("NIFTY", 75)

        self._spot_1m: Optional[pd.DataFrame] = None
        self._options_1m: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_spot(self) -> pd.DataFrame:
        """Load 1-min spot data with warmup for BB calculation."""
        path = os.path.join(BASE_DIR, SPOT_DATA_PATH[self.instrument])
        df = pd.read_parquet(path)
        dt = pd.to_datetime(df["datetime"])
        if dt.dt.tz is None:
            dt = dt.dt.tz_localize("Asia/Kolkata")
        else:
            dt = dt.dt.tz_convert("Asia/Kolkata")
        df = df.copy()
        df["datetime"] = dt
        df["date"] = dt.dt.date
        df["time_str"] = dt.dt.strftime("%H:%M")

        start = pd.to_datetime(self.start_date).tz_localize("Asia/Kolkata")
        end = pd.to_datetime(self.end_date).tz_localize("Asia/Kolkata") + pd.Timedelta(days=1)
        # 5-day warmup for BB(20) — continuous across days
        warmup_start = start - pd.Timedelta(days=5)
        df = df[(df["datetime"] >= warmup_start) & (df["datetime"] < end)]
        return df.sort_values("datetime").reset_index(drop=True)

    def _calculate_bb(self, spot_df: pd.DataFrame) -> pd.DataFrame:
        """Calculate BB(period, std) on spot close. Continuous across days."""
        from indicators.bollinger import BollingerBands
        bb = BollingerBands(name="bb", period=self.bb_period, std_dev=self.bb_std)
        result = bb.calculate(spot_df["close"])
        spot_df = spot_df.copy()
        spot_df["bb_upper"] = result["upper"].values
        spot_df["bb_middle"] = result["middle"].values
        spot_df["bb_lower"] = result["lower"].values
        return spot_df

    def _get_atm_strike(self, spot: float) -> float:
        """Round spot to nearest strike interval."""
        rounding = STRIKE_ROUNDING.get(self.instrument, 50)
        return round(spot / rounding) * rounding

    def _prepare_data(self):
        """Load spot + options data, calculate BB on spot."""
        logger.info("Loading spot data...")
        spot_raw = self._load_spot()
        logger.info(f"Spot 1m: {len(spot_raw):,} rows")

        logger.info(f"Calculating BB({self.bb_period}, {self.bb_std}) on spot...")
        self._spot_1m = self._calculate_bb(spot_raw)

        logger.info("Loading options data...")
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        self._options_1m = load_data(
            options_path, self.start_date, self.end_date, "weekly"
        )
        logger.info(f"Options 1m: {len(self._options_1m):,} rows")

    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[BBReversalTrade]:
        """Run backtest. Returns list of BBReversalTrade."""
        self._prepare_data()

        # Get trading dates from options data (spot may have extra warmup days)
        all_dates = sorted(self._options_1m["date"].unique())
        trades: List[BBReversalTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))

            day_trades = self._process_day(trading_date)
            trades.extend(day_trades)

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    def _process_day(self, trading_date) -> List[BBReversalTrade]:
        """Process a single trading day. Returns 0 or more trades."""
        # Spot data for today
        day_spot = self._spot_1m[self._spot_1m["date"] == trading_date]
        if day_spot.empty:
            return []

        # Options data for today
        day_options = self._options_1m[self._options_1m["date"] == trading_date]
        if day_options.empty:
            return []

        expiry_date = get_nearest_weekly_expiry(trading_date)
        if expiry_date is None:
            return []

        trades: List[BBReversalTrade] = []
        signal_state = SignalState()

        # Position state
        in_position = False
        entry_price = None
        pe_strike = None
        tp_level = None
        sl_level = None
        entry_time = None
        spot_at_entry = None

        for _, spot_row in day_spot.iterrows():
            t_str = spot_row["time_str"]
            bb_upper = spot_row["bb_upper"]

            # Skip if BB not yet valid (warmup)
            if pd.isna(bb_upper):
                continue

            # ============ 1. EXIT CHECKS (on PE option price) ============
            if in_position:
                # Find PE candle for this minute
                pe_candle = day_options[
                    (day_options["datetime"] == spot_row["datetime"])
                    & (day_options["strike"] == pe_strike)
                    & (day_options["option_type"] == "PE")
                ]

                if not pe_candle.empty:
                    pc = pe_candle.iloc[0]

                    # SL checked first (worst case assumption on same-candle)
                    if pc["low"] <= sl_level:
                        trade = self._make_trade(
                            trading_date, pe_strike, expiry_date,
                            entry_time, entry_price, spot_at_entry,
                            tp_level, sl_level,
                            t_str, sl_level, "SL",
                        )
                        trades.append(trade)
                        in_position = False
                        # After exit: check if spot is above BB for re-entry
                        if spot_row["close"] > bb_upper:
                            signal_state.phase = "WATCHING"
                        else:
                            signal_state = SignalState()

                    elif pc["high"] >= tp_level:
                        trade = self._make_trade(
                            trading_date, pe_strike, expiry_date,
                            entry_time, entry_price, spot_at_entry,
                            tp_level, sl_level,
                            t_str, tp_level, "TP",
                        )
                        trades.append(trade)
                        in_position = False
                        if spot_row["close"] > bb_upper:
                            signal_state.phase = "WATCHING"
                        else:
                            signal_state = SignalState()

                    elif t_str >= self.force_exit_time:
                        trade = self._make_trade(
                            trading_date, pe_strike, expiry_date,
                            entry_time, entry_price, spot_at_entry,
                            tp_level, sl_level,
                            t_str, pc["close"], "EOD",
                        )
                        trades.append(trade)
                        in_position = False
                        signal_state = SignalState()

                elif t_str >= self.force_exit_time:
                    # No PE data at force exit time — close flat
                    trade = self._make_trade(
                        trading_date, pe_strike, expiry_date,
                        entry_time, entry_price, spot_at_entry,
                        tp_level, sl_level,
                        t_str, entry_price, "EOD",
                    )
                    trades.append(trade)
                    in_position = False
                    signal_state = SignalState()

                if in_position:
                    continue

            # ============ 2. SIGNAL DETECTION (on spot) ============
            if t_str < self.entry_start or t_str > self.entry_end:
                # Outside entry window — still advance signal state for watching
                # but only for transitions that don't require entry
                if t_str < self.entry_start:
                    # Pre-window: allow state to build (watching, red candle)
                    check_signal_state(
                        signal_state,
                        spot_close=spot_row["close"],
                        spot_open=spot_row["open"],
                        spot_high=spot_row["high"],
                        spot_low=spot_row["low"],
                        bb_upper=bb_upper,
                    )
                continue

            fired = check_signal_state(
                signal_state,
                spot_close=spot_row["close"],
                spot_open=spot_row["open"],
                spot_high=spot_row["high"],
                spot_low=spot_row["low"],
                bb_upper=bb_upper,
            )

            if fired and not in_position:
                # Find ATM PE for entry
                spot_price = spot_row["close"]
                atm_strike = self._get_atm_strike(spot_price)

                pe_candle = day_options[
                    (day_options["datetime"] == spot_row["datetime"])
                    & (day_options["strike"] == atm_strike)
                    & (day_options["option_type"] == "PE")
                ]

                if pe_candle.empty:
                    # No PE data — skip this signal, reset to allow new setup
                    signal_state = SignalState()
                    if spot_row["close"] > bb_upper:
                        signal_state.phase = "WATCHING"
                    continue

                pc = pe_candle.iloc[0]
                entry_price = pc["close"]
                pe_strike = atm_strike
                tp_level = round(entry_price + self.tp_points, 2)
                sl_level = round(entry_price - self.sl_points, 2)
                entry_time = t_str
                spot_at_entry = spot_price
                in_position = True

                # Reset signal state — will rebuild after exit
                signal_state = SignalState()

                logger.debug(
                    f"{trading_date} {t_str} BUY PE "
                    f"strike={pe_strike} @ {entry_price} "
                    f"TP={tp_level} SL={sl_level} spot={spot_price}"
                )

        # Safety net: force close if still in position at day end
        if in_position:
            # Find last available PE candle
            pe_data = day_options[
                (day_options["strike"] == pe_strike)
                & (day_options["option_type"] == "PE")
            ]
            if not pe_data.empty:
                last_pe = pe_data.iloc[-1]
                trade = self._make_trade(
                    trading_date, pe_strike, expiry_date,
                    entry_time, entry_price, spot_at_entry,
                    tp_level, sl_level,
                    last_pe["time_only"].strftime("%H:%M") if hasattr(last_pe["time_only"], "strftime") else "15:30",
                    last_pe["close"], "EOD",
                )
            else:
                trade = self._make_trade(
                    trading_date, pe_strike, expiry_date,
                    entry_time, entry_price, spot_at_entry,
                    tp_level, sl_level,
                    "15:30", entry_price, "EOD",
                )
            trades.append(trade)

        return trades

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_trade(
        self, trading_date, pe_strike, expiry_date,
        entry_time, entry_price, spot_at_entry,
        tp_level, sl_level,
        exit_time, exit_price, exit_reason,
    ) -> BBReversalTrade:
        # Buying PE: profit when PE price rises
        pnl_points = round(exit_price - entry_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        pnl_inr = round(pnl_points * self.lot_size, 2)
        return BBReversalTrade(
            date=str(trading_date),
            spot_strike=pe_strike,
            pe_strike=pe_strike,
            expiry_date=str(expiry_date),
            signal_step="break_red_low",
            entry_time=entry_time,
            entry_price=entry_price,
            spot_at_entry=spot_at_entry,
            qty=self.lot_size,
            tp_level=tp_level,
            sl_level=sl_level,
            exit_time=exit_time,
            exit_price=round(exit_price, 2),
            exit_reason=exit_reason,
            pnl_points=pnl_points,
            pnl_pct=pnl_pct,
            pnl_inr=pnl_inr,
        )
```

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/test_bb_reversal.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add engine/bb_reversal_backtest.py tests/test_bb_reversal.py
git commit -m "feat(bb-reversal): add backtest engine with data loading and day processing"
```

---

### Task 4: Streamlit UI Tab

**Files:**
- Create: `ui/bb_reversal_backtest_runner.py`
- Modify: `app.py` (lines 42-83)

- [ ] **Step 1: Create the UI runner**

```python
# ui/bb_reversal_backtest_runner.py
"""
Nifty Express — BB Reversal PE Buy Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.bb_reversal_backtest import BBReversalBacktestEngine, trades_to_dataframe


def render_bb_reversal_backtest():
    st.header("Nifty Express — BB Reversal PE Buy")
    st.caption(
        "BB(20,2) breakout on spot → red candle → break below red low → BUY ATM PE  |  "
        "TP/SL: ±15 pts on PE  |  09:18 - 15:20"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-20").date(), key="bbr_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-12").date(), key="bbr_end"
            )

        with col2:
            st.markdown("**Exit (points on PE)**")
            tp_pts = st.number_input(
                "TP (pts)", value=15.0, step=1.0, min_value=1.0, key="bbr_tp"
            )
            sl_pts = st.number_input(
                "SL (pts)", value=15.0, step=1.0, min_value=1.0, key="bbr_sl"
            )

    with st.expander("Bollinger Band Settings", expanded=False):
        bc1, bc2 = st.columns(2)
        with bc1:
            bb_period = st.number_input(
                "BB Period", value=20, min_value=5, max_value=100, key="bbr_bb_period"
            )
        with bc2:
            bb_std = st.number_input(
                "BB Std Dev", value=2.0, step=0.1, min_value=0.5, key="bbr_bb_std"
            )

    with st.expander("Time Windows", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            entry_start = st.text_input(
                "Entry window start", value="09:18", key="bbr_entry_start"
            )
        with tc2:
            entry_end = st.text_input(
                "Entry window end", value="15:19", key="bbr_entry_end"
            )
        with tc3:
            force_exit = st.text_input(
                "Force exit time", value="15:20", key="bbr_force_exit"
            )

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="bbr_run"):
        engine = BBReversalBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            tp_points=float(tp_pts),
            sl_points=float(sl_pts),
            bb_period=int(bb_period),
            bb_std=float(bb_std),
            entry_start=entry_start,
            entry_end=entry_end,
            force_exit_time=force_exit,
        )

        progress_bar = st.progress(0.0)
        status_text = st.empty()

        def on_progress(i, total, date_str):
            progress_bar.progress(min((i + 1) / total, 1.0))
            status_text.text(f"Processing {date_str}  ({i + 1} / {total})")

        trades = engine.run(progress_callback=on_progress)

        progress_bar.empty()
        status_text.empty()

        if not trades:
            st.warning("No trades found for the selected parameters and date range.")
            return

        st.session_state["bbr_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "bbr_results" in st.session_state:
        _show_results(st.session_state["bbr_results"])


# ----------------------------------------------------------------
# Results display
# ----------------------------------------------------------------

def _show_results(df: pd.DataFrame):
    st.divider()
    st.subheader("Results")

    total = len(df)
    wins = int((df["pnl_inr"] > 0).sum())
    losses = int((df["pnl_inr"] < 0).sum())
    win_rate = wins / total * 100 if total > 0 else 0
    total_pnl = df["pnl_inr"].sum()
    avg_pnl = df["pnl_inr"].mean()

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Trades", total)
    c2.metric("Wins", wins)
    c3.metric("Losses", losses)
    c4.metric("Win Rate", f"{win_rate:.1f}%")
    c5.metric("Total P&L", f"\u20b9{total_pnl:,.0f}")
    c6.metric("Avg P&L / Trade", f"\u20b9{avg_pnl:,.0f}")

    # Exit reason breakdown
    reasons = df["exit_reason"].value_counts()
    r1, r2, r3 = st.columns(3)
    r1.metric("SL exits", int(reasons.get("SL", 0)))
    r2.metric("TP exits", int(reasons.get("TP", 0)))
    r3.metric("EOD exits", int(reasons.get("EOD", 0)))

    # Avg P&L by exit reason
    st.markdown("**Avg P&L by Exit Reason:**")
    reason_stats = df.groupby("exit_reason")["pnl_inr"].agg(["count", "mean", "sum"])
    reason_stats.columns = ["Count", "Avg P&L", "Total P&L"]
    st.dataframe(
        reason_stats.style.format({"Avg P&L": "\u20b9{:,.0f}", "Total P&L": "\u20b9{:,.0f}"}),
    )

    # Equity curve
    st.divider()
    st.subheader("Equity Curve")
    equity = df["pnl_inr"].cumsum()
    chart_df = pd.DataFrame(
        {"Cumulative P&L (\u20b9)": equity.values},
        index=range(1, len(equity) + 1),
    )
    chart_df.index.name = "Trade #"
    st.line_chart(chart_df)

    # Daily P&L
    st.divider()
    st.subheader("Daily P&L")
    daily = df.groupby("date")["pnl_inr"].sum().reset_index()
    daily.columns = ["Date", "P&L"]
    st.bar_chart(daily.set_index("Date"))

    # All trades table
    st.divider()
    st.subheader("All Trades")

    filter_reason = st.multiselect(
        "Filter by exit reason",
        options=["SL", "TP", "EOD"],
        key="bbr_filter_reason",
    )

    filtered = df.copy()
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="bb_reversal_pe_buy_backtest.csv",
        mime="text/csv",
        key="bbr_download",
    )
```

- [ ] **Step 2: Wire into app.py**

In `app.py`, add the import:

```python
from ui.bb_reversal_backtest_runner import render_bb_reversal_backtest
```

Update the tabs line (line 42) to add the new tab:

```python
tab_dashboard, tab_trades, tab_backtest, tab_forward, tab_dema_st, tab_st_ema, tab_straddle_vwap, tab_boom, tab_boom_st, tab_vwap_ema_rsi, tab_bb_reversal = st.tabs([
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
])
```

Add the tab content at the end (after the last `with` block):

```python
with tab_bb_reversal:
    render_bb_reversal_backtest()
```

- [ ] **Step 3: Smoke test the UI**

Run: `streamlit run app.py`
Expected: New "BB Reversal PE" tab appears. Parameters render correctly. Clicking "Run Backtest" starts the engine without errors.

- [ ] **Step 4: Commit**

```bash
git add ui/bb_reversal_backtest_runner.py app.py
git commit -m "feat(bb-reversal): add Streamlit UI tab for BB Reversal PE strategy"
```

---

### Task 5: End-to-End Validation

**Files:**
- Modify: `tests/test_bb_reversal.py`

- [ ] **Step 1: Write a quick smoke test with real data**

```python
# Add to tests/test_bb_reversal.py

import os

@pytest.mark.skipif(
    not os.path.exists("data/spot/nifty/NIFTY_1m.parquet"),
    reason="Spot data not available"
)
def test_engine_runs_one_month():
    """Engine runs without errors on one month of real data."""
    engine = BBReversalBacktestEngine(
        start_date="2025-03-01",
        end_date="2025-03-31",
    )
    trades = engine.run()
    # Should produce at least some trades in a month
    assert isinstance(trades, list)
    for t in trades:
        assert t.exit_reason in ("TP", "SL", "EOD")
        assert t.entry_price > 0
        assert t.pnl_inr == round((t.exit_price - t.entry_price) * t.qty, 2)
```

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest tests/test_bb_reversal.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run a quick backtest and inspect output**

```bash
python -c "
from engine.bb_reversal_backtest import BBReversalBacktestEngine, trades_to_dataframe
engine = BBReversalBacktestEngine('2025-03-01', '2025-03-31')
trades = engine.run()
df = trades_to_dataframe(trades)
print(f'Trades: {len(df)}')
if len(df) > 0:
    print(df[['date','entry_time','entry_price','exit_time','exit_price','exit_reason','pnl_inr']].head(10))
    print(f\"Total P&L: {df['pnl_inr'].sum():.0f}\")
    print(f\"Win rate: {(df['pnl_inr']>0).mean()*100:.1f}%\")
    print(f\"Exit reasons: {df['exit_reason'].value_counts().to_dict()}\")
"
```

Expected: Trades print without errors. Each trade has valid entry/exit prices and reasons.

- [ ] **Step 4: Commit**

```bash
git add tests/test_bb_reversal.py
git commit -m "test(bb-reversal): add end-to-end validation test"
```
