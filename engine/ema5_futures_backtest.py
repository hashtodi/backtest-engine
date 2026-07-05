"""
EMA5 Futures Breakout Backtest Engine.

Strategy (Nifty Future — 5 EMA):
  Signal timeframe: configurable (1/3/5 min, default 3).
  EMA(5) and alert/confirmation detected on signal-timeframe candles.
  SL/TP always checked on 1-min candles.

  Alert State (on signal-tf candle):
    EMA(5) completely outside candle's H-L range:
    - Bullish: ema < candle_low
    - Bearish: ema > candle_high

  Confirmation (strictly next signal-tf candle):
    - Bullish: close > alert_high  |  Bearish: close < alert_low
    - If not confirmed -> alert dies

  Entry:
    - At OPEN of first 1-min candle after confirmation candle closes
    - Buy ITM option: ATM(spot) -50 CE / +50 PE

  SL/TP (on futures 1-min):
    - SL: alert candle close +/- sl_buffer (default 5 pts)
    - TP: entry + risk * rr_ratio (default 1:1)

  Exit priority: SL > TP > EOD
  Multiple trades per day. One position at a time.
"""

import logging
import math
import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

import pandas as pd

from config import (
    DATA_PATH,
    FUTURES_DATA_PATH,
    LOT_SIZE,
    SPOT_DATA_PATH,
    STRIKE_ROUNDING,
    get_nearest_weekly_expiry,
)
from engine.data_loader import load_data
from indicators.ema import EMA

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

# State machine constants
_IDLE = 0
_ALERT = 1
_PENDING_ENTRY = 2
_IN_POSITION = 3


# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------

@dataclass
class Ema5FuturesTrade:
    date: str
    direction: str              # "CE" / "PE"
    strike: float
    expiry_date: str

    # Alert candle (signal timeframe)
    alert_time: str
    alert_high: float
    alert_low: float
    alert_close: float
    alert_ema: float

    # Confirmation candle (signal timeframe)
    confirm_time: str
    confirm_close: float

    # Entry
    entry_time: str
    entry_price_futures: float
    entry_price_option: float
    entry_spot_price: float

    # Levels (futures)
    sl_level: float
    tp_level: float

    # Exit
    exit_time: str
    exit_price_futures: float
    exit_price_option: float
    exit_reason: str            # "SL" / "TP" / "EOD"

    # P&L
    pnl_futures_points: float
    pnl_option_points: float
    pnl_option_pct: float
    pnl_inr: float


def trades_to_dataframe(trades: List[Ema5FuturesTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Ema5FuturesBacktestEngine:
    """EMA5 Futures Breakout strategy backtest engine."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        ema_period: int = 5,
        signal_tf: int = 3,
        sl_buffer: float = 5.0,
        rr_ratio: float = 1.0,
        entry_start: str = "09:30",
        force_exit_time: str = "15:00",
        strike_depth: int = 1,
        instrument: str = "NIFTY",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.ema_period = ema_period
        self.signal_tf = signal_tf      # 1, 3, or 5 minutes
        self.sl_buffer = sl_buffer
        self.rr_ratio = rr_ratio
        self.entry_start = entry_start
        self.force_exit_time = force_exit_time
        self.strike_depth = strike_depth
        self.instrument = instrument
        self.lot_size = LOT_SIZE.get(instrument, 75)
        self.strike_rounding = STRIKE_ROUNDING.get(instrument, 50)

    # ------------------------------------------------------------------
    # Data loading & resampling
    # ------------------------------------------------------------------

    def _load_futures(self) -> pd.DataFrame:
        """Load 1-min futures data, parse datetime, filter date range."""
        path = os.path.join(BASE_DIR, FUTURES_DATA_PATH[self.instrument])
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
        warmup_start = start - pd.Timedelta(days=5)
        df = df[(df["datetime"] >= warmup_start) & (df["datetime"] < end)]

        # If multiple contracts at same datetime, keep nearest expiry
        df = df.sort_values(["datetime", "expiry_date"]).drop_duplicates(
            "datetime", keep="first"
        )
        return df.sort_values("datetime").reset_index(drop=True)

    def _resample_futures(self, futures_1m: pd.DataFrame) -> pd.DataFrame:
        """Resample 1-min futures to signal timeframe, market hours only."""
        if self.signal_tf == 1:
            return futures_1m.copy()

        df = futures_1m.set_index("datetime").copy()
        df = df.between_time("09:15", "15:29")
        ohlcv = df.resample(f"{self.signal_tf}min").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna(subset=["close"])

        ohlcv = ohlcv.reset_index()
        ohlcv["date"] = ohlcv["datetime"].dt.date
        ohlcv["time_str"] = ohlcv["datetime"].dt.strftime("%H:%M")
        return ohlcv

    def _calculate_ema(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add EMA column (continuous across days)."""
        ema_ind = EMA(name="ema5", period=self.ema_period)
        df = df.copy()
        df["ema"] = ema_ind.calculate(df["close"]).values
        return df

    def _load_spot(self) -> pd.DataFrame:
        """Load 1-min spot data for strike calculation."""
        path = os.path.join(BASE_DIR, SPOT_DATA_PATH[self.instrument])
        df = pd.read_parquet(path)
        dt = pd.to_datetime(df["datetime"])
        if dt.dt.tz is None:
            dt = dt.dt.tz_localize("Asia/Kolkata")
        else:
            dt = dt.dt.tz_convert("Asia/Kolkata")
        df = df.copy()
        df["datetime"] = dt
        start = pd.to_datetime(self.start_date).tz_localize("Asia/Kolkata")
        end = pd.to_datetime(self.end_date).tz_localize("Asia/Kolkata") + pd.Timedelta(days=1)
        df = df[(df["datetime"] >= start) & (df["datetime"] < end)]
        return df.sort_values("datetime").reset_index(drop=True)

    # ------------------------------------------------------------------
    # Option price lookup
    # ------------------------------------------------------------------

    @staticmethod
    def _get_option_price(
        opt_by_dt: Dict, dt, strike: float, option_type: str, field: str = "close"
    ) -> Optional[float]:
        """Look up a single option field at (datetime, strike, type)."""
        minute_opts = opt_by_dt.get(dt)
        if minute_opts is None:
            return None
        match = minute_opts[
            (minute_opts["strike"] == strike)
            & (minute_opts["option_type"] == option_type)
        ]
        if match.empty:
            return None
        return round(float(match.iloc[0][field]), 2)

    # ------------------------------------------------------------------
    # ITM strike calculation
    # ------------------------------------------------------------------

    def _itm_strike(self, spot_price: float, direction: str) -> float:
        """Return the ITM strike: ATM from spot, then -50 CE / +50 PE."""
        r = self.strike_rounding
        atm = round(spot_price / r) * r
        if direction == "CE":
            return atm - r * self.strike_depth
        else:
            return atm + r * self.strike_depth

    # ------------------------------------------------------------------
    # Core backtest
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[Ema5FuturesTrade]:
        """Run backtest. Returns list of trades."""
        logger.info("Loading futures data...")
        futures_1m = self._load_futures()
        logger.info(f"Futures 1m: {len(futures_1m):,} rows")

        # Resample for signal detection + EMA
        logger.info(f"Resampling to {self.signal_tf}-min for signals...")
        futures_signal = self._resample_futures(futures_1m)
        logger.info(f"Futures {self.signal_tf}m: {len(futures_signal):,} rows")

        logger.info(f"Calculating EMA({self.ema_period}) on {self.signal_tf}-min...")
        futures_signal = self._calculate_ema(futures_signal)

        # Trim to actual backtest range (remove warmup)
        start_dt = pd.to_datetime(self.start_date).date()
        futures_1m = futures_1m[futures_1m["date"] >= start_dt].reset_index(drop=True)
        futures_signal = futures_signal[futures_signal["date"] >= start_dt].reset_index(drop=True)

        logger.info("Loading spot data (for strike calculation)...")
        spot_df = self._load_spot()
        spot_open_map = dict(zip(spot_df["datetime"], spot_df["open"]))
        logger.info(f"Spot 1m: {len(spot_df):,} rows")

        logger.info("Loading options data...")
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        options_df = load_data(options_path, self.start_date, self.end_date, "weekly")
        logger.info(f"Options 1m: {len(options_df):,} rows")

        all_dates = sorted(futures_1m["date"].unique())
        trades: List[Ema5FuturesTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))

            day_1m = futures_1m[futures_1m["date"] == trading_date]
            day_signal = futures_signal[futures_signal["date"] == trading_date]
            day_options = options_df[options_df["date"] == trading_date]
            day_trades = self._process_day(
                trading_date, day_1m, day_signal, day_options, spot_open_map
            )
            trades.extend(day_trades)

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    # ------------------------------------------------------------------
    # Per-day processing
    # ------------------------------------------------------------------

    def _process_day(
        self,
        trading_date,
        day_1m: pd.DataFrame,
        day_signal: pd.DataFrame,
        day_options: pd.DataFrame,
        spot_open_map: Dict,
    ) -> List[Ema5FuturesTrade]:
        """Process a single trading day. Returns 0+ trades.

        Two loops:
          1. Signal-tf candles: detect alerts & confirmations.
             On confirmation, record the datetime of the NEXT 1-min candle
             as the pending entry time.
          2. 1-min candles: execute entries at pending entry time,
             monitor SL/TP every minute.
        """
        if day_1m.empty or day_signal.empty:
            return []

        expiry_date = get_nearest_weekly_expiry(trading_date)
        if expiry_date is None:
            return []

        # Pre-group options by datetime
        opt_by_dt: Dict = {}
        if not day_options.empty:
            opt_by_dt = {dt: grp for dt, grp in day_options.groupby("datetime")}

        # --- Phase 1: scan signal-tf candles for alerts & confirmations ---
        # Build a list of confirmed signals with their entry datetime
        # (the first 1-min candle AFTER the signal-tf confirmation candle closes).
        signals = []  # list of dicts with alert + confirmation data + entry_after_dt

        signal_candles = list(day_signal.itertuples(index=False))
        state = _IDLE
        alert_data = {}

        for i, candle in enumerate(signal_candles):
            t_str = candle.time_str
            ema_val = candle.ema

            if pd.isna(ema_val):
                continue

            # Check confirmation
            if state == _ALERT:
                if alert_data["dir"] == "CE":
                    confirmed = candle.close > alert_data["high"]
                else:
                    confirmed = candle.close < alert_data["low"]

                if confirmed:
                    # Entry at the first 1-min candle after this signal-tf candle closes.
                    entry_after_dt = candle.datetime + pd.Timedelta(minutes=self.signal_tf)

                    signals.append({
                        **alert_data,
                        "confirm_time": t_str,
                        "confirm_close": candle.close,
                        "entry_after_dt": entry_after_dt,
                    })
                    state = _IDLE
                    continue
                else:
                    state = _IDLE
                    # Fall through to check if this candle is a new alert

            # Check for new alert
            if state == _IDLE:
                if t_str < self.entry_start or t_str >= self.force_exit_time:
                    continue

                if ema_val < candle.low:
                    alert_data = {
                        "dir": "CE",
                        "high": candle.high,
                        "low": candle.low,
                        "close": candle.close,
                        "ema": ema_val,
                        "time": t_str,
                    }
                    state = _ALERT
                elif ema_val > candle.high:
                    alert_data = {
                        "dir": "PE",
                        "high": candle.high,
                        "low": candle.low,
                        "close": candle.close,
                        "ema": ema_val,
                        "time": t_str,
                    }
                    state = _ALERT

        if not signals:
            return []

        # --- Phase 2: iterate 1-min candles for entry execution & SL/TP ---
        trades: List[Ema5FuturesTrade] = []
        sig_idx = 0          # next signal to consume
        pos_state = _IDLE    # _IDLE or _IN_POSITION

        # Position state
        entry_time = ""
        entry_fut = entry_opt = entry_spot = 0.0
        opt_strike = 0.0
        opt_type = ""
        sl_level = tp_level = 0.0
        cur_alert = {}       # alert data for the current/pending trade

        candles_1m = list(day_1m.itertuples(index=False))

        for candle in candles_1m:
            t_str = candle.time_str
            t_dt = candle.datetime

            # ========== 1. ENTRY — if this is the entry candle for next signal ==========
            if (pos_state == _IDLE
                    and sig_idx < len(signals)
                    and t_dt >= signals[sig_idx]["entry_after_dt"]):

                sig = signals[sig_idx]
                sig_idx += 1

                if t_str >= self.force_exit_time:
                    # Too late to enter
                    continue

                entry_fut = candle.open
                alert_dir = sig["dir"]

                # SL / risk / TP
                if alert_dir == "CE":
                    sl_level = sig["close"] - self.sl_buffer
                    risk = entry_fut - sl_level
                else:
                    sl_level = sig["close"] + self.sl_buffer
                    risk = sl_level - entry_fut

                if risk <= 0:
                    continue  # invalid risk, skip to next signal

                if alert_dir == "CE":
                    tp_level = entry_fut + risk * self.rr_ratio
                else:
                    tp_level = entry_fut - risk * self.rr_ratio

                opt_type = alert_dir
                spot_open = spot_open_map.get(t_dt)
                if spot_open is None:
                    continue

                opt_strike = self._itm_strike(spot_open, opt_type)
                opt_open = self._get_option_price(
                    opt_by_dt, t_dt, opt_strike, opt_type, "open"
                )
                if opt_open is None:
                    continue

                entry_opt = opt_open
                entry_spot = spot_open
                entry_time = t_str
                cur_alert = sig
                pos_state = _IN_POSITION

                logger.debug(
                    f"{trading_date} {t_str} ENTRY {opt_type} "
                    f"strike={opt_strike} fut={entry_fut} spot={entry_spot} "
                    f"opt={entry_opt} SL={sl_level:.1f} TP={tp_level:.1f}"
                )
                # Fall through to check SL/TP on this same candle

            # ========== 2. IN POSITION — check SL/TP on 1-min futures ==========
            if pos_state == _IN_POSITION:
                exited = False
                alert_dir = cur_alert["dir"]

                if alert_dir == "CE":
                    sl_hit = candle.low <= sl_level
                    tp_hit = candle.high >= tp_level
                else:
                    sl_hit = candle.high >= sl_level
                    tp_hit = candle.low <= tp_level

                if sl_hit:
                    exit_opt = self._get_option_price(
                        opt_by_dt, t_dt, opt_strike, opt_type, "close"
                    )
                    trades.append(self._make_trade(
                        trading_date, expiry_date, cur_alert, opt_strike,
                        entry_time, entry_fut, entry_opt, entry_spot,
                        sl_level, tp_level,
                        t_str, sl_level, exit_opt or 0.0, "SL",
                    ))
                    pos_state = _IDLE
                    exited = True

                elif tp_hit:
                    exit_opt = self._get_option_price(
                        opt_by_dt, t_dt, opt_strike, opt_type, "close"
                    )
                    trades.append(self._make_trade(
                        trading_date, expiry_date, cur_alert, opt_strike,
                        entry_time, entry_fut, entry_opt, entry_spot,
                        sl_level, tp_level,
                        t_str, tp_level, exit_opt or 0.0, "TP",
                    ))
                    pos_state = _IDLE
                    exited = True

                elif t_str >= self.force_exit_time:
                    exit_opt = self._get_option_price(
                        opt_by_dt, t_dt, opt_strike, opt_type, "close"
                    )
                    trades.append(self._make_trade(
                        trading_date, expiry_date, cur_alert, opt_strike,
                        entry_time, entry_fut, entry_opt, entry_spot,
                        sl_level, tp_level,
                        t_str, candle.close, exit_opt or 0.0, "EOD",
                    ))
                    pos_state = _IDLE
                    exited = True

                if exited:
                    # After exit, skip any signals whose entry_after_dt is in the past
                    while (sig_idx < len(signals)
                           and signals[sig_idx]["entry_after_dt"] <= t_dt):
                        sig_idx += 1

        # Safety: force close if still in position
        if pos_state == _IN_POSITION and candles_1m:
            last = candles_1m[-1]
            exit_opt = self._get_option_price(
                opt_by_dt, last.datetime, opt_strike, opt_type, "close"
            )
            trades.append(self._make_trade(
                trading_date, expiry_date, cur_alert, opt_strike,
                entry_time, entry_fut, entry_opt, entry_spot,
                sl_level, tp_level,
                last.time_str, last.close, exit_opt or 0.0, "EOD",
            ))

        return trades

    # ------------------------------------------------------------------
    # Trade builder
    # ------------------------------------------------------------------

    def _make_trade(
        self,
        trading_date,
        expiry_date,
        alert: Dict,
        strike: float,
        entry_time: str,
        entry_fut: float,
        entry_opt: float,
        entry_spot: float,
        sl_level: float,
        tp_level: float,
        exit_time: str,
        exit_fut: float,
        exit_opt: float,
        exit_reason: str,
    ) -> Ema5FuturesTrade:
        direction = alert["dir"]
        if direction == "CE":
            pnl_fut = round(exit_fut - entry_fut, 2)
        else:
            pnl_fut = round(entry_fut - exit_fut, 2)

        pnl_opt = round(exit_opt - entry_opt, 2)
        pnl_opt_pct = round(pnl_opt / entry_opt * 100, 3) if entry_opt else 0.0
        pnl_inr = round(pnl_opt * self.lot_size, 2)

        return Ema5FuturesTrade(
            date=str(trading_date),
            direction=direction,
            strike=strike,
            expiry_date=str(expiry_date),
            alert_time=alert["time"],
            alert_high=round(alert["high"], 2),
            alert_low=round(alert["low"], 2),
            alert_close=round(alert["close"], 2),
            alert_ema=round(alert["ema"], 2),
            confirm_time=alert.get("confirm_time", ""),
            confirm_close=round(alert.get("confirm_close", 0.0), 2),
            entry_time=entry_time,
            entry_price_futures=round(entry_fut, 2),
            entry_price_option=round(entry_opt, 2),
            entry_spot_price=round(entry_spot, 2),
            sl_level=round(sl_level, 2),
            tp_level=round(tp_level, 2),
            exit_time=exit_time,
            exit_price_futures=round(exit_fut, 2),
            exit_price_option=round(exit_opt, 2),
            exit_reason=exit_reason,
            pnl_futures_points=pnl_fut,
            pnl_option_points=pnl_opt,
            pnl_option_pct=pnl_opt_pct,
            pnl_inr=pnl_inr,
        )
