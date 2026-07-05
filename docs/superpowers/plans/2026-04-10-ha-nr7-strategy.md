# HA-NR7 Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a specialized backtest engine for the HA-NR7 strategy — Heikin-Ashi neutral candle alert on spot triggers NR7 entry on ITM options with pyramiding, reversal, and DTE-based TP/SL.

**Architecture:** Custom state machine engine (same pattern as PRT, Boom SMA). Spot 3-min HA for alerts, option 3-min NR7/EMA for entry signals, 1-min option OHLC for SL/TP fills. State machine: IDLE → ALERT_ACTIVE → POSITION_OPEN → DAY_STOPPED. Reuses shared utilities (data_loader, expiry_calendar, EMA indicator).

**Tech Stack:** Python, pandas, Streamlit, existing indicators/data_loader/expiry_calendar

**Spec:** `docs/superpowers/specs/2026-04-10-ha-nr7-strategy-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `indicators/heikin_ashi.py` | Create | HA candle computation (regular OHLC → HA columns) |
| `engine/ha_nr7_backtest.py` | Create | Engine class, state machine, data pipeline, trade logic, helpers (NR7, DTE, EMA adj) |
| `ui/ha_nr7_backtest_runner.py` | Create | Streamlit UI — parameters, run, results display |
| `tests/test_ha_nr7.py` | Create | Unit tests for HA, NR7, DTE, EMA adjustment, state machine basics |
| `app.py` | Modify (lines 14, 29, 46-61, add new tab) | Add HA-NR7 tab |

---

### Task 1: Heikin-Ashi Indicator

**Files:**
- Create: `indicators/heikin_ashi.py`
- Create: `tests/test_ha_nr7.py`

- [ ] **Step 1: Write the failing test for HA calculation**

```python
# tests/test_ha_nr7.py
import pandas as pd
import pytest

from indicators.heikin_ashi import compute_heikin_ashi


class TestHeikinAshi:
    """Test HA candle computation matches TradingView PineScript formula."""

    def _make_spot_df(self, rows):
        """Helper: list of (open, high, low, close) → DataFrame."""
        return pd.DataFrame(rows, columns=["open", "high", "low", "close"])

    def test_first_candle_ha_open(self):
        """First HA_Open = (Open + Close) / 2."""
        df = self._make_spot_df([(100, 110, 95, 105)])
        result = compute_heikin_ashi(df)
        assert result["ha_open"].iloc[0] == pytest.approx((100 + 105) / 2)

    def test_first_candle_ha_close(self):
        """HA_Close = (O + H + L + C) / 4."""
        df = self._make_spot_df([(100, 110, 95, 105)])
        result = compute_heikin_ashi(df)
        assert result["ha_close"].iloc[0] == pytest.approx((100 + 110 + 95 + 105) / 4)

    def test_first_candle_ha_high(self):
        """HA_High = max(High, HA_Open, HA_Close)."""
        df = self._make_spot_df([(100, 110, 95, 105)])
        result = compute_heikin_ashi(df)
        ha_open = (100 + 105) / 2  # 102.5
        ha_close = (100 + 110 + 95 + 105) / 4  # 102.5
        assert result["ha_high"].iloc[0] == pytest.approx(max(110, ha_open, ha_close))

    def test_first_candle_ha_low(self):
        """HA_Low = min(Low, HA_Open, HA_Close)."""
        df = self._make_spot_df([(100, 110, 95, 105)])
        result = compute_heikin_ashi(df)
        ha_open = (100 + 105) / 2  # 102.5
        ha_close = (100 + 110 + 95 + 105) / 4  # 102.5
        assert result["ha_low"].iloc[0] == pytest.approx(min(95, ha_open, ha_close))

    def test_second_candle_ha_open_uses_previous(self):
        """HA_Open[1] = (prev_HA_Open + prev_HA_Close) / 2."""
        df = self._make_spot_df([
            (100, 110, 95, 105),
            (106, 115, 100, 112),
        ])
        result = compute_heikin_ashi(df)
        prev_ha_open = (100 + 105) / 2  # 102.5
        prev_ha_close = (100 + 110 + 95 + 105) / 4  # 102.5
        expected_ha_open = (prev_ha_open + prev_ha_close) / 2
        assert result["ha_open"].iloc[1] == pytest.approx(expected_ha_open)

    def test_original_columns_preserved(self):
        """Original OHLC columns are preserved alongside HA columns."""
        df = self._make_spot_df([(100, 110, 95, 105)])
        result = compute_heikin_ashi(df)
        assert result["open"].iloc[0] == 100
        assert result["high"].iloc[0] == 110
        assert result["low"].iloc[0] == 95
        assert result["close"].iloc[0] == 105

    def test_neutral_candle_detection(self):
        """A neutral HA candle has small body and large regular range."""
        # Craft a candle where HA body ≈ 0 but regular range > 20
        df = self._make_spot_df([
            (100, 105, 95, 100),   # seed candle
            (100, 121, 99, 100),   # HA body should be tiny, regular range = 22
        ])
        result = compute_heikin_ashi(df)
        ha_body = abs(result["ha_close"].iloc[1] - result["ha_open"].iloc[1])
        regular_range = result["high"].iloc[1] - result["low"].iloc[1]
        assert regular_range == 22
        assert ha_body < 2.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -m pytest tests/test_ha_nr7.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'indicators.heikin_ashi'`

- [ ] **Step 3: Implement Heikin-Ashi indicator**

```python
# indicators/heikin_ashi.py
"""
Heikin-Ashi candle computation.

Matches TradingView PineScript formula:
  HA_Close = (O + H + L + C) / 4
  HA_Open  = first ? (O + C) / 2 : (prev_HA_Open + prev_HA_Close) / 2
  HA_High  = max(H, HA_Open, HA_Close)
  HA_Low   = min(L, HA_Open, HA_Close)
"""

import numpy as np
import pandas as pd


def compute_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Add ha_open, ha_close, ha_high, ha_low columns to a DataFrame with OHLC data.

    Args:
        df: DataFrame with columns: open, high, low, close

    Returns:
        Copy of df with four new HA columns added. Original columns preserved.
    """
    out = df.copy()
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(df)

    ha_close = (o + h + l + c) / 4.0

    ha_open = np.empty(n)
    ha_open[0] = (o[0] + c[0]) / 2.0
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0

    ha_high = np.maximum(h, np.maximum(ha_open, ha_close))
    ha_low = np.minimum(l, np.minimum(ha_open, ha_close))

    out["ha_open"] = ha_open
    out["ha_close"] = ha_close
    out["ha_high"] = ha_high
    out["ha_low"] = ha_low

    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -m pytest tests/test_ha_nr7.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

Suggested message: `feat: add Heikin-Ashi indicator for HA-NR7 strategy`

---

### Task 2: Helper Functions — NR7, DTE, EMA Adjustment

**Files:**
- Create: `engine/ha_nr7_backtest.py` (initial version — helpers only)
- Modify: `tests/test_ha_nr7.py` (add tests)

- [ ] **Step 1: Write failing tests for NR7, DTE lookup, and EMA adjustment**

Append to `tests/test_ha_nr7.py`:

```python
from engine.ha_nr7_backtest import compute_nr7, get_dte_tp_sl, adjust_tp_for_ema


class TestNR7:
    """Test NR7 computation matching LuxAlgo PineScript: rng == ta.lowest(rng, 7)."""

    def test_nr7_basic(self):
        """NR7 fires on the candle with the smallest range in 7 candles."""
        # Ranges: 10, 8, 12, 9, 11, 7, 6 → candle 6 (idx=6) has smallest
        df = pd.DataFrame({
            "high": [110, 108, 112, 109, 111, 107, 106],
            "low":  [100, 100, 100, 100, 100, 100, 100],
        })
        result = compute_nr7(df, lookback=7)
        assert result.iloc[6] == True
        # First 6 candles don't have 7 bars of history → NaN/False
        assert result.iloc[:6].sum() == 0

    def test_nr7_tie_counts(self):
        """Ties count as NR7 (== not <)."""
        # Ranges: 10, 8, 12, 9, 11, 8, 8 → candles 5 and 6 both tie at 8
        df = pd.DataFrame({
            "high": [110, 108, 112, 109, 111, 108, 108],
            "low":  [100, 100, 100, 100, 100, 100, 100],
        })
        result = compute_nr7(df, lookback=7)
        assert result.iloc[6] == True

    def test_nr7_not_smallest(self):
        """Candle whose range is not the smallest → NR7 = False."""
        # Ranges: 5, 10, 8, 12, 9, 11, 7 → candle 6 range=7, but min is 5
        df = pd.DataFrame({
            "high": [105, 110, 108, 112, 109, 111, 107],
            "low":  [100, 100, 100, 100, 100, 100, 100],
        })
        result = compute_nr7(df, lookback=7)
        assert result.iloc[6] == False

    def test_nr7_needs_7_candles(self):
        """NR7 returns False/NaN for first 6 candles (insufficient history)."""
        df = pd.DataFrame({
            "high": [110, 108, 112, 109, 111, 107, 106, 115],
            "low":  [100, 100, 100, 100, 100, 100, 100, 100],
        })
        result = compute_nr7(df, lookback=7)
        assert result.iloc[:6].any() == False


class TestDteTpSl:
    """Test DTE-based TP/SL lookup."""

    def test_dte_0_expiry_day(self):
        tp, sl = get_dte_tp_sl(0)
        assert tp == 15.0
        assert sl == 15.0

    def test_dte_1(self):
        tp, sl = get_dte_tp_sl(1)
        assert tp == 12.5
        assert sl == 12.5

    def test_dte_2(self):
        tp, sl = get_dte_tp_sl(2)
        assert tp == 10.0
        assert sl == 10.0

    def test_dte_3(self):
        tp, sl = get_dte_tp_sl(3)
        assert tp == 7.5
        assert sl == 7.5

    def test_dte_4_and_above(self):
        tp, sl = get_dte_tp_sl(4)
        assert tp == 5.0
        assert sl == 7.5
        tp2, sl2 = get_dte_tp_sl(10)
        assert tp2 == 5.0
        assert sl2 == 7.5


class TestEmaAdjustment:
    """Test EMA-based TP adjustment."""

    def test_above_both_emas_tp_gte_7_5_reduces(self):
        """Entry above both EMAs and TP >= 7.5 → reduce to 5%."""
        tp, adjusted = adjust_tp_for_ema(
            tp_pct=12.5, entry_price=200, ema10=195, ema21=190
        )
        assert tp == 5.0
        assert adjusted == True

    def test_below_both_emas_tp_lte_7_5_increases(self):
        """Entry below both EMAs and TP <= 7.5 → increase to 10%."""
        tp, adjusted = adjust_tp_for_ema(
            tp_pct=5.0, entry_price=150, ema10=155, ema21=160
        )
        assert tp == 10.0
        assert adjusted == True

    def test_above_both_tp_below_threshold_no_change(self):
        """Entry above both EMAs but TP < 7.5 → no change."""
        tp, adjusted = adjust_tp_for_ema(
            tp_pct=5.0, entry_price=200, ema10=195, ema21=190
        )
        assert tp == 5.0
        assert adjusted == False

    def test_below_both_tp_above_threshold_no_change(self):
        """Entry below both EMAs but TP > 7.5 → no change."""
        tp, adjusted = adjust_tp_for_ema(
            tp_pct=10.0, entry_price=150, ema10=155, ema21=160
        )
        assert tp == 10.0
        assert adjusted == False

    def test_between_emas_no_change(self):
        """Entry between EMAs → no change regardless of TP."""
        tp, adjusted = adjust_tp_for_ema(
            tp_pct=12.5, entry_price=155, ema10=150, ema21=160
        )
        assert tp == 12.5
        assert adjusted == False

    def test_boundary_tp_7_5_above_both(self):
        """TP exactly 7.5 and above both → reduces to 5."""
        tp, adjusted = adjust_tp_for_ema(
            tp_pct=7.5, entry_price=200, ema10=195, ema21=190
        )
        assert tp == 5.0
        assert adjusted == True

    def test_boundary_tp_7_5_below_both(self):
        """TP exactly 7.5 and below both → increases to 10."""
        tp, adjusted = adjust_tp_for_ema(
            tp_pct=7.5, entry_price=150, ema10=155, ema21=160
        )
        assert tp == 10.0
        assert adjusted == True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -m pytest tests/test_ha_nr7.py -v -k "NR7 or Dte or Ema"`
Expected: FAIL with `ImportError: cannot import name 'compute_nr7'`

- [ ] **Step 3: Implement helper functions**

```python
# engine/ha_nr7_backtest.py (initial version — helpers only, engine class added in Task 3)
"""
HA-NR7 Strategy Backtest Engine.

Strategy:
  Alert: Heikin-Ashi neutral candle on 3-min spot (body < 2.5, range > 20)
  Entry: NR7 on ITM option 3-min chart → buy at next candle open
  Pyramiding: up to 3 lots on same-side NR7
  Reversal: opposite-side NR7 → close + enter new side
  Exits: SL/TP on 1-min (DTE-based %), EOD at 14:55

No-lookahead: 3-min candle at T known at T+3, entry at T+3 open.
"""

import logging
import math
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import DATA_PATH, LOT_SIZE, SPOT_DATA_PATH
from engine.data_loader import load_data
from engine.expiry_calendar import get_nearest_weekly_expiry, NIFTY_WEEKLY_EXPIRY_DATES
from indicators.ema import EMA
from indicators.heikin_ashi import compute_heikin_ashi

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.dirname(__file__))


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def compute_nr7(df: pd.DataFrame, lookback: int = 7) -> pd.Series:
    """Compute NR7 flag per candle.

    NR7 = True when current candle's range is the smallest (or tied)
    in the last ``lookback`` candles including itself.
    Matches LuxAlgo PineScript: ``rng == ta.lowest(rng, 7)``.

    Returns a boolean Series aligned to df's index.
    First ``lookback - 1`` values are False (insufficient history).
    """
    rng = df["high"] - df["low"]
    min_rng = rng.rolling(window=lookback, min_periods=lookback).min()
    return (rng == min_rng).fillna(False)


# DTE lookup table: trading_dte → (tp_pct, sl_pct)
_DTE_TABLE: Dict[int, Tuple[float, float]] = {
    0: (15.0, 15.0),
    1: (12.5, 12.5),
    2: (10.0, 10.0),
    3: (7.5, 7.5),
}
_DTE_DEFAULT: Tuple[float, float] = (5.0, 7.5)  # DTE >= 4


def get_dte_tp_sl(dte: int) -> Tuple[float, float]:
    """Return (tp_pct, sl_pct) for a given trading DTE."""
    return _DTE_TABLE.get(dte, _DTE_DEFAULT)


def adjust_tp_for_ema(
    tp_pct: float,
    entry_price: float,
    ema10: float,
    ema21: float,
) -> Tuple[float, bool]:
    """Adjust TP based on entry price position relative to EMAs.

    Returns (adjusted_tp_pct, was_adjusted).
    """
    if entry_price > ema10 and entry_price > ema21 and tp_pct >= 7.5:
        return 5.0, True
    if entry_price < ema10 and entry_price < ema21 and tp_pct <= 7.5:
        return 10.0, True
    return tp_pct, False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -m pytest tests/test_ha_nr7.py -v`
Expected: All tests PASS (HA tests + NR7 + DTE + EMA adjustment)

- [ ] **Step 5: Commit**

Suggested message: `feat: add NR7, DTE, and EMA adjustment helpers for HA-NR7 strategy`

---

### Task 3: Trade Dataclass + Engine Scaffold + Data Pipeline

**Files:**
- Modify: `engine/ha_nr7_backtest.py` (add dataclass, engine class, data pipeline)

- [ ] **Step 1: Add EngineState enum, trade dataclass, and trades_to_dataframe**

Append to `engine/ha_nr7_backtest.py` after the helper functions:

```python
# ------------------------------------------------------------------
# State machine & data types
# ------------------------------------------------------------------

class EngineState(Enum):
    IDLE = "IDLE"
    ALERT_ACTIVE = "ALERT_ACTIVE"
    POSITION_OPEN = "POSITION_OPEN"
    DAY_STOPPED = "DAY_STOPPED"


@dataclass
class HaNr7Trade:
    """Single completed trade record."""

    entry_date: str
    alert_candle_time: str
    entry_time: str
    exit_time: str
    option_type: str          # CE / PE
    strike: int
    entry_prices: str         # stringified list for CSV compat
    avg_entry: float
    num_lots: int
    exit_price: float
    exit_reason: str          # TP / SL / EOD / REVERSAL / REVERSAL_STOP
    tp_pct: float
    sl_pct: float
    dte: int
    ema_adjusted: bool
    is_reversal: bool
    pnl_points: float
    pnl_inr: float


def trades_to_dataframe(trades: List[HaNr7Trade]) -> pd.DataFrame:
    """Convert list of HaNr7Trade to DataFrame."""
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
```

- [ ] **Step 2: Add engine __init__ and data loading methods**

Append to `engine/ha_nr7_backtest.py`:

```python
# ------------------------------------------------------------------
# Engine
# ------------------------------------------------------------------

class HaNr7BacktestEngine:
    """HA-NR7 strategy backtest engine."""

    _SPOT_PATHS = {
        "NIFTY": "data/spot/nifty/NIFTY_1m.parquet",
    }

    def __init__(
        self,
        start_date: str,
        end_date: str,
        instrument: str = "NIFTY",
        strike_rounding: int = 100,
        ha_body_threshold: float = 2.5,
        ha_range_threshold: float = 20.0,
        nr7_lookback: int = 7,
        nr7_scan_window: int = 5,
        ema_short_period: int = 10,
        ema_long_period: int = 21,
        trading_start: str = "09:30",
        last_entry: str = "14:45",
        force_exit: str = "14:55",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.instrument = instrument
        self.strike_rounding = strike_rounding
        self.ha_body_threshold = ha_body_threshold
        self.ha_range_threshold = ha_range_threshold
        self.nr7_lookback = nr7_lookback
        self.nr7_scan_window = nr7_scan_window
        self.ema_short_period = ema_short_period
        self.ema_long_period = ema_long_period
        self.trading_start = trading_start
        self.last_entry = last_entry
        self.force_exit = force_exit
        self.lot_size = LOT_SIZE.get(instrument, 65)

        # Data holders
        self._spot_3m: Optional[pd.DataFrame] = None
        self._options_1m: Optional[pd.DataFrame] = None
        self._options_3m: Optional[pd.DataFrame] = None
        self._trading_dates: Optional[List] = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_parquet_1m(path: str, start_str: str, end_str: str, warmup_days: int = 0):
        """Load a 1-min parquet with optional warmup."""
        df = pd.read_parquet(path)
        dt = pd.to_datetime(df["datetime"])
        if dt.dt.tz is None:
            dt = dt.dt.tz_localize("Asia/Kolkata")
        else:
            dt = dt.dt.tz_convert("Asia/Kolkata")
        df = df.copy()
        df["datetime"] = dt
        df["date"] = dt.dt.date
        start = pd.to_datetime(start_str).tz_localize("Asia/Kolkata")
        end = pd.to_datetime(end_str).tz_localize("Asia/Kolkata") + pd.Timedelta(days=1)
        if warmup_days:
            start = start - pd.Timedelta(days=warmup_days)
        df = df[(df["datetime"] >= start) & (df["datetime"] < end)]
        return df.sort_values("datetime").reset_index(drop=True)

    def _load_and_prepare_spot(self):
        """Load spot 1-min → resample to 3-min → compute HA → detect alerts."""
        path = os.path.join(BASE_DIR, self._SPOT_PATHS[self.instrument])
        logger.info(f"Loading spot data from {path}...")
        spot_1m = self._load_parquet_1m(path, self.start_date, self.end_date, warmup_days=1)
        logger.info(f"Spot 1m: {len(spot_1m):,} rows")

        # Extract unique trading dates from spot data (for DTE calculation)
        self._trading_dates = sorted(spot_1m["date"].unique())

        # Resample to 3-min
        spot_1m = spot_1m.set_index("datetime")
        spot_3m = spot_1m.groupby("date").resample("3min").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna(subset=["open"]).reset_index(level="date", drop=True).reset_index()
        spot_3m["date"] = spot_3m["datetime"].dt.date
        logger.info(f"Spot 3m: {len(spot_3m):,} rows")

        # Compute Heikin-Ashi (per day to reset HA_Open)
        parts = []
        for _, day_group in spot_3m.groupby("date"):
            parts.append(compute_heikin_ashi(day_group))
        spot_3m = pd.concat(parts, ignore_index=True)

        # Detect HA alerts
        spot_3m["ha_body"] = (spot_3m["ha_close"] - spot_3m["ha_open"]).abs()
        spot_3m["regular_range"] = spot_3m["high"] - spot_3m["low"]
        spot_3m["is_ha_alert"] = (
            (spot_3m["ha_body"] < self.ha_body_threshold)
            & (spot_3m["regular_range"] > self.ha_range_threshold)
        )

        self._spot_3m = spot_3m

    def _load_and_prepare_options(self):
        """Load options 1-min (for SL/TP) and compute 3-min indicators per contract."""
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        logger.info("Loading options data...")
        self._options_1m = load_data(
            options_path, self.start_date, self.end_date, "weekly"
        )
        logger.info(f"Options 1m: {len(self._options_1m):,} rows")

        # Resample to 3-min per contract and compute NR7 + EMA
        logger.info("Resampling options to 3-min and computing indicators...")
        contract_cols = ["strike", "option_type", "expiry_date"]
        ema_short = EMA(name="ema_short", period=self.ema_short_period)
        ema_long = EMA(name="ema_long", period=self.ema_long_period)

        parts = []
        opt_indexed = self._options_1m.set_index("datetime")
        for keys, group in opt_indexed.groupby(contract_cols):
            strike, opt_type, expiry = keys
            group = group.sort_index()
            # Resample to 3-min within each day to avoid overnight gaps
            resampled = group.groupby("date").resample("3min").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }).dropna(subset=["open"])
            if resampled.empty:
                continue
            resampled = resampled.reset_index(level="date", drop=True).reset_index()
            resampled["date"] = resampled["datetime"].dt.date

            # NR7
            resampled["nr7"] = compute_nr7(resampled, lookback=self.nr7_lookback)

            # EMA (per contract — independent history)
            resampled["ema_short"] = ema_short.calculate(resampled["close"])
            resampled["ema_long"] = ema_long.calculate(resampled["close"])

            resampled["strike"] = strike
            resampled["option_type"] = opt_type
            resampled["expiry_date"] = expiry
            parts.append(resampled)

        self._options_3m = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        logger.info(f"Options 3m: {len(self._options_3m):,} rows")

    def _prepare_data(self):
        """Full data pipeline."""
        self._load_and_prepare_spot()
        self._load_and_prepare_options()

    def _get_trading_dte(self, trade_date) -> int:
        """Count trading days from trade_date (exclusive) to nearest weekly expiry (inclusive)."""
        expiry = get_nearest_weekly_expiry(trade_date)
        if expiry is None:
            return 0
        return sum(1 for d in self._trading_dates if trade_date < d <= expiry)
```

- [ ] **Step 3: Verify the data pipeline compiles**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "from engine.ha_nr7_backtest import HaNr7BacktestEngine; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

Suggested message: `feat: add HA-NR7 engine scaffold with data pipeline`

---

### Task 4: Core Engine Loop — Alert Detection, NR7 Entry, SL/TP Exits

**Files:**
- Modify: `engine/ha_nr7_backtest.py` (add _process_day and run methods)

This is the core task. The `_process_day` method implements the state machine with all entry/exit logic for a single day.

- [ ] **Step 1: Add _process_day method with state machine**

Add to the `HaNr7BacktestEngine` class in `engine/ha_nr7_backtest.py`:

```python
    def _process_day(self, trading_date) -> List[HaNr7Trade]:
        """Process a single trading day. Returns completed trades."""
        day_spot_3m = self._spot_3m[self._spot_3m["date"] == trading_date].sort_values("datetime")
        if day_spot_3m.empty:
            return []

        expiry_date = get_nearest_weekly_expiry(trading_date)
        if expiry_date is None:
            return []

        dte = self._get_trading_dte(trading_date)

        day_opt_1m = self._options_1m[self._options_1m["date"] == trading_date]
        day_opt_3m = self._options_3m[self._options_3m["date"] == trading_date]
        if day_opt_1m.empty or day_opt_3m.empty:
            return []

        # Build lookups
        # opt_1m: datetime → {(strike, option_type): Series}
        opt_1m_by_dt: Dict[pd.Timestamp, Dict[Tuple, pd.Series]] = {}
        for _, row in day_opt_1m.iterrows():
            key = (int(row["strike"]), row["option_type"])
            opt_1m_by_dt.setdefault(row["datetime"], {})[key] = row

        # opt_3m: datetime → {(strike, option_type): Series}
        opt_3m_by_dt: Dict[pd.Timestamp, Dict[Tuple, pd.Series]] = {}
        for _, row in day_opt_3m.iterrows():
            key = (int(row["strike"]), row["option_type"])
            opt_3m_by_dt.setdefault(row["datetime"], {})[key] = row

        # spot_3m: datetime → Series
        spot_3m_by_dt = {row["datetime"]: row for _, row in day_spot_3m.iterrows()}

        # Set of 3-min candle datetimes → boundary = candle_time + 3min
        boundary_times = set()
        for dt_3m in spot_3m_by_dt:
            boundary_times.add(dt_3m + pd.Timedelta(minutes=3))

        # All 1-min timestamps for the day, sorted
        all_1m_times = sorted(opt_1m_by_dt.keys())

        # Time thresholds
        t_start = pd.Timestamp(f"{trading_date} {self.trading_start}", tz="Asia/Kolkata")
        t_last_entry = pd.Timestamp(f"{trading_date} {self.last_entry}", tz="Asia/Kolkata")
        t_force_exit = pd.Timestamp(f"{trading_date} {self.force_exit}", tz="Asia/Kolkata")

        # --- Day state ---
        state = EngineState.IDLE
        trades: List[HaNr7Trade] = []

        # Alert state
        alert_candle_time: Optional[pd.Timestamp] = None
        alert_ce_strike: int = 0
        alert_pe_strike: int = 0
        scan_remaining: int = 0

        # Position state
        position_option_type: str = ""
        position_strike: int = 0
        position_entry_prices: List[float] = []
        position_avg_entry: float = 0.0
        position_tp_pct: float = 0.0
        position_sl_pct: float = 0.0
        position_tp_level: float = 0.0
        position_sl_level: float = 0.0
        position_ema_adjusted: bool = False
        position_is_reversal: bool = False
        position_entry_time: str = ""
        reversal_count: int = 0

        # Pending entry from NR7 detection (fills at next candle open)
        pending_entry_type: Optional[str] = None  # "CE" or "PE"
        pending_reversal_close_price: Optional[float] = None
        pending_is_reversal: bool = False

        def _avg(prices: List[float]) -> float:
            return sum(prices) / len(prices) if prices else 0.0

        def _close_position(exit_price: float, exit_time: str, exit_reason: str):
            nonlocal state
            trade = HaNr7Trade(
                entry_date=str(trading_date),
                alert_candle_time=str(alert_candle_time),
                entry_time=position_entry_time,
                exit_time=exit_time,
                option_type=position_option_type,
                strike=position_strike,
                entry_prices=str(position_entry_prices),
                avg_entry=position_avg_entry,
                num_lots=len(position_entry_prices),
                exit_price=exit_price,
                exit_reason=exit_reason,
                tp_pct=position_tp_pct,
                sl_pct=position_sl_pct,
                dte=dte,
                ema_adjusted=position_ema_adjusted,
                is_reversal=position_is_reversal,
                pnl_points=exit_price - position_avg_entry,
                pnl_inr=(exit_price - position_avg_entry) * len(position_entry_prices) * self.lot_size,
            )
            trades.append(trade)

        def _enter_position(option_type: str, entry_price: float, entry_time_str: str,
                            is_reversal: bool, ema_s: float, ema_l: float):
            nonlocal state, position_option_type, position_strike, position_entry_prices
            nonlocal position_avg_entry, position_tp_pct, position_sl_pct
            nonlocal position_tp_level, position_sl_level, position_ema_adjusted
            nonlocal position_is_reversal, position_entry_time

            position_option_type = option_type
            position_strike = alert_ce_strike if option_type == "CE" else alert_pe_strike
            position_entry_prices = [entry_price]
            position_avg_entry = entry_price
            position_is_reversal = is_reversal
            position_entry_time = entry_time_str

            base_tp, base_sl = get_dte_tp_sl(dte)
            tp_adj, was_adjusted = adjust_tp_for_ema(base_tp, entry_price, ema_s, ema_l)
            position_tp_pct = tp_adj
            position_sl_pct = base_sl
            position_ema_adjusted = was_adjusted
            position_tp_level = entry_price * (1 + tp_adj / 100)
            position_sl_level = entry_price * (1 - base_sl / 100)
            state = EngineState.POSITION_OPEN

        def _add_lot(entry_price: float):
            nonlocal position_entry_prices, position_avg_entry
            nonlocal position_tp_level, position_sl_level
            position_entry_prices.append(entry_price)
            position_avg_entry = _avg(position_entry_prices)
            position_tp_level = position_avg_entry * (1 + position_tp_pct / 100)
            position_sl_level = position_avg_entry * (1 - position_sl_pct / 100)

        # ============================================================
        # Main 1-min loop
        # ============================================================
        for t in all_1m_times:
            if t < t_start:
                continue
            if state == EngineState.DAY_STOPPED:
                break

            time_str = t.strftime("%Y-%m-%d %H:%M")
            is_boundary = t in boundary_times
            candle_time_3m = t - pd.Timedelta(minutes=3) if is_boundary else None
            can_enter = t <= t_last_entry

            # --- Fill pending entry ---
            if pending_entry_type is not None:
                opt_type = pending_entry_type
                strike = alert_ce_strike if opt_type == "CE" else alert_pe_strike
                opt_row = opt_1m_by_dt.get(t, {}).get((strike, opt_type))
                if opt_row is not None and can_enter:
                    entry_price = opt_row["open"]
                    # Get EMA from the candle that triggered the NR7
                    ema_candle_time = t - pd.Timedelta(minutes=3)
                    ema_row = opt_3m_by_dt.get(ema_candle_time, {}).get((strike, opt_type))
                    ema_s = ema_row["ema_short"] if ema_row is not None and pd.notna(ema_row.get("ema_short")) else entry_price
                    ema_l = ema_row["ema_long"] if ema_row is not None and pd.notna(ema_row.get("ema_long")) else entry_price

                    if pending_is_reversal and pending_reversal_close_price is not None:
                        # Close old position at the reversal candle's close
                        _close_position(pending_reversal_close_price, time_str, "REVERSAL")

                    _enter_position(opt_type, entry_price, time_str,
                                    is_reversal=pending_is_reversal, ema_s=ema_s, ema_l=ema_l)
                pending_entry_type = None
                pending_reversal_close_price = None
                pending_is_reversal = False

            # --- SL/TP check (every 1-min bar, while in position) ---
            if state == EngineState.POSITION_OPEN:
                opt_row = opt_1m_by_dt.get(t, {}).get(
                    (position_strike, position_option_type)
                )

                if opt_row is not None:
                    # SL check (priority over TP)
                    if opt_row["low"] <= position_sl_level:
                        exit_reason = "SL"
                        _close_position(position_sl_level, time_str, exit_reason)
                        if position_is_reversal:
                            state = EngineState.DAY_STOPPED
                        else:
                            state = EngineState.IDLE
                        continue

                    # TP check
                    if opt_row["high"] >= position_tp_level:
                        _close_position(position_tp_level, time_str, "TP")
                        state = EngineState.IDLE
                        continue

                # EOD force exit
                if t >= t_force_exit:
                    if opt_row is not None:
                        _close_position(opt_row["close"], time_str, "EOD")
                    state = EngineState.IDLE
                    continue

            # --- 3-min boundary checks ---
            if is_boundary and candle_time_3m is not None:

                # POSITION_OPEN: check for pyramid / reversal NR7
                if state == EngineState.POSITION_OPEN and can_enter:
                    ce_strike = alert_ce_strike
                    pe_strike = alert_pe_strike
                    ce_row = opt_3m_by_dt.get(candle_time_3m, {}).get((ce_strike, "CE"))
                    pe_row = opt_3m_by_dt.get(candle_time_3m, {}).get((pe_strike, "PE"))
                    ce_nr7 = bool(ce_row["nr7"]) if ce_row is not None and pd.notna(ce_row.get("nr7")) else False
                    pe_nr7 = bool(pe_row["nr7"]) if pe_row is not None and pd.notna(pe_row.get("nr7")) else False

                    same_side_nr7 = (ce_nr7 if position_option_type == "CE" else pe_nr7)
                    opp_side_nr7 = (pe_nr7 if position_option_type == "CE" else ce_nr7)
                    opp_type = "PE" if position_option_type == "CE" else "CE"

                    if opp_side_nr7 and not same_side_nr7:
                        # Reversal trigger
                        if reversal_count >= 1:
                            # 2nd reversal → close and day stop
                            opp_strike = ce_strike if opp_type == "CE" else pe_strike
                            close_row = opt_3m_by_dt.get(candle_time_3m, {}).get(
                                (position_strike, position_option_type)
                            )
                            close_price = close_row["close"] if close_row is not None else position_avg_entry
                            _close_position(close_price, time_str, "REVERSAL_STOP")
                            state = EngineState.DAY_STOPPED
                            continue
                        else:
                            # 1st reversal → close old, pend entry on new side
                            close_row = opt_3m_by_dt.get(candle_time_3m, {}).get(
                                (position_strike, position_option_type)
                            )
                            close_price = close_row["close"] if close_row is not None else position_avg_entry
                            pending_entry_type = opp_type
                            pending_reversal_close_price = close_price
                            pending_is_reversal = True
                            reversal_count += 1

                    elif same_side_nr7 and len(position_entry_prices) < 3:
                        # Pyramid: pend add-lot at next candle open
                        # We handle this directly at next boundary or next 1-min
                        # Actually, pyramid fills at next candle OPEN. We need to
                        # track this as a pending pyramid.
                        pass  # handled below

                    # Pyramid — add lot at this boundary if NR7 was on prev candle
                    # (check is at boundary, fill is at this bar's open)
                    # Since we process boundaries at T+3, the entry is at T+3 open = current bar
                    if same_side_nr7 and len(position_entry_prices) < 3 and not opp_side_nr7:
                        opt_row_now = opt_1m_by_dt.get(t, {}).get(
                            (position_strike, position_option_type)
                        )
                        if opt_row_now is not None:
                            _add_lot(opt_row_now["open"])

                # ALERT_ACTIVE: scan for NR7
                if state == EngineState.ALERT_ACTIVE:
                    # Check for new HA alert (replaces current)
                    spot_candle = spot_3m_by_dt.get(candle_time_3m)
                    if spot_candle is not None and spot_candle["is_ha_alert"]:
                        spot_close = spot_candle["close"]
                        alert_candle_time = candle_time_3m
                        alert_ce_strike = int(math.floor(spot_close / self.strike_rounding) * self.strike_rounding)
                        alert_pe_strike = int(math.ceil(spot_close / self.strike_rounding) * self.strike_rounding)
                        if alert_ce_strike == alert_pe_strike:
                            alert_pe_strike += self.strike_rounding
                        scan_remaining = self.nr7_scan_window
                    elif scan_remaining <= 0:
                        state = EngineState.IDLE
                        continue

                    if scan_remaining > 0:
                        ce_row = opt_3m_by_dt.get(candle_time_3m, {}).get((alert_ce_strike, "CE"))
                        pe_row = opt_3m_by_dt.get(candle_time_3m, {}).get((alert_pe_strike, "PE"))
                        ce_nr7 = bool(ce_row["nr7"]) if ce_row is not None and pd.notna(ce_row.get("nr7")) else False
                        pe_nr7 = bool(pe_row["nr7"]) if pe_row is not None and pd.notna(pe_row.get("nr7")) else False

                        if ce_nr7 and pe_nr7:
                            pass  # Both NR7 → skip
                        elif ce_nr7 and can_enter:
                            pending_entry_type = "CE"
                            pending_is_reversal = False
                            state = EngineState.POSITION_OPEN  # will be set in _enter_position
                        elif pe_nr7 and can_enter:
                            pending_entry_type = "PE"
                            pending_is_reversal = False
                            state = EngineState.POSITION_OPEN
                        scan_remaining -= 1

                # IDLE: check for HA alert
                if state == EngineState.IDLE:
                    spot_candle = spot_3m_by_dt.get(candle_time_3m)
                    if spot_candle is not None and spot_candle["is_ha_alert"]:
                        spot_close = spot_candle["close"]
                        alert_candle_time = candle_time_3m
                        alert_ce_strike = int(math.floor(spot_close / self.strike_rounding) * self.strike_rounding)
                        alert_pe_strike = int(math.ceil(spot_close / self.strike_rounding) * self.strike_rounding)
                        if alert_ce_strike == alert_pe_strike:
                            alert_pe_strike += self.strike_rounding
                        scan_remaining = self.nr7_scan_window
                        reversal_count = 0
                        state = EngineState.ALERT_ACTIVE

                        # Also check NR7 on the alert candle itself
                        ce_row = opt_3m_by_dt.get(candle_time_3m, {}).get((alert_ce_strike, "CE"))
                        pe_row = opt_3m_by_dt.get(candle_time_3m, {}).get((alert_pe_strike, "PE"))
                        ce_nr7 = bool(ce_row["nr7"]) if ce_row is not None and pd.notna(ce_row.get("nr7")) else False
                        pe_nr7 = bool(pe_row["nr7"]) if pe_row is not None and pd.notna(pe_row.get("nr7")) else False

                        if ce_nr7 and pe_nr7:
                            pass  # Both → skip
                        elif ce_nr7 and can_enter:
                            pending_entry_type = "CE"
                            pending_is_reversal = False
                        elif pe_nr7 and can_enter:
                            pending_entry_type = "PE"
                            pending_is_reversal = False
                        scan_remaining -= 1

        # End of day — force close any open position
        if state == EngineState.POSITION_OPEN and position_entry_prices:
            last_time = all_1m_times[-1] if all_1m_times else t_force_exit
            opt_row = opt_1m_by_dt.get(last_time, {}).get(
                (position_strike, position_option_type)
            )
            if opt_row is not None:
                _close_position(opt_row["close"], last_time.strftime("%Y-%m-%d %H:%M"), "EOD")

        return trades
```

- [ ] **Step 2: Add the run() method**

Add to the `HaNr7BacktestEngine` class:

```python
    def run(self, progress_callback=None) -> List[HaNr7Trade]:
        """Run the full backtest across all trading days."""
        self._prepare_data()
        all_trades: List[HaNr7Trade] = []

        # Get unique trading dates within the requested range
        start_d = pd.to_datetime(self.start_date).date()
        end_d = pd.to_datetime(self.end_date).date()
        trade_dates = [d for d in self._trading_dates if start_d <= d <= end_d]

        logger.info(f"Running backtest: {len(trade_dates)} trading days")

        for i, td in enumerate(trade_dates):
            day_trades = self._process_day(td)
            all_trades.extend(day_trades)

            if progress_callback:
                progress_callback(i, len(trade_dates), str(td))

        logger.info(f"Backtest complete: {len(all_trades)} trades")
        return all_trades
```

- [ ] **Step 3: Verify the engine compiles and can be instantiated**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "
from engine.ha_nr7_backtest import HaNr7BacktestEngine, EngineState
engine = HaNr7BacktestEngine('2025-01-01', '2025-01-31')
print(f'Engine created: {engine.instrument}, lot_size={engine.lot_size}')
print(f'States: {[s.value for s in EngineState]}')
"`
Expected: `Engine created: NIFTY, lot_size=65` and `States: ['IDLE', 'ALERT_ACTIVE', 'POSITION_OPEN', 'DAY_STOPPED']`

- [ ] **Step 4: Commit**

Suggested message: `feat: add HA-NR7 core engine loop with state machine, entry, exit, pyramiding, reversal`

---

### Task 5: Fix State Transition Bugs + Edge Case Handling

**Files:**
- Modify: `engine/ha_nr7_backtest.py`

After Task 4, there are a few state transition issues to fix from the initial implementation:

- [ ] **Step 1: Fix state transition on NR7 entry detection**

In `_process_day`, when NR7 is detected during ALERT_ACTIVE or IDLE scan, the state should NOT immediately change to POSITION_OPEN. It should stay in ALERT_ACTIVE until the pending entry actually fills at the next bar. Fix the NR7 detection blocks:

In the ALERT_ACTIVE NR7 scan block, replace:
```python
                        elif ce_nr7 and can_enter:
                            pending_entry_type = "CE"
                            pending_is_reversal = False
                            state = EngineState.POSITION_OPEN  # will be set in _enter_position
                        elif pe_nr7 and can_enter:
                            pending_entry_type = "PE"
                            pending_is_reversal = False
                            state = EngineState.POSITION_OPEN
```
with:
```python
                        elif ce_nr7 and can_enter:
                            pending_entry_type = "CE"
                            pending_is_reversal = False
                            # State transitions to POSITION_OPEN in _enter_position on next bar
                        elif pe_nr7 and can_enter:
                            pending_entry_type = "PE"
                            pending_is_reversal = False
```

The `_enter_position` helper already sets `state = EngineState.POSITION_OPEN`.

- [ ] **Step 2: Handle edge case — spot exactly on strike boundary**

In the alert ITM strike calculation, when `floor(spot/rounding) == ceil(spot/rounding)` (spot exactly on a 100 boundary), we need CE to be 1 strike deeper ITM. The check `if alert_ce_strike == alert_pe_strike: alert_pe_strike += self.strike_rounding` exists but is wrong — CE should go lower:

Replace in both IDLE and ALERT_ACTIVE blocks:
```python
                        if alert_ce_strike == alert_pe_strike:
                            alert_pe_strike += self.strike_rounding
```
with:
```python
                        if alert_ce_strike == alert_pe_strike:
                            alert_ce_strike -= self.strike_rounding
```

This way: spot = 23900 → CE = 23800 (ITM), PE = 23900 (ATM→ITM since PE ITM = strike > spot, but here strike == spot). Actually per the spec, `floor(23900/100)*100 = 23900` and `ceil(23900/100)*100 = 23900`. Both equal. CE should be strictly ITM (< spot), so CE = 23800. PE at 23900 is ATM for PE, not ITM. So PE should be 24000.

Fix to:
```python
                        if alert_ce_strike == alert_pe_strike:
                            alert_ce_strike -= self.strike_rounding
                            alert_pe_strike += self.strike_rounding
```

- [ ] **Step 3: Run all tests to verify nothing broke**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -m pytest tests/test_ha_nr7.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

Suggested message: `fix: correct state transitions and strike boundary edge case in HA-NR7 engine`

---

### Task 6: Streamlit UI Runner

**Files:**
- Create: `ui/ha_nr7_backtest_runner.py`

- [ ] **Step 1: Create the UI runner**

```python
# ui/ha_nr7_backtest_runner.py
"""
HA-NR7 Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.ha_nr7_backtest import HaNr7BacktestEngine, trades_to_dataframe


def render_ha_nr7_backtest():
    st.header("HA-NR7 Strategy")
    st.caption(
        "Heikin-Ashi alert on spot → NR7 entry on ITM option → "
        "pyramiding / reversal → DTE-based TP/SL with EMA adjustment"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range & Instrument**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-01").date(), key="hanr7_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-04-02").date(), key="hanr7_end"
            )
            instrument = st.selectbox(
                "Instrument", options=["NIFTY"], index=0, key="hanr7_inst"
            )
            strike_rounding = st.number_input(
                "ITM strike rounding", value=100, step=50, min_value=50, key="hanr7_sr"
            )

        with col2:
            st.markdown("**HA Alert (Spot)**")
            ha_body = st.number_input(
                "HA body threshold (pts)", value=2.5, step=0.5, min_value=0.1, key="hanr7_hab"
            )
            ha_range = st.number_input(
                "Regular range threshold (pts)", value=20.0, step=1.0, min_value=1.0, key="hanr7_har"
            )
            st.markdown("**NR7 (Option)**")
            nr7_lookback = st.number_input(
                "NR7 lookback", value=7, step=1, min_value=2, key="hanr7_nr7lb"
            )
            nr7_window = st.number_input(
                "NR7 scan window (candles)", value=5, step=1, min_value=1, key="hanr7_nr7w"
            )

        with col3:
            st.markdown("**EMA (Option)**")
            ema_short = st.number_input(
                "EMA short period", value=10, step=1, min_value=2, key="hanr7_emas"
            )
            ema_long = st.number_input(
                "EMA long period", value=21, step=1, min_value=2, key="hanr7_emal"
            )

    with st.expander("Session Timing", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            trading_start = st.text_input(
                "Trading start", value="09:30", key="hanr7_t_start"
            )
        with tc2:
            last_entry = st.text_input(
                "Last entry", value="14:45", key="hanr7_t_last"
            )
        with tc3:
            force_exit = st.text_input(
                "Force exit (EOD)", value="14:55", key="hanr7_t_eod"
            )

    with st.expander("DTE TP/SL Table (reference)", expanded=False):
        st.markdown("""
        | Trading DTE | Base TP | Base SL | Above Both EMAs | Below Both EMAs |
        |---|---|---|---|---|
        | 4+ | 5% | 7.5% | 5% | 10% |
        | 3 | 7.5% | 7.5% | 5% | 10% |
        | 2 | 10% | 10% | 5% | 10% |
        | 1 | 12.5% | 12.5% | 5% | 12.5% |
        | 0 (expiry) | 15% | 15% | 5% | 15% |
        """)

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="hanr7_run"):
        engine = HaNr7BacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            instrument=instrument,
            strike_rounding=int(strike_rounding),
            ha_body_threshold=float(ha_body),
            ha_range_threshold=float(ha_range),
            nr7_lookback=int(nr7_lookback),
            nr7_scan_window=int(nr7_window),
            ema_short_period=int(ema_short),
            ema_long_period=int(ema_long),
            trading_start=trading_start,
            last_entry=last_entry,
            force_exit=force_exit,
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

        st.session_state["hanr7_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "hanr7_results" in st.session_state:
        _show_results(st.session_state["hanr7_results"])


# ----------------------------------------------------------------
# Results display
# ----------------------------------------------------------------


def _show_results(df: pd.DataFrame):
    st.divider()
    st.subheader("Results")

    total = len(df)
    wins = int((df["pnl_inr"] > 0).sum())
    losses = int((df["pnl_inr"] <= 0).sum())
    win_rate = wins / total * 100 if total > 0 else 0
    total_pnl = df["pnl_inr"].sum()
    avg_pnl = df["pnl_inr"].mean()

    # Row 1: Key metrics
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Trades", total)
    c2.metric("Wins", wins)
    c3.metric("Losses", losses)
    c4.metric("Win Rate", f"{win_rate:.1f}%")
    c5.metric("Total P&L", f"\u20b9{total_pnl:,.0f}")
    c6.metric("Avg P&L / Trade", f"\u20b9{avg_pnl:,.0f}")

    # Row 2: Exit reason breakdown
    reasons = df["exit_reason"].value_counts()
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("TP exits", int(reasons.get("TP", 0)))
    r2.metric("SL exits", int(reasons.get("SL", 0)))
    r3.metric("EOD exits", int(reasons.get("EOD", 0)))
    r4.metric("Reversal exits", int(reasons.get("REVERSAL", 0)))
    r5.metric("Reversal Stop", int(reasons.get("REVERSAL_STOP", 0)))

    # Row 3: CE vs PE
    ce_trades = df[df["option_type"] == "CE"]
    pe_trades = df[df["option_type"] == "PE"]
    d1, d2 = st.columns(2)
    with d1:
        ce_wr = (
            (ce_trades["pnl_inr"] > 0).mean() * 100 if len(ce_trades) > 0 else 0
        )
        st.markdown(
            f"**CE trades:** {len(ce_trades)}  |  "
            f"Win rate: {ce_wr:.1f}%  |  "
            f"P&L: \u20b9{ce_trades['pnl_inr'].sum():,.0f}"
        )
    with d2:
        pe_wr = (
            (pe_trades["pnl_inr"] > 0).mean() * 100 if len(pe_trades) > 0 else 0
        )
        st.markdown(
            f"**PE trades:** {len(pe_trades)}  |  "
            f"Win rate: {pe_wr:.1f}%  |  "
            f"P&L: \u20b9{pe_trades['pnl_inr'].sum():,.0f}"
        )

    # Row 4: Pyramiding and reversal stats
    pyramid_trades = df[df["num_lots"] > 1]
    reversal_trades = df[df["is_reversal"] == True]
    ema_adj_trades = df[df["ema_adjusted"] == True]
    st.markdown(
        f"**Pyramided trades:** {len(pyramid_trades)} of {total}  |  "
        f"**Reversals:** {len(reversal_trades)}  |  "
        f"**EMA-adjusted TP:** {len(ema_adj_trades)}"
    )

    # Row 5: DTE breakdown
    st.divider()
    st.subheader("P&L by DTE")
    dte_summary = df.groupby("dte").agg(
        trades=("pnl_inr", "count"),
        total_pnl=("pnl_inr", "sum"),
        win_rate=("pnl_inr", lambda x: (x > 0).mean() * 100),
    ).reset_index()
    st.dataframe(dte_summary, use_container_width=True, hide_index=True)

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

    # All trades table
    st.divider()
    st.subheader("All Trades")

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        filter_type = st.multiselect(
            "Filter by option type", options=["CE", "PE"], key="hanr7_filter_type"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason",
            options=["TP", "SL", "EOD", "REVERSAL", "REVERSAL_STOP"],
            key="hanr7_filter_reason",
        )
    with fc3:
        filter_date = st.text_input(
            "Filter by date (YYYY-MM-DD)", value="", key="hanr7_filter_date"
        )

    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]
    if filter_date:
        filtered = filtered[filtered["entry_date"] == filter_date]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="ha_nr7_strategy_backtest.csv",
        mime="text/csv",
        key="hanr7_download",
    )
```

- [ ] **Step 2: Verify the UI module imports correctly**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "from ui.ha_nr7_backtest_runner import render_ha_nr7_backtest; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

Suggested message: `feat: add Streamlit UI runner for HA-NR7 strategy`

---

### Task 7: Register Tab in app.py

**Files:**
- Modify: `app.py` (lines 14, 29, 46-61, add tab rendering)

- [ ] **Step 1: Add import**

At `app.py:29`, after the PRT import, add:
```python
from ui.ha_nr7_backtest_runner import render_ha_nr7_backtest
```

- [ ] **Step 2: Add tab to the tabs list**

Modify the `st.tabs()` call at `app.py:46` to include the new tab. Add `tab_ha_nr7` to the variable list and `"🎲 HA-NR7"` to the labels list:

```python
tab_dashboard, tab_trades, tab_backtest, tab_forward, tab_dema_st, tab_st_ema, tab_straddle_vwap, tab_boom, tab_boom_st, tab_vwap_ema_rsi, tab_bb_reversal, tab_bb_reversal_pine, tab_bb_reversal_pine_exit, tab_prt, tab_ha_nr7 = st.tabs([
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
])
```

- [ ] **Step 3: Add tab rendering**

After `app.py:103` (the PRT tab rendering), add:
```python
with tab_ha_nr7:
    render_ha_nr7_backtest()
```

- [ ] **Step 4: Verify the app loads**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "
import importlib
import sys
# Just verify imports work, don't start streamlit
sys.modules['streamlit'] = type(sys)('mock_st')
from ui.ha_nr7_backtest_runner import render_ha_nr7_backtest
print('Import OK')
"`
Expected: `Import OK` (or a clean error about streamlit mock, not about missing modules)

- [ ] **Step 5: Commit**

Suggested message: `feat: register HA-NR7 strategy tab in app.py`

---

### Task 8: Smoke Test + Final Verification

**Files:**
- Modify: `tests/test_ha_nr7.py` (add integration test)

- [ ] **Step 1: Add a smoke test with synthetic data**

Append to `tests/test_ha_nr7.py`:

```python
from engine.ha_nr7_backtest import (
    HaNr7BacktestEngine, HaNr7Trade, EngineState,
    trades_to_dataframe,
)


class TestTradeDataclass:
    def test_trade_to_dict(self):
        """Trade dataclass can be converted to dict for DataFrame."""
        trade = HaNr7Trade(
            entry_date="2025-03-10",
            alert_candle_time="2025-03-10 10:21",
            entry_time="2025-03-10 10:24",
            exit_time="2025-03-10 11:15",
            option_type="CE",
            strike=23400,
            entry_prices="[180.0]",
            avg_entry=180.0,
            num_lots=1,
            exit_price=189.0,
            exit_reason="TP",
            tp_pct=5.0,
            sl_pct=12.5,
            dte=1,
            ema_adjusted=True,
            is_reversal=False,
            pnl_points=9.0,
            pnl_inr=9.0 * 1 * 65,
        )
        df = trades_to_dataframe([trade])
        assert len(df) == 1
        assert df.iloc[0]["option_type"] == "CE"
        assert df.iloc[0]["pnl_inr"] == 585.0

    def test_empty_trades(self):
        df = trades_to_dataframe([])
        assert len(df) == 0


class TestEngineInit:
    def test_default_params(self):
        engine = HaNr7BacktestEngine("2025-01-01", "2025-01-31")
        assert engine.instrument == "NIFTY"
        assert engine.strike_rounding == 100
        assert engine.ha_body_threshold == 2.5
        assert engine.ha_range_threshold == 20.0
        assert engine.nr7_lookback == 7
        assert engine.nr7_scan_window == 5
        assert engine.lot_size == 65

    def test_custom_params(self):
        engine = HaNr7BacktestEngine(
            "2025-01-01", "2025-03-31",
            instrument="NIFTY",
            strike_rounding=200,
            ha_body_threshold=3.0,
        )
        assert engine.strike_rounding == 200
        assert engine.ha_body_threshold == 3.0
```

- [ ] **Step 2: Run all tests**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -m pytest tests/test_ha_nr7.py -v`
Expected: All tests PASS

- [ ] **Step 3: Run the backtest on a small date range (requires data)**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "
from engine.ha_nr7_backtest import HaNr7BacktestEngine, trades_to_dataframe
engine = HaNr7BacktestEngine('2025-03-01', '2025-03-07')
trades = engine.run()
df = trades_to_dataframe(trades)
print(f'Trades: {len(trades)}')
if len(df) > 0:
    print(df[['entry_date','option_type','strike','avg_entry','exit_price','exit_reason','num_lots','pnl_inr']].to_string())
else:
    print('No trades (check if data exists for this date range)')
"`
Expected: Either trades printed or "No trades" message. No errors.

- [ ] **Step 4: Commit**

Suggested message: `test: add smoke tests and integration verification for HA-NR7 strategy`

---

## Task Summary

| Task | Description | Files |
|---|---|---|
| 1 | Heikin-Ashi indicator | `indicators/heikin_ashi.py`, `tests/test_ha_nr7.py` |
| 2 | NR7, DTE, EMA adjustment helpers | `engine/ha_nr7_backtest.py`, `tests/test_ha_nr7.py` |
| 3 | Trade dataclass + engine scaffold + data pipeline | `engine/ha_nr7_backtest.py` |
| 4 | Core engine loop — state machine, entry, SL/TP, pyramid, reversal | `engine/ha_nr7_backtest.py` |
| 5 | Fix state transitions + edge cases | `engine/ha_nr7_backtest.py` |
| 6 | Streamlit UI runner | `ui/ha_nr7_backtest_runner.py` |
| 7 | Register tab in app.py | `app.py` |
| 8 | Smoke tests + final verification | `tests/test_ha_nr7.py` |
