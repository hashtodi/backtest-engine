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
        max_holding_mins: int = 30,
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
        self.max_holding_mins = max_holding_mins
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
        entry_dt = None              # datetime of entry (for max holding calc)

        # Pending exit: reason string or None
        pending_exit_reason = None

        # Pending entry: confirmed touch+bounce, fill at next candle's option open
        pending_entry = False
        pending_entry_data = {}      # stores strike, option_type, levels, indicators

        # Bias
        bias = None
        bias_signal_time = None
        prev_st_dir = None
        bias_initialized = False     # set initial bias at entry_start

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

            # ============ 0b. RESOLVE PENDING ENTRY (at this candle's option open) ============
            if pending_entry and not in_position:
                # Skip if at or past force exit time
                if t_str >= self.force_exit_time:
                    pending_entry = False
                    pending_entry_data = {}
                    prev_st_dir = st_dir
                    continue

                ped = pending_entry_data
                # ATM strike based on spot open at this candle (actual price at entry)
                rounding = STRIKE_ROUNDING.get(self.instrument, 50)
                atm_strike = round(candle["open"] / rounding) * rounding

                minute_opts = opt_by_dt.get(t_dt)
                if minute_opts is not None:
                    opt_candle = minute_opts[
                        (minute_opts["strike"] == atm_strike)
                        & (minute_opts["option_type"] == ped["option_type"])
                    ]
                    if not opt_candle.empty:
                        opt_row = opt_candle.iloc[0]
                        entry_price = round(opt_row["open"], 2)
                        entry_spot = round(candle["open"], 2)  # actual spot at entry
                        option_strike = atm_strike
                        option_type = ped["option_type"]
                        tp_level = ped["tp_level"]
                        sl_level = ped["sl_level"]
                        tp_distance = ped["tp_distance"]
                        sl_distance = ped["sl_distance"]
                        signal_time = ped["signal_time"]
                        ready_time = ped["ready_time"]
                        touch_time = ped["touch_time"]
                        entry_time = t_str  # actual fill time is this candle
                        entry_indicators = ped["indicators"]
                        entry_indicators["spot"] = entry_spot

                        in_position = True
                        window_entered = True
                        entry_dt = t_dt
                        pending_entry = False
                        pending_entry_data = {}

                        logger.debug(
                            f"{trading_date} {t_str} ENTRY (filled) {option_type} "
                            f"strike={option_strike} premium={entry_price} "
                            f"TP_spot={tp_level} SL_spot={sl_level}"
                        )

            # ============ 1. EXIT CHECKS: SL → TP (highest priority, before ST flip) ============
            if in_position and pending_exit_reason is None:
                # 1a. SL hit (highest priority)
                if option_type == "CE" and spot_low <= sl_level:
                    pending_exit_reason = "SL"
                elif option_type == "PE" and spot_high >= sl_level:
                    pending_exit_reason = "SL"

                # 1b. TP hit
                if pending_exit_reason is None:
                    if option_type == "CE" and spot_high >= tp_level:
                        pending_exit_reason = "TP"
                    elif option_type == "PE" and spot_low <= tp_level:
                        pending_exit_reason = "TP"

            # ============ 2. BIAS + SUPERTREND FLIP DETECTION ============
            # Only start tracking bias from entry_start (09:30) onwards.
            # Before that, just update prev_st_dir for later flip detection.
            if t_str < self.entry_start:
                prev_st_dir = st_dir
                continue

            # Set initial bias at entry_start from current ST direction
            if not bias_initialized:
                bias = "LONG" if st_dir == -1 else "SHORT"
                bias_signal_time = t_str
                bias_initialized = True

            # Detect ST flips after initialization
            elif (prev_st_dir is not None
                    and not np.isnan(prev_st_dir)
                    and st_dir != prev_st_dir):
                # Update bias
                bias = "LONG" if st_dir == -1 else "SHORT"
                bias_signal_time = t_str

                # Cancel any stale pending entry from the old bias direction
                if pending_entry:
                    pending_entry = False
                    pending_entry_data = {}
                    window_entered = False

                # If in position and no SL/TP already triggered, exit via ST_FLIP
                if in_position and pending_exit_reason is None:
                    pending_exit_reason = "ST_FLIP"

            # ============ 3. MAX HOLDING exit (deferred to next candle) ============
            if (in_position
                    and pending_exit_reason is None
                    and self.max_holding_mins > 0
                    and entry_dt is not None):
                mins_held = (t_dt - entry_dt).total_seconds() / 60
                if mins_held >= self.max_holding_mins:
                    pending_exit_reason = "MAX_BARS"

            # ============ 4. EOD / forced resolve at market close ============
            # If at or past force_exit_time: resolve any pending exit immediately
            # (no next candle to defer to), or force EOD exit.
            if in_position and t_str >= self.force_exit_time:
                # Use pending reason if SL/TP/ST_FLIP was just detected, else EOD
                reason = pending_exit_reason if pending_exit_reason else "EOD"
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
                            entry_price, t_str, exit_price, reason,
                        )
                        trades.append(trade)
                        in_position = False
                        pending_exit_reason = None
                        pending_entry = False
                        pending_entry_data = {}
                        prev_st_dir = st_dir
                        continue

            # ============ 5. 5-MIN WINDOW UPDATE ============
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
                        if (ema_s - ema_l) >= 5 and _tp_dist >= self.min_target:
                            window_ready = True
                    elif bias == "SHORT" and not pd.isna(swing_low):
                        _tp_dist = ema_l - swing_low
                        if (ema_l - ema_s) >= 5 and _tp_dist >= self.min_target:
                            window_ready = True

            # ============ 6. ENTRY DETECTION (touch + bounce → pending entry) ============
            if (not in_position
                    and not pending_entry
                    and pending_exit_reason is None
                    and window_ready
                    and not window_entered
                    and bias is not None
                    and t_str >= self.entry_start
                    and t_str <= self.entry_end
                    and window_ema_l is not None):

                # Touch + bounce check:
                #   Long:  low <= EMA12 AND close >= EMA12 (touched floor, bounced back)
                #   Short: high >= EMA12 AND close <= EMA12 (touched ceiling, rejected)
                bounce = False
                sig_type = None
                if (bias == "LONG"
                        and spot_low <= window_ema_l
                        and spot_close >= window_ema_l):
                    bounce = True
                    sig_type = "CE"
                elif (bias == "SHORT"
                        and spot_high >= window_ema_l
                        and spot_close <= window_ema_l):
                    bounce = True
                    sig_type = "PE"

                if bounce:
                    _entry_spot = window_ema_l

                    if bias == "LONG":
                        _tp_dist = round(window_swing_high - _entry_spot, 2)
                        _sl_dist = round(_tp_dist / self.rr_ratio, 2)
                        _tp_level = round(window_swing_high, 2)
                        _sl_level = round(_entry_spot - _sl_dist, 2)
                    else:
                        _tp_dist = round(_entry_spot - window_swing_low, 2)
                        _sl_dist = round(_tp_dist / self.rr_ratio, 2)
                        _tp_level = round(window_swing_low, 2)
                        _sl_level = round(_entry_spot + _sl_dist, 2)

                    # Queue entry for next candle's option open
                    # Strike will be calculated at fill time using actual spot
                    pending_entry = True
                    pending_entry_data = {
                        "option_type": sig_type,
                        "entry_spot": _entry_spot,
                        "tp_level": _tp_level,
                        "sl_level": _sl_level,
                        "tp_distance": _tp_dist,
                        "sl_distance": _sl_dist,
                        "signal_time": bias_signal_time,
                        "ready_time": current_5m_window_start.strftime("%H:%M") if current_5m_window_start else t_str,
                        "touch_time": t_str,
                        "indicators": {
                            "st_dir": float(st_dir),
                            "st_val": round(float(st_val), 2),
                            "ema_s": round(float(ema_s), 2),
                            "ema_l": round(float(ema_l), 2),
                            "spot": round(float(_entry_spot), 2),
                        },
                    }
                    window_entered = True  # block further touches in this window

                    logger.debug(
                        f"{trading_date} {t_str} TOUCH+BOUNCE {sig_type} "
                        f"EMA12={window_ema_l} (entry queued for next candle)"
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
