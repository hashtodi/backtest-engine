# PRT Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a custom backtest engine for the PRT (SuperTrend + MACD + VWAP + PCR) strategy with partial exit logic and a Streamlit UI.

**Architecture:** Custom engine class (`PrtBacktestEngine`) following the same pattern as `engine/st_ema_backtest.py`. Loads spot+PCR parquet, resamples to configurable timeframe, calculates indicators, forward-fills to 1-min, loops day-by-day with entry/exit logic including MACD partial exits. Streamlit UI runner with configurable params.

**Tech Stack:** Python, pandas, existing indicator classes (SuperTrend, MACD, VWAP), Streamlit

**Spec:** `docs/superpowers/specs/2026-04-09-prt-strategy-design.md`

---

### Task 1: Engine — Dataclass, __init__, and data pipeline

**Files:**
- Create: `engine/prt_backtest.py`

- [ ] **Step 1: Create engine file with imports, dataclass, and trades_to_dataframe**

```python
"""
PRT Strategy Backtest Engine.

Strategy (CE — bullish entry, all 4 on resampled spot candle):
  1. SuperTrend bullish (direction == -1)
  2. MACD histogram > 0 (state, not crossover)
  3. Spot candle close < VWAP
  4. PCR > threshold OR PCR uptrending (30-min change > 0)

Vice versa for PE (bearish).

Entry: ATM option at next 1-min bar OPEN after resampled candle close.
Exits: ST flip (full, pending), MACD flip (50%, pending),
       TP/SL on option premium (immediate), EOD.
Priority: SL/TP > indicator flips.

No-lookahead: indicators forward-filled with +timeframe shift,
PCR uses T-1 (pcr_prev), entry/exit at OPEN of boundary bar.
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
    STRIKE_ROUNDING,
    get_nearest_weekly_expiry,
)
from engine.data_loader import load_data
from indicators.macd import MACD
from indicators.supertrend import SuperTrend
from indicators.vwap import VWAP

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.dirname(__file__))


@dataclass
class PrtTrade:
    """Single PRT strategy trade with optional partial exit."""

    date: str
    option_type: str          # CE / PE
    strike: float
    expiry_date: str

    # Signal context
    signal_time: str
    st_direction: float
    macd_histogram: float
    spot_close: float         # resampled candle close
    vwap_value: float
    pcr_value: float
    pcr_change: float

    # Entry
    entry_time: str
    entry_price: float        # option OPEN
    entry_lots: int

    # Partial exit (MACD flip) — "" / 0 if none
    partial_exit_time: str
    partial_exit_price: float
    partial_exit_lots: int
    partial_exit_pnl: float

    # Final exit
    exit_time: str
    exit_price: float
    exit_lots: int
    exit_reason: str          # ST_FLIP, TP, SL, EOD (MACD_FLIP only if all lots gone)

    # Combined P&L
    pnl_points: float
    pnl_pct: float
    pnl_inr: float


def trades_to_dataframe(trades: List[PrtTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
```

- [ ] **Step 2: Add PrtBacktestEngine.__init__**

Append to the same file:

```python
class PrtBacktestEngine:
    """PRT strategy backtest engine."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        spot_pcr_path: str = "data/spot/nifty/NIFTY_1m_pcr.parquet",
        spot_timeframe: int = 5,
        st_period: int = 10,
        st_factor: float = 3.0,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        tp_pct: float = 30.0,
        sl_pct: float = 30.0,
        lots_per_trade: int = 2,
        pcr_threshold: float = 1.0,
        pcr_lookback: int = 30,
        trading_start: str = "09:30",
        trading_end: str = "14:30",
        max_trades_per_day: int = 0,
        instrument: str = "NIFTY",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.spot_pcr_path = spot_pcr_path
        self.spot_timeframe = spot_timeframe
        self.st_period = st_period
        self.st_factor = st_factor
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal_period = macd_signal
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.lots_per_trade = lots_per_trade
        self.pcr_threshold = pcr_threshold
        self.pcr_lookback = pcr_lookback
        self.trading_start = trading_start
        self.trading_end = trading_end
        self.max_trades_per_day = max_trades_per_day
        self.instrument = instrument
        self.lot_size = LOT_SIZE.get(instrument, 75)

        self._spot_1m: Optional[pd.DataFrame] = None
        self._options_1m: Optional[pd.DataFrame] = None
```

- [ ] **Step 3: Add data pipeline methods**

Append to `PrtBacktestEngine`:

```python
    # ------------------------------------------------------------------
    # Data loading & preparation
    # ------------------------------------------------------------------

    def _load_spot_pcr(self) -> pd.DataFrame:
        """Load 1-min spot+PCR data with 30-day warmup for indicators."""
        path = os.path.join(BASE_DIR, self.spot_pcr_path)
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

        # PCR: forward-fill gaps, shift by 1 for no-lookahead
        df["pcr"] = df["pcr"].ffill()
        df["pcr_prev"] = df["pcr"].shift(1)
        df["pcr_change"] = df["pcr_prev"] - df["pcr_prev"].shift(self.pcr_lookback)

        start = pd.to_datetime(self.start_date).tz_localize("Asia/Kolkata")
        end = pd.to_datetime(self.end_date).tz_localize("Asia/Kolkata") + pd.Timedelta(
            days=1
        )
        warmup_start = start - pd.Timedelta(days=30)
        df = df[(df["datetime"] >= warmup_start) & (df["datetime"] < end)]
        return df.sort_values("datetime").reset_index(drop=True)

    def _resample(self, spot_1m: pd.DataFrame) -> pd.DataFrame:
        """Resample 1-min spot OHLCV to configured timeframe."""
        df = spot_1m.set_index("datetime").copy()
        df = df.between_time("09:15", "15:29")
        freq = f"{self.spot_timeframe}min"
        ohlcv = df.resample(freq).agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        ).dropna(subset=["close"])
        return ohlcv

    def _calculate_indicators(self, resampled: pd.DataFrame) -> pd.DataFrame:
        """Calculate SuperTrend, MACD, VWAP on resampled data."""
        df = resampled.copy()

        # SuperTrend
        st = SuperTrend(
            name="st", factor=self.st_factor, atr_period=self.st_period
        )
        st_result = st.calculate(df["close"], high=df["high"], low=df["low"])
        df["st_dir"] = st_result["direction"]
        df["st_val"] = st_result["value"]

        # MACD
        macd = MACD(
            name="macd",
            fast=self.macd_fast,
            slow=self.macd_slow,
            signal=self.macd_signal_period,
        )
        macd_result = macd.calculate(df["close"])
        df["macd_line"] = macd_result["macd"]
        df["macd_signal"] = macd_result["signal"]
        df["macd_hist"] = macd_result["histogram"]

        # VWAP — resets daily
        df["_date"] = df.index.date
        vwap_calc = VWAP(name="vwap")
        vwap_parts = []
        for _, day_group in df.groupby("_date"):
            vwap_series = vwap_calc.calculate(day_group["close"], day_group["volume"])
            vwap_parts.append(vwap_series)
        df["vwap"] = pd.concat(vwap_parts)
        df.drop(columns=["_date"], inplace=True)

        # Store resampled close for VWAP comparison (close < vwap)
        df["resampled_close"] = df["close"]

        return df

    def _forward_fill_to_1m(
        self, spot_1m: pd.DataFrame, resampled: pd.DataFrame
    ) -> pd.DataFrame:
        """Forward-fill resampled indicators to 1-min with +timeframe shift."""
        cols = [
            "st_dir", "st_val", "macd_line", "macd_signal", "macd_hist",
            "vwap", "resampled_close",
        ]
        ind = resampled[cols].copy()
        # Shift by timeframe: candle values available only after candle closes
        ind.index = ind.index + pd.Timedelta(minutes=self.spot_timeframe)

        spot_1m = spot_1m.copy()
        spot_idx = spot_1m.set_index("datetime").index
        ind_1m = ind.reindex(spot_idx, method="ffill")

        for col in cols:
            spot_1m[col] = ind_1m[col].values
        return spot_1m

    def _prepare_data(self):
        """Full data pipeline: load → resample → indicators → forward-fill."""
        logger.info("Loading spot+PCR data...")
        spot_1m = self._load_spot_pcr()
        logger.info(f"Spot 1m: {len(spot_1m):,} rows")

        logger.info(f"Resampling to {self.spot_timeframe}-min...")
        resampled = self._resample(spot_1m)
        logger.info(f"Resampled: {len(resampled):,} rows")

        logger.info("Calculating indicators on resampled data...")
        resampled = self._calculate_indicators(resampled)

        logger.info("Forward-filling indicators to 1-min...")
        spot_1m = self._forward_fill_to_1m(spot_1m, resampled)

        # Trim warmup days
        start_dt = pd.to_datetime(self.start_date).date()
        spot_1m = spot_1m[spot_1m["date"] >= start_dt].reset_index(drop=True)
        self._spot_1m = spot_1m

        # Load options data
        logger.info("Loading options data...")
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        self._options_1m = load_data(
            options_path, self.start_date, self.end_date, "weekly"
        )
        logger.info(f"Options 1m: {len(self._options_1m):,} rows")
```

- [ ] **Step 4: Verify file structure so far**

Run: `python -c "from engine.prt_backtest import PrtBacktestEngine, PrtTrade, trades_to_dataframe; print('OK')"`

Expected: `OK`

---

### Task 2: Engine — Day processing loop (entry + exit logic)

**Files:**
- Modify: `engine/prt_backtest.py`

- [ ] **Step 1: Add _process_day method**

Append to `PrtBacktestEngine`:

```python
    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def _process_day(self, trading_date) -> List[PrtTrade]:
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

        trades: List[PrtTrade] = []
        day_trade_count = 0

        # --- Position state ---
        in_position = False
        remaining_lots = 0
        macd_partial_done = False
        entry_price = 0.0
        option_strike = 0.0
        option_type = ""
        tp_price = 0.0
        sl_price = 0.0
        signal_time = ""
        entry_time = ""
        signal_indicators: dict = {}

        # Partial exit tracking
        partial_exit_time = ""
        partial_exit_price = 0.0
        partial_exit_lots = 0
        partial_exit_pnl = 0.0

        # Pending indicator exit: None, "ST_FLIP", or "MACD_FLIP"
        pending_exit: Optional[str] = None

        # Previous bar's forward-filled values (for flip detection)
        prev_st_dir: Optional[float] = None
        prev_macd_hist: Optional[float] = None

        for _, candle in day_spot.iterrows():
            t_str = candle["time_str"]
            t_dt = candle["datetime"]
            st_dir = candle.get("st_dir", np.nan)
            macd_hist = candle.get("macd_hist", np.nan)
            vwap_val = candle.get("vwap", np.nan)
            resampled_close = candle.get("resampled_close", np.nan)
            pcr_prev_val = candle.get("pcr_prev", np.nan)
            pcr_change_val = candle.get("pcr_change", np.nan)

            # Skip bars with no indicator data (warmup period)
            if pd.isna(st_dir) or pd.isna(macd_hist) or pd.isna(vwap_val):
                prev_st_dir = st_dir
                prev_macd_hist = macd_hist
                continue

            # Skip pre-trading hours (but still update prev values)
            if t_str < self.trading_start:
                prev_st_dir = st_dir
                prev_macd_hist = macd_hist
                continue

            just_exited = False

            # ============ 1. FILL PENDING INDICATOR EXIT ============
            if pending_exit is not None and in_position:
                opt_open = self._get_option_open(
                    opt_by_dt, t_dt, option_strike, option_type
                )
                if opt_open is not None:
                    if pending_exit == "ST_FLIP":
                        trade = self._make_trade(
                            trading_date, option_type, option_strike,
                            expiry_date, signal_indicators, signal_time,
                            entry_time, entry_price, self.lots_per_trade,
                            partial_exit_time, partial_exit_price,
                            partial_exit_lots, partial_exit_pnl,
                            t_str, opt_open, remaining_lots, "ST_FLIP",
                        )
                        trades.append(trade)
                        in_position = False
                        just_exited = True
                        day_trade_count += 1

                    elif pending_exit == "MACD_FLIP" and not macd_partial_done:
                        exit_lots = remaining_lots // 2
                        if exit_lots > 0:
                            partial_exit_time = t_str
                            partial_exit_price = opt_open
                            partial_exit_lots = exit_lots
                            partial_exit_pnl = round(
                                (opt_open - entry_price) * exit_lots * self.lot_size,
                                2,
                            )
                            remaining_lots -= exit_lots
                            macd_partial_done = True
                            logger.debug(
                                f"{trading_date} {t_str} MACD partial exit "
                                f"{exit_lots} lot(s) @ {opt_open}"
                            )
                        # If all lots gone via partial (e.g. lots_per_trade=1)
                        if remaining_lots <= 0:
                            trade = self._make_trade(
                                trading_date, option_type, option_strike,
                                expiry_date, signal_indicators, signal_time,
                                entry_time, entry_price, self.lots_per_trade,
                                partial_exit_time, partial_exit_price,
                                partial_exit_lots, partial_exit_pnl,
                                t_str, opt_open, 0, "MACD_FLIP",
                            )
                            trades.append(trade)
                            in_position = False
                            just_exited = True
                            day_trade_count += 1

                pending_exit = None

            # ============ 2. SL / TP CHECK (every bar) ============
            if in_position:
                ohlc = self._get_option_ohlc(
                    opt_by_dt, t_dt, option_strike, option_type
                )
                if ohlc is not None:
                    opt_o, opt_h, opt_l, opt_c = ohlc

                    # SL first (conservative)
                    sl_hit, sl_exit_p = False, 0.0
                    if opt_o <= sl_price:
                        sl_hit, sl_exit_p = True, opt_o  # gap below SL
                    elif opt_l <= sl_price:
                        sl_hit, sl_exit_p = True, sl_price

                    # TP (only if SL not hit — SL wins on same bar)
                    tp_hit, tp_exit_p = False, 0.0
                    if not sl_hit:
                        if opt_o >= tp_price:
                            tp_hit, tp_exit_p = True, opt_o  # gap above TP
                        elif opt_h >= tp_price:
                            tp_hit, tp_exit_p = True, tp_price

                    if sl_hit:
                        trade = self._make_trade(
                            trading_date, option_type, option_strike,
                            expiry_date, signal_indicators, signal_time,
                            entry_time, entry_price, self.lots_per_trade,
                            partial_exit_time, partial_exit_price,
                            partial_exit_lots, partial_exit_pnl,
                            t_str, sl_exit_p, remaining_lots, "SL",
                        )
                        trades.append(trade)
                        in_position = False
                        just_exited = True
                        day_trade_count += 1
                        pending_exit = None

                    elif tp_hit:
                        trade = self._make_trade(
                            trading_date, option_type, option_strike,
                            expiry_date, signal_indicators, signal_time,
                            entry_time, entry_price, self.lots_per_trade,
                            partial_exit_time, partial_exit_price,
                            partial_exit_lots, partial_exit_pnl,
                            t_str, tp_exit_p, remaining_lots, "TP",
                        )
                        trades.append(trade)
                        in_position = False
                        just_exited = True
                        day_trade_count += 1
                        pending_exit = None

            # ============ 3. INDICATOR FLIP DETECTION (boundary bars) ============
            is_boundary = t_dt.minute % self.spot_timeframe == 0

            if in_position and pending_exit is None and is_boundary:
                if (
                    prev_st_dir is not None
                    and not np.isnan(prev_st_dir)
                    and st_dir != prev_st_dir
                ):
                    # ST flipped against our position?
                    if option_type == "CE" and st_dir == 1:      # was bullish → bearish
                        pending_exit = "ST_FLIP"
                    elif option_type == "PE" and st_dir == -1:   # was bearish → bullish
                        pending_exit = "ST_FLIP"

                if (
                    pending_exit is None
                    and not macd_partial_done
                    and prev_macd_hist is not None
                    and not np.isnan(prev_macd_hist)
                ):
                    if option_type == "CE" and prev_macd_hist > 0 and macd_hist <= 0:
                        pending_exit = "MACD_FLIP"
                    elif option_type == "PE" and prev_macd_hist < 0 and macd_hist >= 0:
                        pending_exit = "MACD_FLIP"

            # ============ 4. EOD CHECK ============
            if in_position and t_str >= self.trading_end:
                ohlc = self._get_option_ohlc(
                    opt_by_dt, t_dt, option_strike, option_type
                )
                if ohlc is not None:
                    _, _, _, opt_c = ohlc
                    trade = self._make_trade(
                        trading_date, option_type, option_strike,
                        expiry_date, signal_indicators, signal_time,
                        entry_time, entry_price, self.lots_per_trade,
                        partial_exit_time, partial_exit_price,
                        partial_exit_lots, partial_exit_pnl,
                        t_str, opt_c, remaining_lots, "EOD",
                    )
                    trades.append(trade)
                    in_position = False
                    just_exited = True
                    day_trade_count += 1
                    pending_exit = None

            # ============ 5. ENTRY CHECK (boundary bars) ============
            at_trade_cap = (
                self.max_trades_per_day > 0
                and day_trade_count >= self.max_trades_per_day
            )
            if (
                not in_position
                and not just_exited
                and is_boundary
                and t_str >= self.trading_start
                and t_str < self.trading_end
                and not at_trade_cap
            ):
                sig_type = self._check_signal(
                    st_dir, macd_hist, resampled_close, vwap_val,
                    pcr_prev_val, pcr_change_val,
                )
                if sig_type is not None:
                    rounding = STRIKE_ROUNDING.get(self.instrument, 50)
                    spot_open = candle["open"]
                    atm_strike = round(spot_open / rounding) * rounding

                    opt_open = self._get_option_open(
                        opt_by_dt, t_dt, atm_strike, sig_type
                    )
                    if opt_open is not None and opt_open > 0:
                        entry_price = opt_open
                        option_strike = atm_strike
                        option_type = sig_type
                        entry_time = t_str
                        signal_time = t_str
                        remaining_lots = self.lots_per_trade
                        macd_partial_done = False
                        in_position = True
                        pending_exit = None

                        tp_price = round(
                            entry_price * (1 + self.tp_pct / 100), 2
                        )
                        sl_price = round(
                            entry_price * (1 - self.sl_pct / 100), 2
                        )

                        # Reset partial exit tracking
                        partial_exit_time = ""
                        partial_exit_price = 0.0
                        partial_exit_lots = 0
                        partial_exit_pnl = 0.0

                        signal_indicators = {
                            "st_dir": float(st_dir),
                            "macd_hist": round(float(macd_hist), 4),
                            "spot_close": round(float(resampled_close), 2),
                            "vwap": round(float(vwap_val), 2),
                            "pcr": (
                                round(float(pcr_prev_val), 4)
                                if not pd.isna(pcr_prev_val)
                                else 0.0
                            ),
                            "pcr_change": (
                                round(float(pcr_change_val), 4)
                                if not pd.isna(pcr_change_val)
                                else 0.0
                            ),
                        }

                        logger.debug(
                            f"{trading_date} {t_str} ENTRY {option_type} "
                            f"strike={option_strike} price={entry_price} "
                            f"TP={tp_price} SL={sl_price}"
                        )

            # Update previous bar values
            prev_st_dir = st_dir
            prev_macd_hist = macd_hist

        # Safety net: force close if still in position at day end
        if in_position:
            exit_price, exit_time_str = self._last_option_price(
                day_options, option_strike, option_type
            )
            trade = self._make_trade(
                trading_date, option_type, option_strike,
                expiry_date, signal_indicators, signal_time,
                entry_time, entry_price, self.lots_per_trade,
                partial_exit_time, partial_exit_price,
                partial_exit_lots, partial_exit_pnl,
                exit_time_str, exit_price, remaining_lots, "EOD",
            )
            trades.append(trade)

        return trades
```

- [ ] **Step 2: Verify file parses**

Run: `python -c "from engine.prt_backtest import PrtBacktestEngine; print('OK')"`

Expected: `OK`

---

### Task 3: Engine — Signal check, helpers, and run()

**Files:**
- Modify: `engine/prt_backtest.py`

- [ ] **Step 1: Add _check_signal, option helpers, _make_trade, and run()**

Append to `PrtBacktestEngine`:

```python
    # ------------------------------------------------------------------
    # Signal detection
    # ------------------------------------------------------------------

    def _check_signal(
        self,
        st_dir: float,
        macd_hist: float,
        resampled_close: float,
        vwap_val: float,
        pcr_prev_val: float,
        pcr_change_val: float,
    ) -> Optional[str]:
        """Check entry conditions. Returns 'CE', 'PE', or None."""
        if pd.isna(pcr_prev_val):
            return None

        pcr_up = pcr_prev_val > self.pcr_threshold or (
            not pd.isna(pcr_change_val) and pcr_change_val > 0
        )
        pcr_down = pcr_prev_val < self.pcr_threshold or (
            not pd.isna(pcr_change_val) and pcr_change_val < 0
        )

        # CE (bullish)
        if (
            st_dir == -1
            and macd_hist > 0
            and resampled_close < vwap_val
            and pcr_up
        ):
            return "CE"

        # PE (bearish)
        if (
            st_dir == 1
            and macd_hist < 0
            and resampled_close > vwap_val
            and pcr_down
        ):
            return "PE"

        return None

    # ------------------------------------------------------------------
    # Option data helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_option_open(opt_by_dt, t_dt, strike, option_type) -> Optional[float]:
        """Get option open price for a given strike/type at a datetime."""
        minute_opts = opt_by_dt.get(t_dt)
        if minute_opts is None:
            return None
        match = minute_opts[
            (minute_opts["strike"] == strike)
            & (minute_opts["option_type"] == option_type)
        ]
        if match.empty:
            return None
        return round(match.iloc[0]["open"], 2)

    @staticmethod
    def _get_option_ohlc(opt_by_dt, t_dt, strike, option_type):
        """Get (open, high, low, close) for a given strike/type at a datetime."""
        minute_opts = opt_by_dt.get(t_dt)
        if minute_opts is None:
            return None
        match = minute_opts[
            (minute_opts["strike"] == strike)
            & (minute_opts["option_type"] == option_type)
        ]
        if match.empty:
            return None
        row = match.iloc[0]
        return (
            round(row["open"], 2),
            round(row["high"], 2),
            round(row["low"], 2),
            round(row["close"], 2),
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

    # ------------------------------------------------------------------
    # Trade builder
    # ------------------------------------------------------------------

    def _make_trade(
        self,
        trading_date,
        option_type,
        strike,
        expiry_date,
        indicators,
        signal_time,
        entry_time,
        entry_price,
        entry_lots,
        partial_exit_time,
        partial_exit_price,
        partial_exit_lots,
        partial_exit_pnl,
        exit_time,
        exit_price,
        exit_lots,
        exit_reason,
    ) -> PrtTrade:
        final_pnl = round(
            (exit_price - entry_price) * exit_lots * self.lot_size, 2
        )
        total_pnl = round(partial_exit_pnl + final_pnl, 2)
        total_points = (
            round(total_pnl / (entry_lots * self.lot_size), 2)
            if entry_lots > 0
            else 0.0
        )
        total_pct = (
            round(
                total_pnl / (entry_price * entry_lots * self.lot_size) * 100, 3
            )
            if entry_price > 0 and entry_lots > 0
            else 0.0
        )

        return PrtTrade(
            date=str(trading_date),
            option_type=option_type,
            strike=strike,
            expiry_date=str(expiry_date),
            signal_time=signal_time,
            st_direction=indicators.get("st_dir", 0.0),
            macd_histogram=indicators.get("macd_hist", 0.0),
            spot_close=indicators.get("spot_close", 0.0),
            vwap_value=indicators.get("vwap", 0.0),
            pcr_value=indicators.get("pcr", 0.0),
            pcr_change=indicators.get("pcr_change", 0.0),
            entry_time=entry_time,
            entry_price=entry_price,
            entry_lots=entry_lots,
            partial_exit_time=partial_exit_time,
            partial_exit_price=partial_exit_price,
            partial_exit_lots=partial_exit_lots,
            partial_exit_pnl=partial_exit_pnl,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_lots=exit_lots,
            exit_reason=exit_reason,
            pnl_points=total_points,
            pnl_pct=total_pct,
            pnl_inr=total_pnl,
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[PrtTrade]:
        """Run backtest. Returns list of PrtTrade."""
        self._prepare_data()

        all_dates = sorted(self._spot_1m["date"].unique())
        trades: List[PrtTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))
            day_trades = self._process_day(trading_date)
            trades.extend(day_trades)

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades
```

- [ ] **Step 2: Verify full engine imports cleanly**

Run: `python -c "from engine.prt_backtest import PrtBacktestEngine, PrtTrade, trades_to_dataframe; print('OK')"`

Expected: `OK`

---

### Task 4: UI Runner

**Files:**
- Create: `ui/prt_backtest_runner.py`

- [ ] **Step 1: Create UI runner file**

```python
"""
PRT Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.prt_backtest import PrtBacktestEngine, trades_to_dataframe


def render_prt_backtest():
    st.header("PRT Strategy")
    st.caption(
        "SuperTrend + MACD + VWAP + PCR  →  ATM option entry  →  "
        "ST flip (full) / MACD flip (50%) / TP-SL exits"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-01").date(), key="prt_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-04-02").date(), key="prt_end"
            )
            st.markdown("**Data**")
            spot_pcr_path = st.text_input(
                "Spot+PCR file path",
                value="data/spot/nifty/NIFTY_1m_pcr.parquet",
                key="prt_file",
            )
            spot_tf = st.selectbox(
                "Spot timeframe (min)", options=[3, 5], index=1, key="prt_tf"
            )

        with col2:
            st.markdown("**SuperTrend**")
            st_period = st.number_input(
                "ST period", value=10, step=1, min_value=1, key="prt_st_p"
            )
            st_factor = st.number_input(
                "ST factor", value=3.0, step=0.5, min_value=0.5, key="prt_st_f"
            )
            st.markdown("**MACD**")
            macd_fast = st.number_input(
                "Fast", value=12, step=1, min_value=1, key="prt_macd_f"
            )
            macd_slow = st.number_input(
                "Slow", value=26, step=1, min_value=1, key="prt_macd_s"
            )
            macd_signal = st.number_input(
                "Signal", value=9, step=1, min_value=1, key="prt_macd_sig"
            )

        with col3:
            st.markdown("**Exit**")
            tp_pct = st.number_input(
                "TP (%)", value=30.0, step=1.0, min_value=1.0, key="prt_tp"
            )
            sl_pct = st.number_input(
                "SL (%)", value=30.0, step=1.0, min_value=1.0, key="prt_sl"
            )
            lots = st.number_input(
                "Lots per trade", value=2, step=1, min_value=1, key="prt_lots"
            )
            st.markdown("**PCR**")
            pcr_threshold = st.number_input(
                "PCR threshold", value=1.0, step=0.1, min_value=0.0, key="prt_pcr_th"
            )
            pcr_lookback = st.number_input(
                "PCR lookback (min)", value=30, step=5, min_value=1, key="prt_pcr_lb"
            )

    with st.expander("Time & Limits", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            trading_start = st.text_input(
                "Entry start", value="09:30", key="prt_t_start"
            )
        with tc2:
            trading_end = st.text_input(
                "Entry end / EOD exit", value="14:30", key="prt_t_end"
            )
        with tc3:
            max_trades = st.number_input(
                "Max trades/day (0=unlimited)",
                value=0, step=1, min_value=0, key="prt_max_trades",
            )

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="prt_run"):
        engine = PrtBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            spot_pcr_path=spot_pcr_path,
            spot_timeframe=int(spot_tf),
            st_period=int(st_period),
            st_factor=float(st_factor),
            macd_fast=int(macd_fast),
            macd_slow=int(macd_slow),
            macd_signal=int(macd_signal),
            tp_pct=float(tp_pct),
            sl_pct=float(sl_pct),
            lots_per_trade=int(lots),
            pcr_threshold=float(pcr_threshold),
            pcr_lookback=int(pcr_lookback),
            trading_start=trading_start,
            trading_end=trading_end,
            max_trades_per_day=int(max_trades),
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

        st.session_state["prt_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "prt_results" in st.session_state:
        _show_results(st.session_state["prt_results"])


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
    r1.metric("ST Flip exits", int(reasons.get("ST_FLIP", 0)))
    r2.metric("SL exits", int(reasons.get("SL", 0)))
    r3.metric("TP exits", int(reasons.get("TP", 0)))
    r4.metric("EOD exits", int(reasons.get("EOD", 0)))
    r5.metric("MACD Flip exits", int(reasons.get("MACD_FLIP", 0)))

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

    # Row 4: Partial exit stats
    partial_trades = df[df["partial_exit_lots"] > 0]
    st.markdown(
        f"**Partial exits (MACD flip):** {len(partial_trades)} of {total} trades "
        f"({len(partial_trades) / total * 100:.1f}%)"
        if total > 0
        else "**Partial exits:** 0"
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

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        filter_type = st.multiselect(
            "Filter by option type", options=["CE", "PE"], key="prt_filter_type"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason",
            options=["ST_FLIP", "MACD_FLIP", "SL", "TP", "EOD"],
            key="prt_filter_reason",
        )
    with fc3:
        filter_date = st.text_input(
            "Filter by date (YYYY-MM-DD)", value="", key="prt_filter_date"
        )

    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]
    if filter_date:
        filtered = filtered[filtered["date"] == filter_date]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="prt_strategy_backtest.csv",
        mime="text/csv",
        key="prt_download",
    )
```

- [ ] **Step 2: Verify UI runner imports**

Run: `python -c "from ui.prt_backtest_runner import render_prt_backtest; print('OK')"`

Expected: `OK`

---

### Task 5: Wire PRT tab into app.py

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add import**

Add after the existing import block (after `from ui.bb_reversal_pine_exit_backtest_runner import ...`):

```python
from ui.prt_backtest_runner import render_prt_backtest
```

- [ ] **Step 2: Add tab to the st.tabs list**

Update the `st.tabs([...])` call to add `"PRT Strategy"` at the end, and add the corresponding tab variable. The line becomes:

```python
tab_dashboard, tab_trades, tab_backtest, tab_forward, tab_dema_st, tab_st_ema, tab_straddle_vwap, tab_boom, tab_boom_st, tab_vwap_ema_rsi, tab_bb_reversal, tab_bb_reversal_pine, tab_bb_reversal_pine_exit, tab_prt = st.tabs([
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
])
```

- [ ] **Step 3: Add tab content**

Add at the end of `app.py`, after the `with tab_bb_reversal_pine_exit:` block:

```python
with tab_prt:
    render_prt_backtest()
```

- [ ] **Step 4: Verify app loads**

Run: `python -c "import app; print('OK')"` (will fail in non-Streamlit context, but verifies imports)

Alternative: `python -c "from ui.prt_backtest_runner import render_prt_backtest; from engine.prt_backtest import PrtBacktestEngine; print('All imports OK')"`

Expected: `All imports OK`

---

### Task 6: Smoke test

- [ ] **Step 1: Verify the Streamlit app launches without errors**

Run: `streamlit run app.py` and check that the PRT Strategy tab appears and renders the parameter form.

Expected: Tab visible, form renders, no Python errors.

- [ ] **Step 2: Run backtest with data (when spot+PCR file is available)**

Once the user provides the spot+PCR parquet file, select the PRT Strategy tab, set parameters, and click "Run Backtest."

Expected: Progress bar, trade results displayed, equity curve rendered, CSV download available.
