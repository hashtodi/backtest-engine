"""
HA-NR7 Strategy Backtest Engine.

Strategy:
  Alert: Heikin-Ashi neutral candle on 3-min spot (body < 2.5, range > 20)
  Entry: NR7 on ITM option 3-min chart -> buy at next candle open
  Pyramiding: up to 3 lots on same-side NR7
  Reversal: opposite-side NR7 -> close + enter new side
  Exits: SL/TP on 1-min (DTE-based %), EOD at 14:55

No-lookahead: 3-min candle at T known at T+3, entry at T+3 open.
"""

import logging
import math
import os
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

import pandas as pd

from config import (
    DATA_PATH,
    LOT_SIZE,
    NIFTY_WEEKLY_EXPIRY_DATES,
    get_nearest_weekly_expiry,
)
from engine.data_loader import load_data
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


# DTE lookup table: trading_dte -> (tp_pct, sl_pct)
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


def adjust_for_ema(
    tp_pct: float,
    sl_pct: float,
    entry_price: float,
    ema10: float,
    ema21: float,
) -> Tuple[float, float, bool]:
    """Adjust TP and SL based on entry price position relative to EMAs.

    Rules:
      - Above both EMAs AND base TP >= 7.5%: TP → 5%, SL → 7.5% (fixed)
      - Below both EMAs AND base TP <= 7.5%: TP → 10%, SL unchanged
      - Otherwise: no change

    Returns (adjusted_tp_pct, adjusted_sl_pct, was_adjusted).
    """
    if entry_price > ema10 and entry_price > ema21 and tp_pct >= 7.5:
        return 5.0, 7.5, True
    if entry_price < ema10 and entry_price < ema21 and tp_pct <= 7.5:
        return 10.0, min(sl_pct, 10.0), True
    return tp_pct, sl_pct, False


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
    entry_times: str          # stringified list of entry times per lot
    exit_time: str
    option_type: str          # CE / PE
    strike: int
    entry_prices: str         # stringified list of entry prices per lot
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
    def _load_parquet_1m(
        path: str, start_str: str, end_str: str, warmup_days: int = 0
    ):
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
        end = pd.to_datetime(end_str).tz_localize("Asia/Kolkata") + pd.Timedelta(
            days=1
        )
        if warmup_days:
            start = start - pd.Timedelta(days=warmup_days)
        df = df[(df["datetime"] >= start) & (df["datetime"] < end)]
        return df.sort_values("datetime").reset_index(drop=True)

    def _load_and_prepare_spot(self):
        """Load spot 1-min -> resample to 3-min -> compute HA -> detect alerts."""
        path = os.path.join(BASE_DIR, self._SPOT_PATHS[self.instrument])
        logger.info(f"Loading spot data from {path}...")
        # Use 120-day warmup so HA recursive state has enough history
        # (HA_Open depends on all previous candles — more warmup = better convergence with TV)
        spot_1m = self._load_parquet_1m(
            path, self.start_date, self.end_date, warmup_days=120
        )
        logger.info(f"Spot 1m: {len(spot_1m):,} rows")

        # Resample to 3-min CONTINUOUSLY (no per-day grouping)
        # TradingView computes HA continuously across days — daily reset causes
        # divergence of up to 500+ pts. Continuous HA matches TV within ~3-7 pts.
        spot_1m = spot_1m.set_index("datetime")
        spot_3m = (
            spot_1m.resample("3min")
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                }
            )
            .dropna(subset=["open"])
            .reset_index()
        )
        spot_3m["date"] = spot_3m["datetime"].dt.date
        logger.info(f"Spot 3m: {len(spot_3m):,} rows")

        # Compute Heikin-Ashi CONTINUOUSLY (no daily reset — matches TradingView)
        spot_3m = compute_heikin_ashi(spot_3m)

        # Detect HA alerts
        spot_3m["ha_body"] = (spot_3m["ha_close"] - spot_3m["ha_open"]).abs()
        spot_3m["regular_range"] = spot_3m["high"] - spot_3m["low"]
        spot_3m["is_ha_alert"] = (spot_3m["ha_body"] < self.ha_body_threshold) & (
            spot_3m["regular_range"] > self.ha_range_threshold
        )

        # Trim to requested date range (warmup days served their purpose)
        start_d = pd.to_datetime(self.start_date).date()
        spot_3m = spot_3m[spot_3m["date"] >= start_d].reset_index(drop=True)

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
        # Contract grouping: (strike, option_type). Since we filter to weekly
        # expiry_type=WEEK & expiry_code=1, these two columns uniquely identify a contract.
        contract_cols = ["strike", "option_type"]
        ema_short = EMA(name="ema_short", period=self.ema_short_period)
        ema_long = EMA(name="ema_long", period=self.ema_long_period)

        parts = []
        opt_indexed = self._options_1m.set_index("datetime")
        for keys, group in opt_indexed.groupby(contract_cols):
            strike, opt_type = keys
            group = group.sort_index()
            # Resample to 3-min within each day to avoid overnight gaps
            resampled = (
                group.groupby("date")
                .resample("3min")
                .agg(
                    {
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                    }
                )
                .dropna(subset=["open"])
            )
            if resampled.empty:
                continue
            resampled = resampled.reset_index(level="date", drop=True).reset_index()
            resampled["date"] = resampled["datetime"].dt.date

            # NR7 (per contract, independent history)
            resampled["nr7"] = compute_nr7(resampled, lookback=self.nr7_lookback)

            # NR7 breakout level: high of the most recent NR7 candle (persists until next NR7)
            # Matches PineScript: var float rh = na; if NR: rh := high[1]
            resampled["nr7_high"] = resampled["high"].where(resampled["nr7"]).ffill()
            # Breakout UP: close crosses above persisted NR7 high
            # ta.crossover(close, rh) = close > rh AND close[1] <= rh
            resampled["nr7_breakout"] = (
                (resampled["close"] > resampled["nr7_high"])
                & (resampled["close"].shift(1) <= resampled["nr7_high"])
            ).fillna(False)

            # EMA (per contract, independent history)
            resampled["ema_short"] = ema_short.calculate(resampled["close"])
            resampled["ema_long"] = ema_long.calculate(resampled["close"])

            resampled["strike"] = strike
            resampled["option_type"] = opt_type
            parts.append(resampled)

        self._options_3m = (
            pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        )
        logger.info(f"Options 3m: {len(self._options_3m):,} rows")

    def _prepare_data(self):
        """Full data pipeline."""
        self._load_and_prepare_spot()
        self._load_and_prepare_options()
        # Build trading calendar from options data (most reliable — actual market-open days)
        opt_dates = set(self._options_1m["date"].unique())
        spot_dates = set(self._spot_3m["date"].unique())
        self._trading_dates = sorted(opt_dates | spot_dates)

    def _get_trading_dte(self, trade_date) -> int:
        """Count trading days from trade_date (exclusive) to nearest weekly expiry (inclusive)."""
        expiry = get_nearest_weekly_expiry(trade_date)
        if expiry is None:
            return 0
        return sum(1 for d in self._trading_dates if trade_date < d <= expiry)

    # ------------------------------------------------------------------
    # Core day processing
    # ------------------------------------------------------------------

    def _process_day(self, trading_date) -> List[HaNr7Trade]:
        """Process a single trading day. Returns completed trades."""
        day_spot_3m = self._spot_3m[self._spot_3m["date"] == trading_date].sort_values(
            "datetime"
        )
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
        # opt_1m: datetime -> {(strike, option_type): Series}
        opt_1m_by_dt: Dict[pd.Timestamp, Dict[Tuple, pd.Series]] = {}
        for _, row in day_opt_1m.iterrows():
            key = (int(row["strike"]), row["option_type"])
            opt_1m_by_dt.setdefault(row["datetime"], {})[key] = row

        # opt_3m: datetime -> {(strike, option_type): Series}
        opt_3m_by_dt: Dict[pd.Timestamp, Dict[Tuple, pd.Series]] = {}
        for _, row in day_opt_3m.iterrows():
            key = (int(row["strike"]), row["option_type"])
            opt_3m_by_dt.setdefault(row["datetime"], {})[key] = row

        # spot_3m: datetime -> Series
        spot_3m_by_dt = {row["datetime"]: row for _, row in day_spot_3m.iterrows()}

        # Set of 3-min candle datetimes -> boundary = candle_time + 3min
        boundary_times = set()
        for dt_3m in spot_3m_by_dt:
            boundary_times.add(dt_3m + pd.Timedelta(minutes=3))

        # All 1-min timestamps for the day, sorted
        all_1m_times = sorted(opt_1m_by_dt.keys())

        # Time thresholds
        t_start = pd.Timestamp(
            f"{trading_date} {self.trading_start}", tz="Asia/Kolkata"
        )
        t_last_entry = pd.Timestamp(
            f"{trading_date} {self.last_entry}", tz="Asia/Kolkata"
        )
        t_force_exit = pd.Timestamp(
            f"{trading_date} {self.force_exit}", tz="Asia/Kolkata"
        )

        # --- Day state ---
        state = EngineState.IDLE
        trades: List[HaNr7Trade] = []

        # Alert state
        alert_candle_time: Optional[pd.Timestamp] = None
        alert_ce_strike: int = 0
        alert_pe_strike: int = 0
        scan_remaining: int = 0

        # NR7 breakout is now pre-computed in options_3m as 'nr7_breakout' column

        # Position state
        position_option_type: str = ""
        position_strike: int = 0
        position_entry_prices: List[float] = []
        position_entry_times: List[str] = []
        position_avg_entry: float = 0.0
        position_tp_pct: float = 0.0
        position_sl_pct: float = 0.0
        position_tp_level: float = 0.0
        position_sl_level: float = 0.0
        position_ema_adjusted: bool = False
        position_is_reversal: bool = False
        reversal_count: int = 0

        # Pending entry from NR7 detection (fills at next candle open)
        pending_entry_type: Optional[str] = None  # "CE" or "PE"
        pending_reversal_close_price: Optional[float] = None
        pending_is_reversal: bool = False

        def _avg(prices: List[float]) -> float:
            return sum(prices) / len(prices) if prices else 0.0

        def _close_position(
            exit_price: float, exit_time_str: str, exit_reason: str
        ):
            pnl_pts = round(exit_price - position_avg_entry, 2)
            pnl_inr = round(
                pnl_pts * len(position_entry_prices) * self.lot_size, 2
            )
            trade = HaNr7Trade(
                entry_date=str(trading_date),
                alert_candle_time=str(alert_candle_time),
                entry_times=str(position_entry_times),
                exit_time=exit_time_str,
                option_type=position_option_type,
                strike=position_strike,
                entry_prices=str(position_entry_prices),
                avg_entry=round(position_avg_entry, 2),
                num_lots=len(position_entry_prices),
                exit_price=round(exit_price, 2),
                exit_reason=exit_reason,
                tp_pct=position_tp_pct,
                sl_pct=position_sl_pct,
                dte=dte,
                ema_adjusted=position_ema_adjusted,
                is_reversal=position_is_reversal,
                pnl_points=pnl_pts,
                pnl_inr=pnl_inr,
            )
            trades.append(trade)

        def _enter_position(
            option_type: str,
            entry_price: float,
            entry_time_str: str,
            is_reversal: bool,
            ema_s: float,
            ema_l: float,
        ):
            nonlocal state, position_option_type, position_strike
            nonlocal position_entry_prices, position_entry_times, position_avg_entry
            nonlocal position_tp_pct, position_sl_pct
            nonlocal position_tp_level, position_sl_level
            nonlocal position_ema_adjusted, position_is_reversal

            position_option_type = option_type
            position_strike = (
                alert_ce_strike if option_type == "CE" else alert_pe_strike
            )
            position_entry_prices = [entry_price]
            position_entry_times = [entry_time_str]
            position_avg_entry = entry_price
            position_is_reversal = is_reversal

            base_tp, base_sl = get_dte_tp_sl(dte)
            tp_adj, sl_adj, was_adjusted = adjust_for_ema(
                base_tp, base_sl, entry_price, ema_s, ema_l
            )
            position_tp_pct = tp_adj
            position_sl_pct = sl_adj
            position_ema_adjusted = was_adjusted
            position_tp_level = entry_price * (1 + tp_adj / 100)
            position_sl_level = entry_price * (1 - sl_adj / 100)
            state = EngineState.POSITION_OPEN

        def _add_lot(entry_price: float, entry_time_str: str):
            nonlocal position_entry_prices, position_entry_times, position_avg_entry
            nonlocal position_tp_level, position_sl_level
            position_entry_prices.append(entry_price)
            position_entry_times.append(entry_time_str)
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

            # --- SL/TP check (every 1-min bar, while in position) ---
            if state == EngineState.POSITION_OPEN:
                opt_row = opt_1m_by_dt.get(t, {}).get(
                    (position_strike, position_option_type)
                )

                if opt_row is not None:
                    # SL check (priority over TP)
                    if opt_row["low"] <= position_sl_level:
                        _close_position(position_sl_level, time_str, "SL")
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

                # --- Read pre-computed NR7 breakout flag for a contract ---
                def _get_breakout(strike, opt_type):
                    row = opt_3m_by_dt.get(candle_time_3m, {}).get(
                        (strike, opt_type)
                    )
                    if row is None:
                        return False
                    return bool(row.get("nr7_breakout", False))

                ce_breakout = _get_breakout(alert_ce_strike, "CE")
                pe_breakout = _get_breakout(alert_pe_strike, "PE")

                # POSITION_OPEN: check for pyramid / reversal via breakout
                if state == EngineState.POSITION_OPEN and can_enter:
                    same_side_breakout = (
                        ce_breakout
                        if position_option_type == "CE"
                        else pe_breakout
                    )
                    opp_side_breakout = (
                        pe_breakout
                        if position_option_type == "CE"
                        else ce_breakout
                    )
                    opp_type = (
                        "PE" if position_option_type == "CE" else "CE"
                    )

                    if opp_side_breakout and not same_side_breakout:
                        # Reversal trigger
                        if reversal_count >= 1:
                            close_row = opt_3m_by_dt.get(
                                candle_time_3m, {}
                            ).get((position_strike, position_option_type))
                            close_price = (
                                close_row["close"]
                                if close_row is not None
                                else position_avg_entry
                            )
                            _close_position(
                                close_price, time_str, "REVERSAL_STOP"
                            )
                            state = EngineState.DAY_STOPPED
                            continue
                        else:
                            close_row = opt_3m_by_dt.get(
                                candle_time_3m, {}
                            ).get((position_strike, position_option_type))
                            close_price = (
                                close_row["close"]
                                if close_row is not None
                                else position_avg_entry
                            )
                            pending_entry_type = opp_type
                            pending_reversal_close_price = close_price
                            pending_is_reversal = True
                            reversal_count += 1

                    elif (
                        same_side_breakout
                        and not opp_side_breakout
                        and len(position_entry_prices) < 3
                    ):
                        # Pyramid at this boundary bar's open
                        opt_row_now = opt_1m_by_dt.get(t, {}).get(
                            (position_strike, position_option_type)
                        )
                        if opt_row_now is not None:
                            _add_lot(opt_row_now["open"], time_str)

                # ALERT_ACTIVE: scan for NR7 breakout
                if state == EngineState.ALERT_ACTIVE:
                    # Check for new HA alert (replaces current)
                    spot_candle = spot_3m_by_dt.get(candle_time_3m)
                    if (
                        spot_candle is not None
                        and spot_candle["is_ha_alert"]
                    ):
                        spot_close = spot_candle["close"]
                        alert_candle_time = candle_time_3m
                        alert_ce_strike = int(
                            math.floor(spot_close / self.strike_rounding)
                            * self.strike_rounding
                        )
                        alert_pe_strike = int(
                            math.ceil(spot_close / self.strike_rounding)
                            * self.strike_rounding
                        )
                        if alert_ce_strike == alert_pe_strike:
                            alert_ce_strike -= self.strike_rounding
                            alert_pe_strike += self.strike_rounding
                        scan_remaining = self.nr7_scan_window

                    if scan_remaining <= 0:
                        state = EngineState.IDLE
                        continue

                    # Check for breakout entry (both = skip)
                    if ce_breakout and pe_breakout:
                        pass  # Both breakout → skip
                    elif ce_breakout and can_enter:
                        pending_entry_type = "CE"
                        pending_is_reversal = False
                    elif pe_breakout and can_enter:
                        pending_entry_type = "PE"
                        pending_is_reversal = False
                    scan_remaining -= 1

                # IDLE: check for HA alert
                if state == EngineState.IDLE:
                    spot_candle = spot_3m_by_dt.get(candle_time_3m)
                    if (
                        spot_candle is not None
                        and spot_candle["is_ha_alert"]
                    ):
                        spot_close = spot_candle["close"]
                        alert_candle_time = candle_time_3m
                        alert_ce_strike = int(
                            math.floor(spot_close / self.strike_rounding)
                            * self.strike_rounding
                        )
                        alert_pe_strike = int(
                            math.ceil(spot_close / self.strike_rounding)
                            * self.strike_rounding
                        )
                        if alert_ce_strike == alert_pe_strike:
                            alert_ce_strike -= self.strike_rounding
                            alert_pe_strike += self.strike_rounding
                        scan_remaining = self.nr7_scan_window
                        reversal_count = 0
                        state = EngineState.ALERT_ACTIVE

                        # Check breakout on the alert candle itself
                        # (breakout is pre-computed, so just read the flag)
                        if ce_breakout and pe_breakout:
                            pass  # Both → skip
                        elif ce_breakout and can_enter:
                            pending_entry_type = "CE"
                            pending_is_reversal = False
                        elif pe_breakout and can_enter:
                            pending_entry_type = "PE"
                            pending_is_reversal = False
                        scan_remaining -= 1

                # --- Fill pending entry at THIS boundary bar's open ---
                if pending_entry_type is not None:
                    opt_type = pending_entry_type
                    strike = (
                        alert_ce_strike if opt_type == "CE" else alert_pe_strike
                    )
                    opt_row = opt_1m_by_dt.get(t, {}).get((strike, opt_type))
                    if opt_row is not None and can_enter:
                        entry_price = opt_row["open"]
                        # EMA from the candle that triggered the NR7
                        ema_row = opt_3m_by_dt.get(candle_time_3m, {}).get(
                            (strike, opt_type)
                        )
                        ema_s = (
                            ema_row["ema_short"]
                            if ema_row is not None
                            and pd.notna(ema_row.get("ema_short"))
                            else entry_price
                        )
                        ema_l = (
                            ema_row["ema_long"]
                            if ema_row is not None
                            and pd.notna(ema_row.get("ema_long"))
                            else entry_price
                        )

                        if (
                            pending_is_reversal
                            and pending_reversal_close_price is not None
                        ):
                            _close_position(
                                pending_reversal_close_price,
                                time_str,
                                "REVERSAL",
                            )

                        _enter_position(
                            opt_type,
                            entry_price,
                            time_str,
                            is_reversal=pending_is_reversal,
                            ema_s=ema_s,
                            ema_l=ema_l,
                        )
                    elif not can_enter:
                        if (
                            pending_is_reversal
                            and pending_reversal_close_price is not None
                        ):
                            _close_position(
                                pending_reversal_close_price,
                                time_str,
                                "REVERSAL_STOP",
                            )
                            state = EngineState.DAY_STOPPED
                        else:
                            state = EngineState.IDLE

                    pending_entry_type = None
                    pending_reversal_close_price = None
                    pending_is_reversal = False

        # End of day -- force close any open position
        if state == EngineState.POSITION_OPEN and position_entry_prices:
            last_time = all_1m_times[-1] if all_1m_times else t_force_exit
            opt_row = opt_1m_by_dt.get(last_time, {}).get(
                (position_strike, position_option_type)
            )
            if opt_row is not None:
                _close_position(
                    opt_row["close"],
                    last_time.strftime("%Y-%m-%d %H:%M"),
                    "EOD",
                )

        return trades

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

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
