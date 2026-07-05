"""
DEMA-SuperTrend EMA Pullback Backtest Engine.

Strategy:
  Bias (checked every 1-min candle using 5-min indicator values):
    - CE signal: spot close > DEMA(200) AND SuperTrend(12,3) direction == -1 (bullish)
    - PE signal: spot close < DEMA(200) AND SuperTrend(12,3) direction == +1 (bearish)

  Entry:
    - Spot touches EMA(12): CE → candle low <= EMA | PE → candle high >= EMA
    - Enter ATM nearest weekly expiry option at close of that 1-min candle
    - 1 lot, max 1 trade per day (strictly)

  Exit (priority order):
    1. SuperTrend direction flips → exit at option OPEN of next 1-min candle
    2. SL: 30% of premium (all days)
    3. TP: expiry day 30% | expiry-1 day 20% | other days 10%
    4. Force exit at 3:00 PM

  All indicators calculated on 5-min resampled spot data, forward-filled to 1-min.
"""

import logging
import os
from dataclasses import asdict, dataclass
from datetime import time as dtime
from typing import List, Optional

import numpy as np
import pandas as pd

from config import (
    DATA_PATH,
    LOT_SIZE,
    SPOT_DATA_PATH,
    STRIKE_ROUNDING,
    get_expiry_day_type,
    get_nearest_weekly_expiry,
)
from engine.data_loader import load_data
from indicators.dema import DEMA
from indicators.ema import EMA
from indicators.supertrend import SuperTrend

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------

@dataclass
class DemaStTrade:
    date: str                   # "YYYY-MM-DD"
    option_type: str            # "CE" / "PE"
    strike: float
    expiry_date: str
    expiry_day_type: str        # "expiry" / "expiry-1" / "other"

    # Indicators at entry
    dema_200: float
    supertrend_dir: float       # -1=bullish, +1=bearish
    ema_12: float
    spot_at_entry: float

    # Trade
    signal_time: str            # "HH:MM" when DEMA+ST bias first aligned (start of continuous stretch)
    touch_time: str             # "HH:MM" when EMA touch happened (while bias active)
    entry_time: str             # "HH:MM" when entry filled (T+1 open after touch)
    entry_price: float          # option open at T+1
    qty: int
    sl_pct: float
    tp_pct: float               # 10/20/30 by day type

    exit_time: str
    exit_price: float
    exit_reason: str            # "ST_FLIP" / "SL" / "TP" / "EOD"

    pnl_points: float           # exit - entry
    pnl_pct: float              # %
    pnl_inr: float              # points * qty


def trades_to_dataframe(trades: List[DemaStTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class DemaStBacktestEngine:
    """DEMA-SuperTrend EMA(12) Pullback strategy backtest engine."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        dema_period: int = 200,
        supertrend_period: int = 12,
        supertrend_factor: int = 3,
        ema_period: int = 12,
        sl_pct: float = 30.0,
        tp_expiry: float = 30.0,
        tp_expiry_minus1: float = 20.0,
        tp_other: float = 10.0,
        entry_start: str = "09:45",
        entry_end: str = "14:45",
        force_exit_time: str = "15:00",
        instrument: str = "NIFTY",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.dema_period = dema_period
        self.supertrend_period = supertrend_period
        self.supertrend_factor = supertrend_factor
        self.ema_period = ema_period
        self.sl_pct = sl_pct
        self.tp_expiry = tp_expiry
        self.tp_expiry_minus1 = tp_expiry_minus1
        self.tp_other = tp_other
        self.entry_start = entry_start
        self.entry_end = entry_end
        self.force_exit_time = force_exit_time
        self.instrument = instrument
        self.lot_size = LOT_SIZE.get(instrument, 75)

        self._spot_1m: Optional[pd.DataFrame] = None
        self._options_1m: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Data loading & preparation
    # ------------------------------------------------------------------

    def _load_spot(self) -> pd.DataFrame:
        """Load 1-min spot data, parse datetime, filter date range."""
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

        # Filter date range
        start = pd.to_datetime(self.start_date).tz_localize("Asia/Kolkata")
        end = pd.to_datetime(self.end_date).tz_localize("Asia/Kolkata") + pd.Timedelta(days=1)
        # Load extra history for DEMA warmup (200 five-min bars ≈ 15 trading days)
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
        """Calculate DEMA, SuperTrend, EMA on 5-min data."""
        dema_ind = DEMA(name="dema", period=self.dema_period)
        st_ind = SuperTrend(name="st", factor=self.supertrend_factor,
                            atr_period=self.supertrend_period)
        ema_ind = EMA(name="ema", period=self.ema_period)

        spot_5m = spot_5m.copy()
        spot_5m["dema"] = dema_ind.calculate(spot_5m["close"])
        st_result = st_ind.calculate(
            spot_5m["close"],
            high=spot_5m["high"],
            low=spot_5m["low"],
        )
        spot_5m["st_dir"] = st_result["direction"]
        spot_5m["st_val"] = st_result["value"]
        spot_5m["ema"] = ema_ind.calculate(spot_5m["close"])
        return spot_5m

    def _forward_fill_to_1m(self, spot_1m: pd.DataFrame, spot_5m: pd.DataFrame) -> pd.DataFrame:
        """Forward-fill 5-min indicator values onto 1-min candles.

        Shifts indicators by one 5-min bar to avoid lookahead bias:
        the 09:15 bar (close at 09:19) becomes available at 09:20.
        """
        indicator_cols = ["dema", "st_dir", "st_val", "ema"]
        ind_5m = spot_5m[indicator_cols].copy()

        # Shift index forward by 5 minutes: 09:15 bar's values → available from 09:20
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

        # Trim back to actual backtest range (remove warmup days)
        start_dt = pd.to_datetime(self.start_date).date()
        spot_1m = spot_1m[spot_1m["date"] >= start_dt].reset_index(drop=True)
        self._spot_1m = spot_1m

        logger.info("Loading options data...")
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        self._options_1m = load_data(options_path, self.start_date, self.end_date, "weekly")
        logger.info(f"Options 1m: {len(self._options_1m):,} rows")

    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[DemaStTrade]:
        """Run backtest. Returns list of DemaStTrade."""
        self._prepare_data()

        all_dates = sorted(self._spot_1m["date"].unique())
        trades: List[DemaStTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))

            trade = self._process_day(trading_date)
            if trade is not None:
                trades.append(trade)

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    def _process_day(self, trading_date) -> Optional[DemaStTrade]:
        """Process a single trading day. Returns at most 1 trade."""
        day_spot = self._spot_1m[self._spot_1m["date"] == trading_date]
        if day_spot.empty:
            return None

        # Determine day-dependent TP
        expiry_day_type = get_expiry_day_type(trading_date)
        if expiry_day_type == "expiry":
            tp_pct = self.tp_expiry
        elif expiry_day_type == "expiry-1":
            tp_pct = self.tp_expiry_minus1
        else:
            tp_pct = self.tp_other

        # Nearest weekly expiry
        expiry_date = get_nearest_weekly_expiry(trading_date)
        if expiry_date is None:
            return None

        # Day's options data, pre-grouped by datetime for O(1) lookup
        day_options = self._options_1m[self._options_1m["date"] == trading_date]
        if day_options.empty:
            return None
        opt_by_dt = {dt: group for dt, group in day_options.groupby("datetime")}

        # State
        in_position = False
        traded = False
        pending_entry = False
        pending_flip_exit = False
        entry_price = None
        option_strike = None
        option_type = None
        signal_type = None
        sl_price = None
        tp_price = None
        entry_time = None
        signal_time = None
        touch_time = None
        bias_start_time = None     # tracks start of continuous bias stretch
        bias_direction = None      # "CE" or "PE" for current bias
        entry_candle_data = {}
        prev_st_dir = None

        for _, candle in day_spot.iterrows():
            t_str = candle["time_str"]
            t_dt = candle["datetime"]
            spot_close = candle["close"]
            spot_low = candle["low"]
            spot_high = candle["high"]
            dema_val = candle["dema"]
            st_dir = candle["st_dir"]
            ema_val = candle["ema"]

            # Skip if indicators not ready (DEMA warmup)
            if pd.isna(dema_val) or pd.isna(st_dir) or pd.isna(ema_val):
                prev_st_dir = st_dir
                continue

            # ============ 1. PENDING FLIP EXIT ============
            if pending_flip_exit and in_position:
                minute_opts = opt_by_dt.get(t_dt)
                if minute_opts is not None:
                    opt_candle = minute_opts[
                        (minute_opts["strike"] == option_strike) &
                        (minute_opts["option_type"] == option_type)
                    ]
                    if not opt_candle.empty:
                        return self._make_trade(
                            trading_date, option_type, signal_type, option_strike,
                            expiry_date, expiry_day_type, entry_candle_data,
                            signal_time, touch_time, entry_time, entry_price, tp_pct,
                            t_str, round(opt_candle.iloc[0]["open"], 2), "ST_FLIP",
                        )

            # ============ 2. EXIT CHECKS ============
            if in_position and not pending_flip_exit:
                minute_opts = opt_by_dt.get(t_dt)
                if minute_opts is not None:
                    opt_candle = minute_opts[
                        (minute_opts["strike"] == option_strike) &
                        (minute_opts["option_type"] == option_type)
                    ]
                    if not opt_candle.empty:
                        opt_row = opt_candle.iloc[0]

                        # Exit 1: SL — option low breaches SL level (intra-candle, priority)
                        if opt_row["low"] <= sl_price:
                            return self._make_trade(
                                trading_date, option_type, signal_type, option_strike,
                                expiry_date, expiry_day_type, entry_candle_data,
                                signal_time, touch_time, entry_time, entry_price, tp_pct,
                                t_str, round(sl_price, 2), "SL",
                            )

                        # Exit 2: TP — option high reaches TP level (intra-candle)
                        if opt_row["high"] >= tp_price:
                            return self._make_trade(
                                trading_date, option_type, signal_type, option_strike,
                                expiry_date, expiry_day_type, entry_candle_data,
                                signal_time, touch_time, entry_time, entry_price, tp_pct,
                                t_str, round(tp_price, 2), "TP",
                            )

                        # Exit 3: SuperTrend flip — set pending for next candle
                        # (candle-boundary event, checked after intra-candle SL/TP)
                        if (prev_st_dir is not None
                                and not np.isnan(prev_st_dir)
                                and st_dir != prev_st_dir):
                            pending_flip_exit = True

                        # Exit 4: EOD force exit
                        if t_str >= self.force_exit_time:
                            return self._make_trade(
                                trading_date, option_type, signal_type, option_strike,
                                expiry_date, expiry_day_type, entry_candle_data,
                                signal_time, touch_time, entry_time, entry_price, tp_pct,
                                t_str, round(opt_row["close"], 2), "EOD",
                            )

            # ============ 3. PENDING ENTRY (from previous candle's signal) ============
            if pending_entry and not in_position:
                # Skip if at or past force exit time
                if t_str >= self.force_exit_time:
                    pending_entry = False
                    prev_st_dir = st_dir
                    continue

                minute_opts = opt_by_dt.get(t_dt)
                if minute_opts is not None:
                    opt_candle = minute_opts[
                        (minute_opts["strike"] == option_strike) &
                        (minute_opts["option_type"] == option_type)
                    ]
                    if not opt_candle.empty:
                        opt_row = opt_candle.iloc[0]
                        entry_price = round(opt_row["open"], 2)
                        entry_time = t_str

                        sl_price = round(entry_price * (1 - self.sl_pct / 100), 2)
                        tp_price = round(entry_price * (1 + tp_pct / 100), 2)

                        in_position = True
                        pending_entry = False

                        logger.debug(
                            f"{trading_date} {t_str} ENTRY {signal_type} "
                            f"strike={option_strike} premium={entry_price} "
                            f"SL={sl_price} TP={tp_price}"
                        )

                        # Entry is at open — check SL/TP on this candle's range
                        if opt_row["low"] <= sl_price:
                            return self._make_trade(
                                trading_date, option_type, signal_type, option_strike,
                                expiry_date, expiry_day_type, entry_candle_data,
                                signal_time, touch_time, entry_time, entry_price, tp_pct,
                                t_str, round(sl_price, 2), "SL",
                            )
                        if opt_row["high"] >= tp_price:
                            return self._make_trade(
                                trading_date, option_type, signal_type, option_strike,
                                expiry_date, expiry_day_type, entry_candle_data,
                                signal_time, touch_time, entry_time, entry_price, tp_pct,
                                t_str, round(tp_price, 2), "TP",
                            )

                        # Check if ST flipped on this same candle
                        if (prev_st_dir is not None
                                and not np.isnan(prev_st_dir)
                                and st_dir != prev_st_dir):
                            pending_flip_exit = True

            # ============ 4. BIAS + TOUCH DETECTION ============
            if not traded and not in_position and not pending_entry:
                # Time window check
                if t_str < self.entry_start or t_str > self.entry_end:
                    bias_start_time = None
                    bias_direction = None
                    prev_st_dir = st_dir
                    continue

                # Determine bias
                ce_signal = (spot_close > dema_val) and (st_dir == -1)
                pe_signal = (spot_close < dema_val) and (st_dir == 1)

                if not ce_signal and not pe_signal:
                    # Bias off — reset stretch
                    bias_start_time = None
                    bias_direction = None
                    prev_st_dir = st_dir
                    continue

                sig = "CE" if ce_signal else "PE"

                # Track bias stretch: reset if direction changed or new stretch
                if bias_direction != sig or bias_start_time is None:
                    bias_start_time = t_str
                    bias_direction = sig

                # Check EMA touch (bias is active, look for pullback)
                if sig == "CE" and spot_low > ema_val:
                    prev_st_dir = st_dir
                    continue
                if sig == "PE" and spot_high < ema_val:
                    prev_st_dir = st_dir
                    continue

                # Compute EMA-based ATM strike
                rounding = STRIKE_ROUNDING.get(self.instrument, 50)
                ema_atm_strike = round(ema_val / rounding) * rounding

                # Verify strike exists in options data at this minute
                minute_opts = opt_by_dt.get(t_dt)
                if minute_opts is None:
                    prev_st_dir = st_dir
                    continue
                atm_options = minute_opts[
                    (minute_opts["strike"] == ema_atm_strike) &
                    (minute_opts["option_type"] == sig)
                ]
                if atm_options.empty:
                    prev_st_dir = st_dir
                    continue

                # Touch confirmed while bias active — queue entry
                option_strike = ema_atm_strike
                option_type = sig
                signal_type = sig
                signal_time = bias_start_time  # when bias first aligned
                touch_time = t_str             # when EMA touch happened
                pending_entry = True
                traded = True  # day consumed

                entry_candle_data = {
                    "dema": round(float(dema_val), 2),
                    "st_dir": float(st_dir),
                    "ema": round(float(ema_val), 2),
                    "spot_close": round(float(ema_val), 2),
                }

                logger.debug(
                    f"{trading_date} {t_str} SIGNAL {sig} strike={option_strike} "
                    f"(entry queued for next candle)"
                )

            prev_st_dir = st_dir

        # Safety net: force close if still in position at end of data
        if in_position:
            exit_price, exit_time = self._last_option_price(
                day_options, option_strike, option_type
            )
            return self._make_trade(
                trading_date, option_type, signal_type, option_strike,
                expiry_date, expiry_day_type, entry_candle_data,
                signal_time, touch_time, entry_time, entry_price, tp_pct,
                exit_time, exit_price, "EOD",
            )

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_trade(
        self, trading_date, option_type, signal_type, strike,
        expiry_date, expiry_day_type, entry_data,
        signal_time, touch_time, entry_time, entry_price, tp_pct,
        exit_time, exit_price, exit_reason,
    ) -> DemaStTrade:
        pnl_points = round(exit_price - entry_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        pnl_inr = round(pnl_points * self.lot_size, 2)
        return DemaStTrade(
            date=str(trading_date),
            option_type=option_type,
            strike=strike,
            expiry_date=str(expiry_date),
            expiry_day_type=expiry_day_type,
            dema_200=entry_data.get("dema", 0.0),
            supertrend_dir=entry_data.get("st_dir", 0.0),
            ema_12=entry_data.get("ema", 0.0),
            spot_at_entry=entry_data.get("spot_close", 0.0),
            signal_time=signal_time,
            touch_time=touch_time,
            entry_time=entry_time,
            entry_price=entry_price,
            qty=self.lot_size,
            sl_pct=self.sl_pct,
            tp_pct=tp_pct,
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
            (day_options["strike"] == strike) &
            (day_options["option_type"] == option_type)
        ]
        if contract.empty:
            return 0.0, "15:30"
        last = contract.iloc[-1]
        t = last["time_only"]
        time_str = t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)[:5]
        return round(last["close"], 2), time_str
