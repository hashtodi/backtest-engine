# Supertrend + EMA Pullback Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a new backtest engine that trades NIFTY ATM weekly options using Supertrend bias, EMA6/EMA12 momentum confirmation, and EMA12 limit-order pullback entries — with spot-point-based TP/SL from same-day swing highs/lows.

**Architecture:** Two files: `engine/st_ema_backtest.py` (engine class + trade dataclass) and `ui/st_ema_backtest_runner.py` (Streamlit UI). Engine follows the same pattern as `engine/dema_st_backtest.py` — load spot 1-min, resample to 5-min, calculate indicators, forward-fill to 1-min, then loop minute-by-minute. Key difference: TP/SL are spot-point levels (not premium %), multiple trades per day allowed on ST flips, and entry is a limit fill at EMA12 value.

**Tech Stack:** Python, pandas, numpy, Streamlit, existing indicator classes (SuperTrend, EMA), existing data_loader and config utilities.

**Spec:** `docs/superpowers/specs/2026-04-06-supertrend-ema-pullback-engine-design.md`

---

### Task 1: Engine — Trade Dataclass and Scaffolding

**Files:**
- Create: `engine/st_ema_backtest.py`

- [ ] **Step 1: Create the engine file with imports, dataclass, and empty engine class**

```python
"""
Supertrend + EMA Pullback Backtest Engine.

Strategy:
  Bias (5-min):
    - Supertrend(12,3) bullish (direction == -1) → LONG bias
    - Supertrend(12,3) bearish (direction == +1) → SHORT bias

  Readiness (5-min, per bar):
    - LONG: EMA6 > EMA12 AND swing_high - EMA12 >= min_target
    - SHORT: EMA6 < EMA12 AND EMA12 - swing_low >= min_target

  Entry (1-min, within each 5-min window):
    - LONG: spot low <= EMA12 → limit fill at EMA12 value
    - SHORT: spot high >= EMA12 → limit fill at EMA12 value
    - ATM strike = round(EMA12 / 50) * 50, nearest weekly expiry

  Exit (1-min spot, priority order):
    1. SL: spot breaches entry ± (tp_distance / rr_ratio)
    2. TP: spot reaches swing high (long) / swing low (short)
    3. SuperTrend flip → exit + flip bias (re-entry allowed same day)
    4. EOD force exit at 15:00

  All indicators on 5-min resampled spot, forward-filled to 1-min.
  TP/SL checked against spot price; P&L from option premiums.
"""

import logging
import os
from dataclasses import asdict, dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from config import (
    DATA_PATH,
    LOT_SIZE,
    SPOT_DATA_PATH,
    STRIKE_ROUNDING,
    get_nearest_weekly_expiry,
)
from engine.data_loader import load_data
from indicators.ema import EMA
from indicators.supertrend import SuperTrend

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------

@dataclass
class StEmaTrade:
    date: str                    # "YYYY-MM-DD"
    option_type: str             # "CE" / "PE"
    strike: float
    expiry_date: str

    # Indicators at entry
    supertrend_dir: float        # -1=bullish, +1=bearish
    supertrend_val: float
    ema_short: float             # EMA6 value
    ema_long: float              # EMA12 value
    spot_at_entry: float         # EMA12 value (limit fill price)

    # Levels
    tp_level: float              # swing high/low in spot
    sl_level: float              # entry ± (tp_distance / rr)
    tp_distance: float           # |swing - ema12|
    sl_distance: float           # tp_distance / rr_ratio

    # Times
    signal_time: str             # "HH:MM" when Supertrend set bias
    ready_time: str              # "HH:MM" when Stage 2 conditions met
    touch_time: str              # "HH:MM" when 1-min candle touched EMA12
    entry_time: str              # same as touch_time (limit fill)
    entry_price: float           # option close at touch candle
    qty: int

    exit_time: str
    exit_price: float
    exit_reason: str             # "TP" / "SL" / "ST_FLIP" / "EOD"

    pnl_points: float            # exit_price - entry_price
    pnl_pct: float
    pnl_inr: float               # pnl_points * qty


def trades_to_dataframe(trades: List[StEmaTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
```

- [ ] **Step 2: Verify file is importable**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "from engine.st_ema_backtest import StEmaTrade, trades_to_dataframe; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

Suggested message: `feat(engine): add StEmaTrade dataclass and scaffolding for ST+EMA pullback engine`

---

### Task 2: Engine — Data Loading and Indicator Calculation

**Files:**
- Modify: `engine/st_ema_backtest.py`

- [ ] **Step 1: Add the engine class with __init__, data loading, resampling, and indicator calculation**

Append to `engine/st_ema_backtest.py` after the `trades_to_dataframe` function:

```python
# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class StEmaBacktestEngine:
    """Supertrend + EMA pullback strategy backtest engine."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        supertrend_period: int = 12,
        supertrend_factor: float = 3.0,
        ema_short_period: int = 6,
        ema_long_period: int = 12,
        min_target: float = 20.0,
        rr_ratio: float = 1.25,
        swing_lookback: int = 12,
        entry_start: str = "09:30",
        entry_end: str = "14:55",
        force_exit_time: str = "15:00",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.supertrend_period = supertrend_period
        self.supertrend_factor = supertrend_factor
        self.ema_short_period = ema_short_period
        self.ema_long_period = ema_long_period
        self.min_target = min_target
        self.rr_ratio = rr_ratio
        self.swing_lookback = swing_lookback
        self.entry_start = entry_start
        self.entry_end = entry_end
        self.force_exit_time = force_exit_time
        self.instrument = "NIFTY"
        self.lot_size = LOT_SIZE.get("NIFTY", 75)

        self._spot_1m: Optional[pd.DataFrame] = None
        self._spot_5m: Optional[pd.DataFrame] = None
        self._options_1m: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Data loading & preparation
    # ------------------------------------------------------------------

    def _load_spot(self) -> pd.DataFrame:
        """Load 1-min spot data with 30-day warmup for indicators."""
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
        warmup_start = start - pd.Timedelta(days=30)
        df = df[(df["datetime"] >= warmup_start) & (df["datetime"] < end)]
        return df.sort_values("datetime").reset_index(drop=True)

    def _resample_5min(self, spot_1m: pd.DataFrame) -> pd.DataFrame:
        """Resample 1-min spot OHLCV to 5-min, market hours only."""
        df = spot_1m.set_index("datetime").copy()
        df = df.between_time("09:15", "15:29")
        ohlcv = df.resample("5min").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna(subset=["close"])
        return ohlcv

    def _calculate_indicators(self, spot_5m: pd.DataFrame) -> pd.DataFrame:
        """Calculate Supertrend, EMA short, EMA long on 5-min data."""
        st_ind = SuperTrend(
            name="st",
            factor=self.supertrend_factor,
            atr_period=self.supertrend_period,
        )
        ema_s = EMA(name="ema_s", period=self.ema_short_period)
        ema_l = EMA(name="ema_l", period=self.ema_long_period)

        spot_5m = spot_5m.copy()
        st_result = st_ind.calculate(
            spot_5m["close"],
            high=spot_5m["high"],
            low=spot_5m["low"],
        )
        spot_5m["st_dir"] = st_result["direction"]
        spot_5m["st_val"] = st_result["value"]
        spot_5m["ema_s"] = ema_s.calculate(spot_5m["close"])
        spot_5m["ema_l"] = ema_l.calculate(spot_5m["close"])
        return spot_5m

    def _forward_fill_to_1m(
        self, spot_1m: pd.DataFrame, spot_5m: pd.DataFrame
    ) -> pd.DataFrame:
        """Forward-fill 5-min indicators onto 1-min candles.

        Shifts by +5min: the 09:15 bar's values become available at 09:20.
        """
        indicator_cols = ["st_dir", "st_val", "ema_s", "ema_l"]
        ind_5m = spot_5m[indicator_cols].copy()
        ind_5m.index = ind_5m.index + pd.Timedelta(minutes=5)

        spot_1m = spot_1m.copy()
        spot_1m_idx = spot_1m.set_index("datetime").index
        ind_1m = ind_5m.reindex(spot_1m_idx, method="ffill")

        for col in indicator_cols:
            spot_1m[col] = ind_1m[col].values
        return spot_1m

    def _prepare_data(self):
        """Full data pipeline: load, resample, calculate, merge."""
        logger.info("Loading spot data...")
        spot_1m = self._load_spot()
        logger.info(f"Spot 1m: {len(spot_1m):,} rows")

        logger.info("Resampling to 5-min...")
        spot_5m = self._resample_5min(spot_1m)
        logger.info(f"Spot 5m: {len(spot_5m):,} rows")

        logger.info("Calculating indicators on 5-min data...")
        spot_5m = self._calculate_indicators(spot_5m)

        logger.info("Forward-filling indicators to 1-min...")
        spot_1m = self._forward_fill_to_1m(spot_1m, spot_5m)

        # Trim warmup days
        start_dt = pd.to_datetime(self.start_date).date()
        spot_1m = spot_1m[spot_1m["date"] >= start_dt].reset_index(drop=True)
        self._spot_1m = spot_1m

        # Keep 5-min data (trimmed) for swing calculations
        self._spot_5m = spot_5m[spot_5m.index.date >= start_dt].copy()
        self._spot_5m["date"] = self._spot_5m.index.date

        logger.info("Loading options data...")
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        self._options_1m = load_data(
            options_path, self.start_date, self.end_date, "weekly"
        )
        logger.info(f"Options 1m: {len(self._options_1m):,} rows")
```

- [ ] **Step 2: Verify data loading compiles**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "from engine.st_ema_backtest import StEmaBacktestEngine; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

Suggested message: `feat(engine): add data loading and indicator pipeline for ST+EMA engine`

---

### Task 3: Engine — Core Backtest Loop (run + _process_day)

**Files:**
- Modify: `engine/st_ema_backtest.py`

This is the most complex task. The `_process_day` method handles:
- Tracking which 5-min window we're in and its EMA12/readiness state
- Minute-by-minute entry detection (limit fill at EMA12)
- Exit checks (SL → TP → ST flip → EOD) on spot levels
- Re-entry on ST flip (multiple trades per day)

- [ ] **Step 1: Add the `run` method and swing helper to the engine class**

Add these methods to `StEmaBacktestEngine`:

```python
    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[StEmaTrade]:
        """Run backtest. Returns list of StEmaTrade."""
        self._prepare_data()

        all_dates = sorted(self._spot_1m["date"].unique())
        trades: List[StEmaTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))

            day_trades = self._process_day(trading_date)
            trades.extend(day_trades)

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    def _get_swing_high_low(self, trading_date, current_5m_time) -> tuple:
        """Get swing high and swing low of last N same-day 5-min candles.

        Only uses candles from the current trading day, up to (but not including)
        the current 5-min bar (since current bar just closed and its values
        are what we're using for EMA — the swing should be from prior bars).

        Returns (swing_high, swing_low). Returns (NaN, NaN) if no candles.
        """
        day_5m = self._spot_5m[self._spot_5m["date"] == trading_date]
        # Only bars before current_5m_time (the bar whose indicators we're using)
        # current_5m_time is the availability time (bar_time + 5min),
        # so the bar itself is at current_5m_time - 5min.
        # We want all same-day bars up to and including that bar.
        bar_time = current_5m_time - pd.Timedelta(minutes=5)
        prior = day_5m[day_5m.index <= bar_time]
        if prior.empty:
            return np.nan, np.nan
        lookback = prior.tail(self.swing_lookback)
        return lookback["high"].max(), lookback["low"].min()
```

- [ ] **Step 2: Add the `_process_day` method**

Add this method to `StEmaBacktestEngine`:

```python
    def _process_day(self, trading_date) -> List[StEmaTrade]:
        """Process a single trading day. Returns 0 or more trades."""
        day_spot = self._spot_1m[self._spot_1m["date"] == trading_date]
        if day_spot.empty:
            return []

        expiry_date = get_nearest_weekly_expiry(trading_date)
        if expiry_date is None:
            return []

        # Day's options, pre-grouped by datetime
        day_options = self._options_1m[self._options_1m["date"] == trading_date]
        if day_options.empty:
            return []
        opt_by_dt = {dt: grp for dt, grp in day_options.groupby("datetime")}

        # --- State ---
        trades: List[StEmaTrade] = []
        in_position = False
        pending_flip_exit = False

        # Position state (set on entry, cleared on exit)
        entry_price = None       # option premium
        entry_spot = None        # EMA12 value (limit fill in spot)
        option_strike = None
        option_type = None       # "CE" / "PE"
        sl_level = None          # spot level
        tp_level = None          # spot level
        tp_distance = None
        sl_distance = None
        entry_time = None
        touch_time = None
        signal_time = None
        ready_time = None
        entry_indicators = {}

        # Bias state (persists across trades within the day)
        bias = None              # "LONG" / "SHORT" / None
        bias_signal_time = None  # when ST flipped to set this bias
        prev_st_dir = None

        # Track which 5-min window we're in
        current_5m_window_start = None  # e.g., "09:20" means we use 09:15 bar's indicators
        window_ready = False            # Stage 2 passed for this window
        window_entered = False          # already entered in this 5-min window

        for _, candle in day_spot.iterrows():
            t_str = candle["time_str"]
            t_dt = candle["datetime"]
            spot_close = candle["close"]
            spot_low = candle["low"]
            spot_high = candle["high"]
            st_dir = candle["st_dir"]
            st_val = candle["st_val"]
            ema_s = candle["ema_s"]
            ema_l = candle["ema_l"]

            # Skip if indicators not ready
            if pd.isna(st_dir) or pd.isna(ema_s) or pd.isna(ema_l):
                prev_st_dir = st_dir
                continue

            # Detect new 5-min window boundary
            # Indicators change at :X0 and :X5 minutes (forward-filled from 5-min bars)
            # A new window starts when ema_l changes or at each 5-min boundary
            minute = t_dt.minute
            is_5m_boundary = (minute % 5 == 0)
            if is_5m_boundary:
                new_window = t_dt
                if new_window != current_5m_window_start:
                    current_5m_window_start = new_window
                    window_entered = False
                    # Recalculate readiness for this window
                    swing_high, swing_low = self._get_swing_high_low(
                        trading_date, t_dt
                    )
                    window_ready = False

                    if bias == "LONG" and not pd.isna(swing_high):
                        _tp_dist = swing_high - ema_l
                        if ema_s > ema_l and _tp_dist >= self.min_target:
                            window_ready = True
                    elif bias == "SHORT" and not pd.isna(swing_low):
                        _tp_dist = ema_l - swing_low
                        if ema_s < ema_l and _tp_dist >= self.min_target:
                            window_ready = True

            # ============ 1. BIAS UPDATE (Supertrend flip detection) ============
            st_flipped = False
            if prev_st_dir is not None and not np.isnan(prev_st_dir) and st_dir != prev_st_dir:
                st_flipped = True
                if st_dir == -1:  # flipped bullish
                    bias = "LONG"
                else:             # flipped bearish
                    bias = "SHORT"
                bias_signal_time = t_str

                # If in position, ST flip triggers exit
                if in_position:
                    pending_flip_exit = True

            # ============ 2. PENDING FLIP EXIT ============
            if pending_flip_exit and in_position:
                minute_opts = opt_by_dt.get(t_dt)
                if minute_opts is not None:
                    opt_candle = minute_opts[
                        (minute_opts["strike"] == option_strike)
                        & (minute_opts["option_type"] == option_type)
                    ]
                    if not opt_candle.empty:
                        exit_price = round(opt_candle.iloc[0]["open"], 2)
                        trade = self._make_trade(
                            trading_date, option_type, option_strike,
                            expiry_date, entry_indicators,
                            tp_level, sl_level, tp_distance, sl_distance,
                            signal_time, ready_time, touch_time, entry_time,
                            entry_price, t_str, exit_price, "ST_FLIP",
                        )
                        trades.append(trade)
                        in_position = False
                        pending_flip_exit = False
                        # Reset window state so we can re-enter
                        window_entered = False

            # ============ 3. EXIT CHECKS (SL → TP → EOD) ============
            if in_position and not pending_flip_exit:
                # 3a. SL check on spot
                sl_hit = False
                if option_type == "CE" and spot_low <= sl_level:
                    sl_hit = True
                elif option_type == "PE" and spot_high >= sl_level:
                    sl_hit = True

                if sl_hit:
                    # Exit at option open of next candle — we detect on this candle,
                    # but since we need "next 1-min open", we flag it.
                    # For simplicity in backtest: use option close of this candle
                    # as proxy (the spec says next open, but within the loop
                    # we handle it by checking on the NEXT iteration).
                    # Actually, let's set a pending exit flag like ST flip.
                    # But to keep it simpler and match the existing pattern,
                    # we use the next candle. We'll use a pending mechanism.
                    pass  # handled below with pending_sl/tp

                # For SL and TP, we detect on this candle but exit at next candle's open.
                # To implement this cleanly, we check the levels and if hit,
                # record the exit on the CURRENT candle using a lookahead to next open.
                # But since we iterate row by row, we use the same pattern as
                # the existing DEMA-ST engine: check option price on this candle.
                #
                # Per spec: "Exit price: Option open of the next 1-min candle"
                # We'll flag pending exits and resolve them on the next iteration.

                # Actually, let's simplify: detect on this candle's spot,
                # set pending_sl_exit or pending_tp_exit, resolve next candle.
                pass

            # We need a cleaner approach. Let me restructure exits.
            prev_st_dir = st_dir

        return trades
```

**Wait — the exit logic above is getting muddled.** Let me rewrite `_process_day` cleanly with pending exit flags for SL/TP (since exit price = option open of NEXT 1-min candle).

Replace the entire `_process_day` method with this cleaner version:

```python
    def _process_day(self, trading_date) -> List[StEmaTrade]:
        """Process a single trading day. Returns 0 or more trades."""
        day_spot = self._spot_1m[self._spot_1m["date"] == trading_date]
        if day_spot.empty:
            return []

        expiry_date = get_nearest_weekly_expiry(trading_date)
        if expiry_date is None:
            return []

        day_options = self._options_1m[self._options_1m["date"] == trading_date]
        if day_options.empty:
            return []
        opt_by_dt = {dt: grp for dt, grp in day_options.groupby("datetime")}

        trades: List[StEmaTrade] = []

        # Position state
        in_position = False
        entry_price = None
        entry_spot = None
        option_strike = None
        option_type = None
        sl_level = None
        tp_level = None
        tp_distance = None
        sl_distance = None
        entry_time = None
        touch_time = None
        signal_time = None
        ready_time = None
        entry_indicators = {}

        # Pending exit: reason string or None
        pending_exit_reason = None

        # Bias
        bias = None
        bias_signal_time = None
        prev_st_dir = None

        # 5-min window tracking
        current_5m_window_start = None
        window_ready = False
        window_entered = False
        window_ema_l = None          # EMA12 for current window (limit price)
        window_swing_high = None
        window_swing_low = None

        for _, candle in day_spot.iterrows():
            t_str = candle["time_str"]
            t_dt = candle["datetime"]
            spot_close = candle["close"]
            spot_low = candle["low"]
            spot_high = candle["high"]
            st_dir = candle["st_dir"]
            st_val = candle["st_val"]
            ema_s = candle["ema_s"]
            ema_l = candle["ema_l"]

            if pd.isna(st_dir) or pd.isna(ema_s) or pd.isna(ema_l):
                prev_st_dir = st_dir
                continue

            # ============ 0. RESOLVE PENDING EXIT (at this candle's option open) ============
            if pending_exit_reason is not None and in_position:
                minute_opts = opt_by_dt.get(t_dt)
                if minute_opts is not None:
                    opt_candle = minute_opts[
                        (minute_opts["strike"] == option_strike)
                        & (minute_opts["option_type"] == option_type)
                    ]
                    if not opt_candle.empty:
                        exit_price = round(opt_candle.iloc[0]["open"], 2)
                        trade = self._make_trade(
                            trading_date, option_type, option_strike,
                            expiry_date, entry_indicators,
                            tp_level, sl_level, tp_distance, sl_distance,
                            signal_time, ready_time, touch_time, entry_time,
                            entry_price, t_str, exit_price, pending_exit_reason,
                        )
                        trades.append(trade)
                        in_position = False
                        pending_exit_reason = None
                        window_entered = False

            # ============ 1. SUPERTREND FLIP DETECTION ============
            if (prev_st_dir is not None
                    and not np.isnan(prev_st_dir)
                    and st_dir != prev_st_dir):
                # Update bias
                bias = "LONG" if st_dir == -1 else "SHORT"
                bias_signal_time = t_str

                # If in position, trigger exit at next candle
                if in_position and pending_exit_reason is None:
                    pending_exit_reason = "ST_FLIP"

            # ============ 2. EXIT CHECKS (only if in position, no pending exit) ============
            if in_position and pending_exit_reason is None:
                # 2a. SL hit (highest priority)
                if option_type == "CE" and spot_low <= sl_level:
                    pending_exit_reason = "SL"
                elif option_type == "PE" and spot_high >= sl_level:
                    pending_exit_reason = "SL"

                # 2b. TP hit
                if pending_exit_reason is None:
                    if option_type == "CE" and spot_high >= tp_level:
                        pending_exit_reason = "TP"
                    elif option_type == "PE" and spot_low <= tp_level:
                        pending_exit_reason = "TP"

                # 2c. EOD force exit (immediate, not deferred)
                if t_str >= self.force_exit_time:
                    minute_opts = opt_by_dt.get(t_dt)
                    if minute_opts is not None:
                        opt_candle = minute_opts[
                            (minute_opts["strike"] == option_strike)
                            & (minute_opts["option_type"] == option_type)
                        ]
                        if not opt_candle.empty:
                            exit_price = round(opt_candle.iloc[0]["close"], 2)
                            trade = self._make_trade(
                                trading_date, option_type, option_strike,
                                expiry_date, entry_indicators,
                                tp_level, sl_level, tp_distance, sl_distance,
                                signal_time, ready_time, touch_time, entry_time,
                                entry_price, t_str, exit_price, "EOD",
                            )
                            trades.append(trade)
                            in_position = False
                            pending_exit_reason = None
                            prev_st_dir = st_dir
                            continue

            # ============ 3. 5-MIN WINDOW UPDATE ============
            minute = t_dt.minute
            if minute % 5 == 0:
                new_window = t_dt
                if new_window != current_5m_window_start:
                    current_5m_window_start = new_window
                    window_ema_l = ema_l
                    if not in_position:
                        window_entered = False

                    swing_high, swing_low = self._get_swing_high_low(
                        trading_date, t_dt
                    )
                    window_swing_high = swing_high
                    window_swing_low = swing_low
                    window_ready = False

                    if bias == "LONG" and not pd.isna(swing_high):
                        _tp_dist = swing_high - ema_l
                        if ema_s > ema_l and _tp_dist >= self.min_target:
                            window_ready = True
                    elif bias == "SHORT" and not pd.isna(swing_low):
                        _tp_dist = ema_l - swing_low
                        if ema_s < ema_l and _tp_dist >= self.min_target:
                            window_ready = True

            # ============ 4. ENTRY DETECTION (1-min) ============
            if (not in_position
                    and pending_exit_reason is None
                    and window_ready
                    and not window_entered
                    and bias is not None
                    and t_str >= self.entry_start
                    and t_str <= self.entry_end
                    and window_ema_l is not None):

                touch = False
                if bias == "LONG" and spot_low <= window_ema_l:
                    touch = True
                    option_type = "CE"
                elif bias == "SHORT" and spot_high >= window_ema_l:
                    touch = True
                    option_type = "PE"

                if touch:
                    rounding = STRIKE_ROUNDING.get(self.instrument, 50)
                    strike = round(window_ema_l / rounding) * rounding

                    minute_opts = opt_by_dt.get(t_dt)
                    if minute_opts is not None:
                        opt_candle = minute_opts[
                            (minute_opts["strike"] == strike)
                            & (minute_opts["option_type"] == option_type)
                        ]
                        if not opt_candle.empty:
                            opt_row = opt_candle.iloc[0]
                            entry_price = round(opt_row["close"], 2)
                            entry_spot = window_ema_l
                            option_strike = strike

                            if bias == "LONG":
                                tp_distance = round(window_swing_high - window_ema_l, 2)
                                sl_distance = round(tp_distance / self.rr_ratio, 2)
                                tp_level = round(window_swing_high, 2)
                                sl_level = round(entry_spot - sl_distance, 2)
                            else:
                                tp_distance = round(window_ema_l - window_swing_low, 2)
                                sl_distance = round(tp_distance / self.rr_ratio, 2)
                                tp_level = round(window_swing_low, 2)
                                sl_level = round(entry_spot + sl_distance, 2)

                            signal_time = bias_signal_time
                            ready_time = current_5m_window_start.strftime("%H:%M") if current_5m_window_start else t_str
                            touch_time = t_str
                            entry_time = t_str

                            entry_indicators = {
                                "st_dir": float(st_dir),
                                "st_val": round(float(st_val), 2),
                                "ema_s": round(float(ema_s), 2),
                                "ema_l": round(float(ema_l), 2),
                                "spot": round(float(entry_spot), 2),
                            }

                            in_position = True
                            window_entered = True

                            logger.debug(
                                f"{trading_date} {t_str} ENTRY {option_type} "
                                f"strike={strike} premium={entry_price} "
                                f"TP_spot={tp_level} SL_spot={sl_level}"
                            )

            prev_st_dir = st_dir

        # Safety net: force close if still in position
        if in_position:
            exit_price, exit_time_str = self._last_option_price(
                day_options, option_strike, option_type
            )
            trade = self._make_trade(
                trading_date, option_type, option_strike,
                expiry_date, entry_indicators,
                tp_level, sl_level, tp_distance, sl_distance,
                signal_time, ready_time, touch_time, entry_time,
                entry_price, exit_time_str, exit_price, "EOD",
            )
            trades.append(trade)

        return trades
```

- [ ] **Step 3: Add helper methods `_make_trade` and `_last_option_price`**

Add these to the engine class:

```python
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_trade(
        self, trading_date, option_type, strike, expiry_date,
        indicators, tp_level, sl_level, tp_distance, sl_distance,
        signal_time, ready_time, touch_time, entry_time,
        entry_price, exit_time, exit_price, exit_reason,
    ) -> StEmaTrade:
        pnl_points = round(exit_price - entry_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        pnl_inr = round(pnl_points * self.lot_size, 2)
        return StEmaTrade(
            date=str(trading_date),
            option_type=option_type,
            strike=strike,
            expiry_date=str(expiry_date),
            supertrend_dir=indicators.get("st_dir", 0.0),
            supertrend_val=indicators.get("st_val", 0.0),
            ema_short=indicators.get("ema_s", 0.0),
            ema_long=indicators.get("ema_l", 0.0),
            spot_at_entry=indicators.get("spot", 0.0),
            tp_level=tp_level,
            sl_level=sl_level,
            tp_distance=tp_distance,
            sl_distance=sl_distance,
            signal_time=signal_time,
            ready_time=ready_time,
            touch_time=touch_time,
            entry_time=entry_time,
            entry_price=entry_price,
            qty=self.lot_size,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl_points=pnl_points,
            pnl_pct=pnl_pct,
            pnl_inr=pnl_inr,
        )

    @staticmethod
    def _last_option_price(day_options, strike, option_type):
        """Get last available option price for a contract on this day."""
        contract = day_options[
            (day_options["strike"] == strike)
            & (day_options["option_type"] == option_type)
        ]
        if contract.empty:
            return 0.0, "15:30"
        last = contract.iloc[-1]
        t = last["time_only"]
        time_str = t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)[:5]
        return round(last["close"], 2), time_str
```

- [ ] **Step 4: Verify full engine is importable**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "from engine.st_ema_backtest import StEmaBacktestEngine; e = StEmaBacktestEngine('2025-03-01', '2025-03-05'); print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

Suggested message: `feat(engine): implement core backtest loop for ST+EMA pullback engine`

---

### Task 4: UI Runner — Streamlit Page

**Files:**
- Create: `ui/st_ema_backtest_runner.py`

- [ ] **Step 1: Create the full UI runner**

```python
"""
Supertrend + EMA Pullback Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.st_ema_backtest import StEmaBacktestEngine, trades_to_dataframe


def render_st_ema_backtest():
    st.header("Supertrend + EMA Pullback")
    st.caption(
        "Supertrend bias → EMA6/12 momentum → EMA12 limit pullback entry → "
        "ATM weekly option  |  TP: swing high/low  |  SL: TP/RR ratio"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-20").date(), key="se_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-12").date(), key="se_end"
            )

        with col2:
            st.markdown("**Indicators (5-min spot)**")
            st_period = st.number_input(
                "SuperTrend ATR period", value=12, step=1, min_value=1, key="se_st_period"
            )
            st_factor = st.number_input(
                "SuperTrend factor", value=3.0, step=0.5, min_value=0.5, key="se_st_factor"
            )
            ema_short = st.number_input(
                "EMA short period", value=6, step=1, min_value=1, key="se_ema_short"
            )
            ema_long = st.number_input(
                "EMA long period (entry level)", value=12, step=1, min_value=1, key="se_ema_long"
            )

        with col3:
            st.markdown("**Exit / Target**")
            min_target = st.number_input(
                "Min target (pts)", value=20.0, step=5.0, min_value=1.0, key="se_min_target"
            )
            rr_ratio = st.number_input(
                "Risk:Reward ratio", value=1.25, step=0.05, min_value=0.1, key="se_rr"
            )
            swing_lookback = st.number_input(
                "Swing lookback (5-min bars)", value=12, step=1, min_value=1, key="se_swing"
            )

    with st.expander("Time Windows", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            entry_start = st.text_input("Entry window start", value="09:30", key="se_entry_start")
        with tc2:
            entry_end = st.text_input("Entry window end", value="14:55", key="se_entry_end")
        with tc3:
            force_exit = st.text_input("Force exit time", value="15:00", key="se_force_exit")

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="se_run"):
        engine = StEmaBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            supertrend_period=int(st_period),
            supertrend_factor=float(st_factor),
            ema_short_period=int(ema_short),
            ema_long_period=int(ema_long),
            min_target=float(min_target),
            rr_ratio=float(rr_ratio),
            swing_lookback=int(swing_lookback),
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

        st.session_state["se_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "se_results" in st.session_state:
        _show_results(st.session_state["se_results"])


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
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("ST Flip exits", int(reasons.get("ST_FLIP", 0)))
    r2.metric("SL exits", int(reasons.get("SL", 0)))
    r3.metric("TP exits", int(reasons.get("TP", 0)))
    r4.metric("EOD exits", int(reasons.get("EOD", 0)))

    # Avg P&L by exit reason
    st.markdown("**Avg P&L by Exit Reason:**")
    reason_stats = df.groupby("exit_reason")["pnl_inr"].agg(["count", "mean", "sum"])
    reason_stats.columns = ["Count", "Avg P&L", "Total P&L"]
    st.dataframe(reason_stats.style.format({"Avg P&L": "\u20b9{:,.0f}", "Total P&L": "\u20b9{:,.0f}"}))

    # CE vs PE
    ce_trades = df[df["option_type"] == "CE"]
    pe_trades = df[df["option_type"] == "PE"]
    d1, d2 = st.columns(2)
    with d1:
        ce_wr = (ce_trades["pnl_inr"] > 0).mean() * 100 if len(ce_trades) > 0 else 0
        st.markdown(
            f"**CE trades:** {len(ce_trades)}  |  "
            f"Win rate: {ce_wr:.1f}%  |  "
            f"P&L: \u20b9{ce_trades['pnl_inr'].sum():,.0f}"
        )
    with d2:
        pe_wr = (pe_trades["pnl_inr"] > 0).mean() * 100 if len(pe_trades) > 0 else 0
        st.markdown(
            f"**PE trades:** {len(pe_trades)}  |  "
            f"Win rate: {pe_wr:.1f}%  |  "
            f"P&L: \u20b9{pe_trades['pnl_inr'].sum():,.0f}"
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

    # All trades table
    st.divider()
    st.subheader("All Trades")

    fc1, fc2 = st.columns(2)
    with fc1:
        filter_type = st.multiselect(
            "Filter by option type", options=["CE", "PE"], key="se_filter_type"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason",
            options=["ST_FLIP", "SL", "TP", "EOD"],
            key="se_filter_reason",
        )

    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="st_ema_pullback_backtest.csv",
        mime="text/csv",
        key="se_download",
    )
```

- [ ] **Step 2: Verify import**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "from ui.st_ema_backtest_runner import render_st_ema_backtest; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

Suggested message: `feat(ui): add Streamlit runner for ST+EMA pullback backtest`

---

### Task 5: Register Tab in app.py

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add import**

Add after the existing imports (line 24 in app.py):

```python
from ui.st_ema_backtest_runner import render_st_ema_backtest
```

- [ ] **Step 2: Add the tab to the tab list**

Change the `st.tabs()` call to include the new tab. The current line is:

```python
tab_dashboard, tab_trades, tab_backtest, tab_forward, tab_stock, tab_pairs, tab_dema_st, tab_boom, tab_boom_st = st.tabs([
    "📊 Dashboard",
    "📋 Trade Explorer",
    "🚀 Run Backtest",
    "📡 Forward Test",
    "📈 Stock Strategy",
    "🔀 Pairs Strategy",
    "🔄 DEMA-ST Pullback",
    "💥 Boom SMA",
    "💥 Boom ST",
])
```

Replace with:

```python
tab_dashboard, tab_trades, tab_backtest, tab_forward, tab_stock, tab_pairs, tab_dema_st, tab_st_ema, tab_boom, tab_boom_st = st.tabs([
    "📊 Dashboard",
    "📋 Trade Explorer",
    "🚀 Run Backtest",
    "📡 Forward Test",
    "📈 Stock Strategy",
    "🔀 Pairs Strategy",
    "🔄 DEMA-ST Pullback",
    "🎯 ST+EMA Pullback",
    "💥 Boom SMA",
    "💥 Boom ST",
])
```

- [ ] **Step 3: Add the tab render block**

Add after the `with tab_dema_st:` block (after line 72):

```python
with tab_st_ema:
    render_st_ema_backtest()
```

- [ ] **Step 4: Verify app loads**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "import app; print('OK')"` (this will fail because streamlit requires runtime, so instead verify syntax):

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "import ast; ast.parse(open('app.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

- [ ] **Step 5: Commit**

Suggested message: `feat(app): register ST+EMA Pullback tab in main app`

---

### Task 6: Smoke Test — Run Backtest on Small Date Range

**Files:**
- No file changes. This is a verification task.

- [ ] **Step 1: Run a quick backtest via Python to verify end-to-end**

```bash
cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "
from engine.st_ema_backtest import StEmaBacktestEngine, trades_to_dataframe
engine = StEmaBacktestEngine(
    start_date='2025-03-01',
    end_date='2025-03-15',
)
trades = engine.run()
print(f'Trades: {len(trades)}')
if trades:
    df = trades_to_dataframe(trades)
    print(df[['date', 'option_type', 'strike', 'entry_price', 'exit_price', 'exit_reason', 'pnl_inr']].to_string())
else:
    print('No trades (check if data exists for this range)')
"
```

Expected: Prints trade table or "No trades" without errors.

- [ ] **Step 2: If errors, fix them in the engine and re-run**

Common issues to check:
- Column name mismatches between spot and options data
- NaN handling in indicator warmup
- Strike not found in options data (rounding issue)

- [ ] **Step 3: Run Streamlit app and verify the tab renders**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && streamlit run app.py`

Verify: The "ST+EMA Pullback" tab appears and the parameter form renders correctly.

- [ ] **Step 4: Run a backtest via the UI and verify results display**

Click "Run Backtest" with default parameters. Verify:
- Progress bar updates
- Results metrics display
- Equity curve renders
- Trade table is filterable
- CSV download works

- [ ] **Step 5: Commit any fixes**

Suggested message: `fix(engine): resolve issues found during ST+EMA smoke test`
