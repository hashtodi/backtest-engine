"""
BB Reversal PE Buy — PineScript-aligned variant.

Same as bb_reversal_backtest.py but with two differences matching the
original NIFTY-DINESH PineScript:

  1. Step 1 reset: ANY close > BB upper while flat resets redCandleFound,
     regardless of candle color (red or green). The original engine only
     resets on a green breakout.

  2. After exit: always resets to IDLE. Needs a fresh close > BB upper
     to re-enter WATCHING. The original engine jumps straight to WATCHING
     if spot is already above BB at exit time.

Everything else is identical: low-based trigger, next-bar PE open entry,
±15pt SL/TP on PE high/low, 09:18-15:19 window, force exit 15:20.
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
from indicators.bollinger import BollingerBands

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Trade dataclass (reuse the same structure)
# ---------------------------------------------------------------------------

@dataclass
class BBReversalPineTrade:
    date: str
    spot_strike: float
    pe_strike: float
    expiry_date: str
    signal_step: str
    signal_time: str             # "HH:MM" when close < redLow (PineScript trigger)
    entry_time: str              # "HH:MM" next candle (PE open)
    entry_price: float           # PE open of next candle
    spot_at_entry: float         # spot open at entry candle
    qty: int
    tp_level: float
    sl_level: float
    exit_time: str
    exit_price: float
    exit_reason: str
    pnl_points: float
    pnl_pct: float
    pnl_inr: float


def trades_to_dataframe(trades: List[BBReversalPineTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


# ---------------------------------------------------------------------------
# Signal State Machine — PineScript-aligned
# ---------------------------------------------------------------------------

class _Phase:
    IDLE = "IDLE"
    WATCHING = "WATCHING"
    RED_FOUND = "RED_FOUND"


class SignalStatePine:
    """PineScript-aligned signal state machine.

    Differs from SignalState in bb_reversal_backtest.py:
    - In RED_FOUND, ANY close > BB upper resets to WATCHING (not just green).
      This matches the PineScript where upperBreakCond fires regardless of
      candle color and sets redCandleFound = false.
    """

    def __init__(self):
        self.phase: str = _Phase.IDLE
        self.red_low: Optional[float] = None

    def reset(self):
        self.phase = _Phase.IDLE
        self.red_low = None


def check_signal_state_pine(
    state: SignalStatePine,
    spot_close: float,
    spot_open: float,
    spot_high: float,
    spot_low: float,
    bb_upper: float,
) -> bool:
    """PineScript-aligned state machine.

    Key difference: in RED_FOUND, ANY close > BB upper (red or green)
    resets to WATCHING and clears redLow.
    """
    is_red = spot_close < spot_open

    if state.phase == _Phase.IDLE:
        if spot_close > bb_upper:
            state.phase = _Phase.WATCHING
        return False

    if state.phase == _Phase.WATCHING:
        if is_red:
            state.phase = _Phase.RED_FOUND
            state.red_low = spot_low
        return False

    if state.phase == _Phase.RED_FOUND:
        # Priority 1: close breaches redLow → signal fires (PineScript uses close)
        if spot_close < state.red_low:
            state.reset()
            return True

        # Priority 2: ANY close > BB upper resets (red or green)
        # This matches PineScript: upperBreakCond doesn't check candle color
        if spot_close > bb_upper:
            state.reset()
            state.phase = _Phase.WATCHING
            state.red_low = None
            return False

        return False

    return False


# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------

class BBReversalPineBacktestEngine:
    """BB Reversal PE-buy — PineScript-aligned variant."""

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
    # Data loading & preparation
    # ------------------------------------------------------------------

    def _load_spot(self) -> pd.DataFrame:
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
        warmup_start = start - pd.Timedelta(days=5)
        df = df[(df["datetime"] >= warmup_start) & (df["datetime"] < end)]
        return df.sort_values("datetime").reset_index(drop=True)

    def _calculate_bb(self, spot_df: pd.DataFrame) -> pd.DataFrame:
        bb = BollingerBands(name="bb", period=self.bb_period, std_dev=self.bb_std)
        result = bb.calculate(spot_df["close"])
        spot_df = spot_df.copy()
        spot_df["bb_upper"] = result["upper"].values
        spot_df["bb_middle"] = result["middle"].values
        spot_df["bb_lower"] = result["lower"].values
        return spot_df

    def _prepare_data(self):
        logger.info("Loading spot data...")
        spot_df = self._load_spot()
        self._spot_1m = self._calculate_bb(spot_df)
        logger.info(f"Spot 1m (with warmup): {len(self._spot_1m):,} rows")

        logger.info("Loading options data...")
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        self._options_1m = load_data(
            options_path, self.start_date, self.end_date, "weekly"
        )
        logger.info(f"Options 1m: {len(self._options_1m):,} rows")

    def _get_atm_strike(self, spot: float) -> float:
        rounding = STRIKE_ROUNDING.get(self.instrument, 50)
        return round(spot / rounding) * rounding

    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[BBReversalPineTrade]:
        self._prepare_data()

        all_dates = sorted(self._options_1m["date"].unique())
        trades: List[BBReversalPineTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))
            day_trades = self._process_day(trading_date)
            trades.extend(day_trades)

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    def _process_day(self, trading_date) -> List[BBReversalPineTrade]:
        day_spot = self._spot_1m[self._spot_1m["date"] == trading_date]
        if day_spot.empty:
            return []

        day_options = self._options_1m[self._options_1m["date"] == trading_date]
        if day_options.empty:
            return []

        expiry_date = get_nearest_weekly_expiry(trading_date)
        if expiry_date is None:
            return []

        trades: List[BBReversalPineTrade] = []

        # State
        signal_state = SignalStatePine()
        in_position = False
        pending_entry_strike = None
        signal_time = None
        pe_strike = None
        entry_price = None
        entry_time = None
        spot_at_entry = None
        tp_level = None
        sl_level = None

        for _, spot_row in day_spot.iterrows():
            t_str = spot_row["time_str"]
            bb_upper = spot_row["bb_upper"]

            if pd.isna(bb_upper):
                continue

            # ============ 0. PENDING ENTRY: enter at this candle's PE open ============
            if pending_entry_strike is not None and not in_position:
                pe_candle = day_options[
                    (day_options["datetime"] == spot_row["datetime"])
                    & (day_options["strike"] == pending_entry_strike)
                    & (day_options["option_type"] == "PE")
                ]

                if not pe_candle.empty:
                    pc = pe_candle.iloc[0]
                    entry_price = pc["open"]
                    pe_strike = pending_entry_strike
                    tp_level = round(entry_price + self.tp_points, 2)
                    sl_level = round(entry_price - self.sl_points, 2)
                    entry_time = t_str
                    spot_at_entry = spot_row["open"]
                    in_position = True
                    signal_state = SignalStatePine()

                pending_entry_strike = None

            # ============ 1. EXIT CHECKS (if in position) ============
            if in_position:
                pe_candle = day_options[
                    (day_options["datetime"] == spot_row["datetime"])
                    & (day_options["strike"] == pe_strike)
                    & (day_options["option_type"] == "PE")
                ]

                if not pe_candle.empty:
                    pc = pe_candle.iloc[0]

                    if pc["low"] <= sl_level:
                        trade = self._make_trade(
                            trading_date, pe_strike, expiry_date,
                            signal_time, entry_time, entry_price, spot_at_entry,
                            tp_level, sl_level,
                            t_str, sl_level, "SL",
                        )
                        trades.append(trade)
                        in_position = False

                    elif pc["high"] >= tp_level:
                        trade = self._make_trade(
                            trading_date, pe_strike, expiry_date,
                            signal_time, entry_time, entry_price, spot_at_entry,
                            tp_level, sl_level,
                            t_str, tp_level, "TP",
                        )
                        trades.append(trade)
                        in_position = False

                    elif t_str >= self.force_exit_time:
                        trade = self._make_trade(
                            trading_date, pe_strike, expiry_date,
                            signal_time, entry_time, entry_price, spot_at_entry,
                            tp_level, sl_level,
                            t_str, pc["close"], "EOD",
                        )
                        trades.append(trade)
                        in_position = False

                elif t_str >= self.force_exit_time:
                    trade = self._make_trade(
                        trading_date, pe_strike, expiry_date,
                        signal_time, entry_time, entry_price, spot_at_entry,
                        tp_level, sl_level,
                        t_str, entry_price, "EOD",
                    )
                    trades.append(trade)
                    in_position = False

                # DIFFERENCE #2: After exit, always reset to IDLE.
                # Needs fresh close > BB upper to start watching again.
                # (Original engine jumps to WATCHING if spot > BB at exit.)
                if not in_position:
                    signal_state = SignalStatePine()

                if in_position:
                    continue

            # ============ 2. SIGNAL DETECTION ============

            if t_str < self.entry_start:
                if signal_state.phase in (_Phase.IDLE, _Phase.WATCHING):
                    prev_phase = signal_state.phase
                    check_signal_state_pine(
                        signal_state,
                        spot_row["close"],
                        spot_row["open"],
                        spot_row["high"],
                        spot_row["low"],
                        bb_upper,
                    )
                    if signal_state.phase == _Phase.WATCHING and prev_phase != _Phase.WATCHING:
                        signal_time = t_str
                continue

            if t_str > self.entry_end:
                continue

            prev_phase = signal_state.phase
            fired = check_signal_state_pine(
                signal_state,
                spot_row["close"],
                spot_row["open"],
                spot_row["high"],
                spot_row["low"],
                bb_upper,
            )

            # Record breakout time: IDLE→WATCHING or RED_FOUND→WATCHING (new breakout)
            if signal_state.phase == _Phase.WATCHING and prev_phase != _Phase.WATCHING:
                signal_time = t_str

            if fired:
                spot_price = spot_row["close"]
                pending_entry_strike = self._get_atm_strike(spot_price)

        # Safety net
        if in_position:
            last_spot = day_spot.iloc[-1]
            pe_candle = day_options[
                (day_options["datetime"] == last_spot["datetime"])
                & (day_options["strike"] == pe_strike)
                & (day_options["option_type"] == "PE")
            ]
            if not pe_candle.empty:
                exit_price = pe_candle.iloc[0]["close"]
            else:
                exit_price = entry_price

            trade = self._make_trade(
                trading_date, pe_strike, expiry_date,
                signal_time, entry_time, entry_price, spot_at_entry,
                tp_level, sl_level,
                last_spot["time_str"], exit_price, "EOD",
            )
            trades.append(trade)

        return trades

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_trade(
        self, trading_date, pe_strike, expiry_date,
        signal_time, entry_time, entry_price, spot_at_entry,
        tp_level, sl_level,
        exit_time, exit_price, exit_reason,
    ) -> BBReversalPineTrade:
        pnl_points = round(exit_price - entry_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        pnl_inr = round(pnl_points * self.lot_size, 2)
        return BBReversalPineTrade(
            date=str(trading_date),
            spot_strike=pe_strike,
            pe_strike=pe_strike,
            expiry_date=str(expiry_date),
            signal_step="break_red_low",
            signal_time=signal_time,
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
