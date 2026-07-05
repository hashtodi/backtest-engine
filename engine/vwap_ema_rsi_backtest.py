"""
VWAP + EMA + RSI Momentum Backtest Engine.

Strategy:
  Signal (5-min closed bar):
    - Buy CE: spot > VWAP+offset% AND EMA9 > EMA20 AND RSI > upper_thresh
              AND strong bullish candle (close in top 25% of range)
              AND EMA9/20 not flat (diff > 0.05% of spot)
    - Buy PE: spot < VWAP-offset% AND EMA9 < EMA20 AND RSI < lower_thresh
              AND strong bearish candle (close in bottom 25% of range)
              AND EMA9/20 not flat

  Entry:
    - Next 1-min candle open after signal fires
    - ATM strike = round(spot / 50) * 50, nearest weekly expiry

  Exit (checked on 1-min spot candles, SL assumed hit first on ambiguity):
    1. SL: spot breaches entry_spot ± sl_points
    2. TP: spot reaches entry_spot ± tp_points
    3. Trailing SL (3-stage):
       a. +15 pts profit → SL to cost (entry_spot)
       b. +20 pts profit → SL to entry_spot + 10
       c. Beyond +20 pts → SL trails 10 pts from peak favorable spot
    4. EOD: force exit at force_exit_time

  Risk:
    - Max N trades/day
    - Stop after M consecutive losses (resets daily, breakeven ≠ loss)

  All indicators on 5-min resampled spot, forward-filled to 1-min.
  SL/TP on spot price; P&L from option premiums.
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
from indicators.rsi import RSI
from indicators.vwap import VWAP

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------

@dataclass
class VwapEmaRsiTrade:
    date: str                    # "YYYY-MM-DD"
    option_type: str             # "CE" / "PE"
    strike: float
    expiry_date: str

    # Indicators at entry
    vwap: float
    ema_short: float             # EMA9
    ema_long: float              # EMA20
    rsi: float
    spot_at_entry: float

    # Levels
    sl_level: float              # SL at exit (may have trailed)
    tp_level: float              # entry_spot ± tp_points
    trail_triggered: bool        # did trailing SL activate?
    peak_favorable: float        # best spot price in trade direction

    # Times
    signal_time: str             # "HH:MM" when 5-min signal fired
    entry_time: str              # "HH:MM" when filled (next 1-min open)
    entry_price: float           # option premium at entry
    qty: int

    exit_time: str
    exit_price: float
    exit_reason: str             # "SL" / "TP" / "TRAIL_SL" / "EOD"

    pnl_points: float            # exit_price - entry_price (option)
    pnl_pct: float
    pnl_inr: float               # pnl_points * qty


def trades_to_dataframe(trades: List[VwapEmaRsiTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class VwapEmaRsiBacktestEngine:
    """VWAP + EMA + RSI momentum strategy backtest engine."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        ema_short_period: int = 9,
        ema_long_period: int = 20,
        rsi_period: int = 14,
        rsi_upper: float = 55.0,
        rsi_lower: float = 45.0,
        vwap_offset_pct: float = 0.2,
        sl_points: float = 15.0,
        tp_points: float = 30.0,
        trail_stage1_trigger: float = 15.0,   # +15 → SL to cost
        trail_stage2_trigger: float = 20.0,   # +20 → SL to +10
        trail_stage2_lock: float = 10.0,      # profit locked at stage 2
        trail_distance: float = 10.0,         # beyond stage 2, trail by this
        max_trades_per_day: int = 3,
        max_consecutive_losses: int = 2,
        ema_flat_pct: float = 0.05,           # EMA9/20 flat if diff ≤ this % of spot
        candle_strength_pct: float = 25.0,    # close in top/bottom N% of range
        entry_start: str = "09:20",
        force_exit_time: str = "14:30",
        instrument: str = "NIFTY",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.ema_short_period = ema_short_period
        self.ema_long_period = ema_long_period
        self.rsi_period = rsi_period
        self.rsi_upper = rsi_upper
        self.rsi_lower = rsi_lower
        self.vwap_offset_pct = vwap_offset_pct
        self.sl_points = sl_points
        self.tp_points = tp_points
        self.trail_stage1_trigger = trail_stage1_trigger
        self.trail_stage2_trigger = trail_stage2_trigger
        self.trail_stage2_lock = trail_stage2_lock
        self.trail_distance = trail_distance
        self.max_trades_per_day = max_trades_per_day
        self.max_consecutive_losses = max_consecutive_losses
        self.ema_flat_pct = ema_flat_pct
        self.candle_strength_pct = candle_strength_pct
        self.entry_start = entry_start
        self.force_exit_time = force_exit_time
        self.instrument = instrument
        self.lot_size = LOT_SIZE.get(instrument, 75)

        self._spot_1m: Optional[pd.DataFrame] = None
        self._options_1m: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Data loading & preparation
    # ------------------------------------------------------------------

    def _load_spot(self) -> pd.DataFrame:
        """Load 1-min spot data with warmup for indicators."""
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
        """Calculate VWAP, EMA short, EMA long, RSI on 5-min data."""
        ema_s = EMA(name="ema_s", period=self.ema_short_period)
        ema_l = EMA(name="ema_l", period=self.ema_long_period)
        rsi_ind = RSI(name="rsi", period=self.rsi_period)

        spot_5m = spot_5m.copy()
        spot_5m["ema_s"] = ema_s.calculate(spot_5m["close"])
        spot_5m["ema_l"] = ema_l.calculate(spot_5m["close"])
        spot_5m["rsi"] = rsi_ind.calculate(spot_5m["close"])

        # VWAP: reset daily, use typical price (H+L+C)/3
        spot_5m["vwap"] = np.nan
        for date, group in spot_5m.groupby(spot_5m.index.date):
            vwap_ind = VWAP(name="vwap")
            typical_price = (group["high"] + group["low"] + group["close"]) / 3
            vwap_vals = vwap_ind.calculate(typical_price, group["volume"])
            spot_5m.loc[group.index, "vwap"] = vwap_vals

        return spot_5m

    def _forward_fill_to_1m(
        self, spot_1m: pd.DataFrame, spot_5m: pd.DataFrame
    ) -> pd.DataFrame:
        """Forward-fill 5-min indicators + OHLC onto 1-min candles.

        Shifts by +5min: the 09:15 bar's values become available at 09:20.
        """
        # Indicators + 5m OHLC for strong candle filter
        cols_to_fill = ["ema_s", "ema_l", "rsi", "vwap",
                        "open_5m", "high_5m", "low_5m", "close_5m"]

        spot_5m = spot_5m.copy()
        spot_5m["open_5m"] = spot_5m["open"]
        spot_5m["high_5m"] = spot_5m["high"]
        spot_5m["low_5m"] = spot_5m["low"]
        spot_5m["close_5m"] = spot_5m["close"]

        ind_5m = spot_5m[cols_to_fill].copy()
        ind_5m.index = ind_5m.index + pd.Timedelta(minutes=5)

        spot_1m = spot_1m.copy()
        spot_1m_idx = spot_1m.set_index("datetime").index
        ind_1m = ind_5m.reindex(spot_1m_idx, method="ffill")

        for col in cols_to_fill:
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

        logger.info("Loading options data...")
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        self._options_1m = load_data(
            options_path, self.start_date, self.end_date, "weekly"
        )
        logger.info(f"Options 1m: {len(self._options_1m):,} rows")

    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[VwapEmaRsiTrade]:
        """Run backtest. Returns list of VwapEmaRsiTrade."""
        self._prepare_data()

        all_dates = sorted(self._spot_1m["date"].unique())
        trades: List[VwapEmaRsiTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))

            day_trades = self._process_day(trading_date)
            trades.extend(day_trades)

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    def _is_strong_candle(self, candle, direction: str) -> bool:
        """Check if the 5-min candle is a strong directional candle.

        Bullish: close > open (green candle) AND close >= high - 25% of range
        Bearish: close < open (red candle) AND close <= low + 25% of range
        """
        o = candle["open_5m"]
        h = candle["high_5m"]
        l = candle["low_5m"]
        c = candle["close_5m"]
        rng = h - l
        if rng <= 0:
            return False
        threshold = rng * self.candle_strength_pct / 100
        if direction == "CE":
            return c > o and c >= h - threshold
        else:  # PE
            return c < o and c <= l + threshold

    def _is_ema_flat(self, ema_s, ema_l, spot_close) -> bool:
        """Check if EMA9 and EMA20 are flat/overlapping.

        Flat = abs(EMA9 - EMA20) <= ema_flat_pct% of spot.
        """
        if spot_close <= 0:
            return True
        diff_pct = abs(ema_s - ema_l) / spot_close * 100
        return diff_pct <= self.ema_flat_pct

    def _update_trailing_sl(
        self, option_type, spot_high, spot_low,
        entry_spot, sl_level, trail_triggered, peak_favorable,
    ):
        """Update trailing SL based on 3-stage logic.

        Stage 1: +trail_stage1_trigger → SL to cost
        Stage 2: +trail_stage2_trigger → SL to entry + trail_stage2_lock
        Stage 3: beyond stage 2 → SL trails trail_distance from peak

        Returns (sl_level, trail_triggered, peak_favorable).
        """
        if option_type == "CE":
            # Update peak
            peak_favorable = max(peak_favorable, spot_high)
            profit = peak_favorable - entry_spot

            if profit >= self.trail_stage2_trigger:
                # Stage 3: trail by distance from peak
                new_sl = peak_favorable - self.trail_distance
                # Stage 2 minimum: entry + lock
                stage2_sl = entry_spot + self.trail_stage2_lock
                new_sl = max(new_sl, stage2_sl)
                if new_sl > sl_level:
                    sl_level = round(new_sl, 2)
                    trail_triggered = True
            elif profit >= self.trail_stage1_trigger:
                # Stage 1: SL to cost
                if entry_spot > sl_level:
                    sl_level = entry_spot
                    trail_triggered = True
        else:  # PE
            # Update peak (lowest spot = best for PE)
            peak_favorable = min(peak_favorable, spot_low)
            profit = entry_spot - peak_favorable

            if profit >= self.trail_stage2_trigger:
                new_sl = peak_favorable + self.trail_distance
                stage2_sl = entry_spot - self.trail_stage2_lock
                new_sl = min(new_sl, stage2_sl)
                if new_sl < sl_level:
                    sl_level = round(new_sl, 2)
                    trail_triggered = True
            elif profit >= self.trail_stage1_trigger:
                if entry_spot < sl_level:
                    sl_level = entry_spot
                    trail_triggered = True

        return sl_level, trail_triggered, peak_favorable

    def _process_day(self, trading_date) -> List[VwapEmaRsiTrade]:
        """Process a single trading day."""
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

        trades: List[VwapEmaRsiTrade] = []

        # Daily state
        trade_count = 0
        consecutive_losses = 0

        # Position state
        in_position = False
        entry_price = None        # option premium
        entry_spot = None         # spot at entry
        option_strike = None
        option_type = None
        sl_level = None
        tp_level = None
        trail_triggered = False
        peak_favorable = None     # best spot in trade direction
        entry_time = None
        signal_time = None
        entry_indicators = {}

        # Pending entry from 5-min signal
        pending_entry = False
        pending_entry_data = {}
        pending_entry_bar = None   # candle after signal; cancel if not filled here

        # Track which 5-min bar we last processed
        last_signal_5m_bar = None

        for _, candle in day_spot.iterrows():
            t_str = candle["time_str"]
            t_dt = candle["datetime"]
            spot_close = candle["close"]
            spot_open = candle["open"]
            spot_low = candle["low"]
            spot_high = candle["high"]
            ema_s = candle["ema_s"]
            ema_l = candle["ema_l"]
            rsi_val = candle["rsi"]
            vwap_val = candle["vwap"]

            # Skip if indicators not ready
            if pd.isna(ema_s) or pd.isna(ema_l) or pd.isna(rsi_val) or pd.isna(vwap_val):
                continue

            # Skip before entry window
            if t_str < self.entry_start:
                continue

            # ============ 1. RESOLVE PENDING ENTRY ============
            just_entered = False
            if pending_entry and not in_position:
                if t_str >= self.force_exit_time:
                    pending_entry = False
                    pending_entry_data = {}
                    continue

                # Check daily limits before entering
                if trade_count >= self.max_trades_per_day:
                    pending_entry = False
                    pending_entry_data = {}
                    continue
                if consecutive_losses >= self.max_consecutive_losses:
                    pending_entry = False
                    pending_entry_data = {}
                    continue

                # Must fill on the very next candle; cancel if not
                if pending_entry_bar is not None and t_dt > pending_entry_bar:
                    logger.debug(
                        f"{trading_date} {t_str} PENDING ENTRY CANCELLED "
                        f"(no option data at {pending_entry_bar})"
                    )
                    pending_entry = False
                    pending_entry_data = {}
                    pending_entry_bar = None
                    # Don't continue — allow signal detection on this candle
                else:
                    ped = pending_entry_data
                    rounding = STRIKE_ROUNDING.get(self.instrument, 50)
                    atm_strike = round(spot_open / rounding) * rounding

                    minute_opts = opt_by_dt.get(t_dt)
                    filled = False
                    if minute_opts is not None:
                        opt_candle = minute_opts[
                            (minute_opts["strike"] == atm_strike)
                            & (minute_opts["option_type"] == ped["option_type"])
                        ]
                        if not opt_candle.empty:
                            entry_price = round(opt_candle.iloc[0]["open"], 2)
                            entry_spot = round(spot_open, 2)
                            option_strike = atm_strike
                            option_type = ped["option_type"]
                            signal_time = ped["signal_time"]
                            entry_time = t_str
                            entry_indicators = ped["indicators"]

                            # Set SL/TP on spot
                            if option_type == "CE":
                                sl_level = round(entry_spot - self.sl_points, 2)
                                tp_level = round(entry_spot + self.tp_points, 2)
                                peak_favorable = entry_spot
                            else:
                                sl_level = round(entry_spot + self.sl_points, 2)
                                tp_level = round(entry_spot - self.tp_points, 2)
                                peak_favorable = entry_spot

                            trail_triggered = False
                            in_position = True
                            just_entered = True
                            trade_count += 1
                            pending_entry = False
                            pending_entry_data = {}
                            pending_entry_bar = None
                            filled = True

                            logger.debug(
                                f"{trading_date} {t_str} ENTRY {option_type} "
                                f"strike={option_strike} premium={entry_price} "
                                f"spot={entry_spot} SL_spot={sl_level} TP_spot={tp_level}"
                            )

                            # Check SL/TP on entry candle itself
                            exit_reason = self._check_exit_on_candle(
                                option_type, spot_high, spot_low,
                                sl_level, tp_level, trail_triggered,
                            )
                            if exit_reason:
                                exit_price = self._get_exit_price(opt_candle.iloc[0])
                                trade = self._make_trade(
                                    trading_date, option_type, option_strike,
                                    expiry_date, entry_indicators, entry_spot,
                                    sl_level, tp_level, trail_triggered,
                                    peak_favorable, signal_time, entry_time,
                                    entry_price, t_str, exit_price, exit_reason,
                                )
                                trades.append(trade)
                                in_position = False
                                just_entered = False
                                if exit_reason == "SL":
                                    consecutive_losses += 1
                                elif exit_reason in ("TP", "TRAIL_SL"):
                                    consecutive_losses = 0
                                continue

                    if not filled:
                        # Option data missing — will cancel on next candle
                        continue

            # ============ 2. EXIT CHECKS (1-min spot) ============
            if in_position and not just_entered:
                # Check SL/TP on this candle's spot range (even at EOD)
                exit_reason = self._check_exit_on_candle(
                    option_type, spot_high, spot_low,
                    sl_level, tp_level, trail_triggered,
                )

                # Force exit at EOD if no SL/TP triggered
                if not exit_reason and t_str >= self.force_exit_time:
                    exit_reason = "EOD"

                if exit_reason == "EOD":
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
                                expiry_date, entry_indicators, entry_spot,
                                sl_level, tp_level, trail_triggered,
                                peak_favorable, signal_time, entry_time,
                                entry_price, t_str, exit_price, "EOD",
                            )
                            trades.append(trade)
                            in_position = False
                            continue

                if exit_reason:
                    minute_opts = opt_by_dt.get(t_dt)
                    if minute_opts is not None:
                        opt_candle = minute_opts[
                            (minute_opts["strike"] == option_strike)
                            & (minute_opts["option_type"] == option_type)
                        ]
                        if not opt_candle.empty:
                            exit_price = self._get_exit_price(opt_candle.iloc[0])
                            trade = self._make_trade(
                                trading_date, option_type, option_strike,
                                expiry_date, entry_indicators, entry_spot,
                                sl_level, tp_level, trail_triggered,
                                peak_favorable, signal_time, entry_time,
                                entry_price, t_str, exit_price, exit_reason,
                            )
                            trades.append(trade)
                            in_position = False
                            if exit_reason == "SL":
                                consecutive_losses += 1
                            elif exit_reason in ("TP", "TRAIL_SL"):
                                consecutive_losses = 0
                            continue

                # Update trailing SL (AFTER exit check — conservative)
                sl_level, trail_triggered, peak_favorable = self._update_trailing_sl(
                    option_type, spot_high, spot_low,
                    entry_spot, sl_level, trail_triggered, peak_favorable,
                )

            # ============ 3. SIGNAL DETECTION (5-min boundaries only) ============
            if not in_position and not pending_entry:
                # Only check at 5-min boundaries
                minute = t_dt.minute
                if minute % 5 != 0:
                    continue

                # The indicators at this candle are from the 5-min bar
                # that started 5 minutes ago. Skip if that bar started
                # before the entry window (e.g., 09:15 bar at 09:20).
                bar_start_str = (t_dt - pd.Timedelta(minutes=5)).strftime("%H:%M")
                if bar_start_str < self.entry_start:
                    continue

                # Deduplicate: don't re-signal on same 5-min bar
                bar_key = t_dt
                if bar_key == last_signal_5m_bar:
                    continue

                # Daily limits check
                if trade_count >= self.max_trades_per_day:
                    continue
                if consecutive_losses >= self.max_consecutive_losses:
                    continue

                # Don't signal too late
                latest_signal = pd.Timestamp(
                    f"{trading_date} {self.force_exit_time}",
                    tz="Asia/Kolkata"
                ) - pd.Timedelta(minutes=1)
                if t_dt >= latest_signal:
                    continue

                # Use the 5-min bar's close for signal conditions
                # (not the 1-min candle close which may have drifted)
                bar_close = candle["close_5m"]
                if pd.isna(bar_close):
                    continue

                # EMA flatness filter
                if self._is_ema_flat(ema_s, ema_l, bar_close):
                    continue

                # VWAP offset
                vwap_upper = vwap_val * (1 + self.vwap_offset_pct / 100)
                vwap_lower = vwap_val * (1 - self.vwap_offset_pct / 100)

                sig = None
                if (bar_close > vwap_upper
                        and ema_s > ema_l
                        and rsi_val > self.rsi_upper):
                    # Check strong bullish candle
                    if self._is_strong_candle(candle, "CE"):
                        sig = "CE"
                elif (bar_close < vwap_lower
                        and ema_s < ema_l
                        and rsi_val < self.rsi_lower):
                    if self._is_strong_candle(candle, "PE"):
                        sig = "PE"

                if sig:
                    last_signal_5m_bar = bar_key
                    pending_entry = True
                    pending_entry_bar = t_dt + pd.Timedelta(minutes=1)
                    pending_entry_data = {
                        "option_type": sig,
                        "signal_time": t_str,
                        "indicators": {
                            "vwap": round(float(vwap_val), 2),
                            "ema_s": round(float(ema_s), 2),
                            "ema_l": round(float(ema_l), 2),
                            "rsi": round(float(rsi_val), 2),
                        },
                    }
                    logger.debug(
                        f"{trading_date} {t_str} SIGNAL {sig} "
                        f"bar_close={bar_close:.2f} VWAP={vwap_val:.2f} "
                        f"EMA9={ema_s:.2f} EMA20={ema_l:.2f} RSI={rsi_val:.1f}"
                    )

        # Safety net: force close if still in position at end of day
        if in_position:
            exit_price, exit_time_str = self._last_option_price(
                day_options, option_strike, option_type
            )
            trade = self._make_trade(
                trading_date, option_type, option_strike,
                expiry_date, entry_indicators, entry_spot,
                sl_level, tp_level, trail_triggered,
                peak_favorable, signal_time, entry_time,
                entry_price, exit_time_str, exit_price, "EOD",
            )
            trades.append(trade)

        return trades

    # ------------------------------------------------------------------
    # Exit logic helpers
    # ------------------------------------------------------------------

    def _check_exit_on_candle(
        self, option_type, spot_high, spot_low,
        sl_level, tp_level, trail_triggered,
    ) -> Optional[str]:
        """Check if SL, TP, or trailing SL is hit on a 1-min candle.

        Priority: SL first (conservative), then TP.
        """
        if option_type == "CE":
            sl_hit = spot_low <= sl_level
            tp_hit = spot_high >= tp_level

            if sl_hit and tp_hit:
                return "SL"  # conservative
            if sl_hit:
                return "TRAIL_SL" if trail_triggered else "SL"
            if tp_hit:
                return "TP"

        else:  # PE
            sl_hit = spot_high >= sl_level
            tp_hit = spot_low <= tp_level

            if sl_hit and tp_hit:
                return "SL"
            if sl_hit:
                return "TRAIL_SL" if trail_triggered else "SL"
            if tp_hit:
                return "TP"

        return None

    @staticmethod
    def _get_exit_price(opt_row) -> float:
        """Option exit price approximation (candle close)."""
        return round(opt_row["close"], 2)

    # ------------------------------------------------------------------
    # Trade construction
    # ------------------------------------------------------------------

    def _make_trade(
        self, trading_date, option_type, strike,
        expiry_date, indicators, entry_spot,
        sl_level, tp_level, trail_triggered,
        peak_favorable, signal_time, entry_time,
        entry_price, exit_time, exit_price, exit_reason,
    ) -> VwapEmaRsiTrade:
        pnl_points = round(exit_price - entry_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        pnl_inr = round(pnl_points * self.lot_size, 2)
        return VwapEmaRsiTrade(
            date=str(trading_date),
            option_type=option_type,
            strike=strike,
            expiry_date=str(expiry_date),
            vwap=indicators.get("vwap", 0.0),
            ema_short=indicators.get("ema_s", 0.0),
            ema_long=indicators.get("ema_l", 0.0),
            rsi=indicators.get("rsi", 0.0),
            spot_at_entry=entry_spot,
            sl_level=sl_level,
            tp_level=tp_level,
            trail_triggered=trail_triggered,
            peak_favorable=round(peak_favorable, 2) if peak_favorable else 0.0,
            signal_time=signal_time,
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
