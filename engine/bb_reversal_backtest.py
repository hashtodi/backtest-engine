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
from indicators.bollinger import BollingerBands

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
    signal_step: str             # "break_red_low" always
    signal_time: str             # "HH:MM" when spot low breached redLow
    entry_time: str              # "HH:MM" next candle (PE open)
    entry_price: float           # PE open of next candle
    spot_at_entry: float         # spot open at entry candle
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


# ---------------------------------------------------------------------------
# Signal State Machine
# ---------------------------------------------------------------------------

class _Phase:
    IDLE = "IDLE"
    WATCHING = "WATCHING"
    RED_FOUND = "RED_FOUND"


class SignalState:
    """
    Tracks the 3-step BB reversal signal progression.

    Phases:
        IDLE       → waiting for spot to close above BB upper
        WATCHING   → spot has closed above BB upper; waiting for a red candle
        RED_FOUND  → first red candle found; waiting for close below red_low
    """

    def __init__(self):
        self.phase: str = _Phase.IDLE
        self.red_low: Optional[float] = None

    def reset(self):
        """Reset to IDLE and clear any stored red_low."""
        self.phase = _Phase.IDLE
        self.red_low = None


def check_signal_state(
    state: SignalState,
    spot_close: float,
    spot_open: float,
    spot_high: float,
    spot_low: float,
    bb_upper: float,
    trigger_on_low: bool = True,
    reset_green_only: bool = True,
) -> bool:
    """
    Advance the signal state machine by one candle.

    Args:
        state:      mutable SignalState object (updated in-place)
        spot_close: spot closing price for this candle
        spot_open:  spot opening price for this candle
        spot_high:  spot high for this candle
        spot_low:   spot low for this candle
        bb_upper:   Bollinger Band upper value for this candle
        trigger_on_low:   True = low < redLow triggers (default).
                          False = close < redLow triggers (PineScript style).
        reset_green_only: True = only green candle above BB resets RED_FOUND (default).
                          False = any candle above BB resets (PineScript style).

    Returns:
        True if the signal fires on this candle, else False.
    """
    is_red = spot_close < spot_open
    is_green = spot_close >= spot_open

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
        # Priority 1: check trigger
        trigger_price = spot_low if trigger_on_low else spot_close
        if trigger_price < state.red_low:
            state.reset()
            return True

        # Priority 2: breakout above BB resets to WATCHING
        if reset_green_only:
            if is_green and spot_close > bb_upper:
                state.reset()
                state.phase = _Phase.WATCHING
                return False
        else:
            if spot_close > bb_upper:
                state.reset()
                state.phase = _Phase.WATCHING
                return False

        return False

    return False


# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------

class BBReversalBacktestEngine:
    """BB Reversal PE-buy strategy backtest engine."""

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
        trigger_on_low: bool = True,
        reset_green_only: bool = True,
        watching_after_exit: bool = True,
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
        self.trigger_on_low = trigger_on_low
        self.reset_green_only = reset_green_only
        self.watching_after_exit = watching_after_exit
        self.instrument = "NIFTY"
        self.lot_size = LOT_SIZE.get("NIFTY", 75)

        self._spot_1m: Optional[pd.DataFrame] = None
        self._options_1m: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Data loading & preparation
    # ------------------------------------------------------------------

    def _load_spot(self) -> pd.DataFrame:
        """Load 1-min spot data with 5-day warmup for BB(20)."""
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
        """Calculate Bollinger Bands on spot close (continuous, no daily reset)."""
        bb = BollingerBands(name="bb", period=self.bb_period, std_dev=self.bb_std)
        result = bb.calculate(spot_df["close"])
        spot_df = spot_df.copy()
        spot_df["bb_upper"] = result["upper"].values
        spot_df["bb_middle"] = result["middle"].values
        spot_df["bb_lower"] = result["lower"].values
        return spot_df

    def _prepare_data(self):
        """Load spot, calculate BB, load options."""
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
        """Get ATM strike from spot price (round to nearest 50)."""
        rounding = STRIKE_ROUNDING.get(self.instrument, 50)
        return round(spot / rounding) * rounding

    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[BBReversalTrade]:
        """Run backtest. Returns list of BBReversalTrade."""
        self._prepare_data()

        # Use trading dates from options data (spot has extra warmup days)
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
        day_spot = self._spot_1m[self._spot_1m["date"] == trading_date]
        if day_spot.empty:
            return []

        day_options = self._options_1m[self._options_1m["date"] == trading_date]
        if day_options.empty:
            return []

        expiry_date = get_nearest_weekly_expiry(trading_date)
        if expiry_date is None:
            return []

        trades: List[BBReversalTrade] = []

        # State
        signal_state = SignalState()
        in_position = False
        pending_entry_strike = None  # ATM strike from signal candle, enter next bar
        signal_time = None           # when close > BB upper (step 1 breakout)
        pe_strike = None
        entry_price = None
        entry_time = None
        spot_at_entry = None
        tp_level = None
        sl_level = None

        for _, spot_row in day_spot.iterrows():
            t_str = spot_row["time_str"]
            bb_upper = spot_row["bb_upper"]

            # Skip candles during BB warmup
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
                    signal_state = SignalState()

                    logger.debug(
                        f"{trading_date} signal={signal_time} entry={t_str} BUY PE "
                        f"strike={pe_strike} at {entry_price} (open) "
                        f"TP={tp_level} SL={sl_level}"
                    )

                pending_entry_strike = None  # consumed, whether filled or not

            # ============ 1. EXIT CHECKS (if in position) ============
            if in_position:
                pe_candle = day_options[
                    (day_options["datetime"] == spot_row["datetime"])
                    & (day_options["strike"] == pe_strike)
                    & (day_options["option_type"] == "PE")
                ]

                if not pe_candle.empty:
                    pc = pe_candle.iloc[0]

                    # SL first (worst case): PE low <= sl_level
                    if pc["low"] <= sl_level:
                        trade = self._make_trade(
                            trading_date, pe_strike, expiry_date,
                            signal_time, entry_time, entry_price, spot_at_entry,
                            tp_level, sl_level,
                            t_str, sl_level, "SL",
                        )
                        trades.append(trade)
                        in_position = False

                    # TP: PE high >= tp_level
                    elif pc["high"] >= tp_level:
                        trade = self._make_trade(
                            trading_date, pe_strike, expiry_date,
                            signal_time, entry_time, entry_price, spot_at_entry,
                            tp_level, sl_level,
                            t_str, tp_level, "TP",
                        )
                        trades.append(trade)
                        in_position = False

                    # EOD: force exit at force_exit_time
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
                    # No PE candle at EOD — exit flat at entry price
                    trade = self._make_trade(
                        trading_date, pe_strike, expiry_date,
                        signal_time, entry_time, entry_price, spot_at_entry,
                        tp_level, sl_level,
                        t_str, entry_price, "EOD",
                    )
                    trades.append(trade)
                    in_position = False

                # After exit: re-seed signal state
                if not in_position:
                    if self.watching_after_exit and spot_row["close"] > bb_upper:
                        signal_state = SignalState()
                        signal_state.phase = _Phase.WATCHING
                        signal_time = t_str
                    else:
                        signal_state = SignalState()

                if in_position:
                    continue  # skip signal detection while in position

            # ============ 2. SIGNAL DETECTION (if not in position) ============

            # Pre-window: build state (IDLE→WATCHING, WATCHING→RED_FOUND)
            # but don't advance RED_FOUND — signal would fire and reset state,
            # wasting the setup before we can enter.
            if t_str < self.entry_start:
                if signal_state.phase in (_Phase.IDLE, _Phase.WATCHING):
                    prev_phase = signal_state.phase
                    check_signal_state(
                        signal_state,
                        spot_row["close"],
                        spot_row["open"],
                        spot_row["high"],
                        spot_row["low"],
                        bb_upper,
                        trigger_on_low=self.trigger_on_low,
                        reset_green_only=self.reset_green_only,
                    )
                    # Record breakout time (IDLE→WATCHING or RED_FOUND→WATCHING)
                    if signal_state.phase == _Phase.WATCHING and prev_phase != _Phase.WATCHING:
                        signal_time = t_str
                continue

            # Past entry window: no new entries
            if t_str > self.entry_end:
                continue

            # Within window: check signal
            prev_phase = signal_state.phase
            fired = check_signal_state(
                signal_state,
                spot_row["close"],
                spot_row["open"],
                spot_row["high"],
                spot_row["low"],
                bb_upper,
                trigger_on_low=self.trigger_on_low,
                reset_green_only=self.reset_green_only,
            )

            # Record breakout time (IDLE→WATCHING or RED_FOUND→WATCHING)
            if signal_state.phase == _Phase.WATCHING and prev_phase != _Phase.WATCHING:
                signal_time = t_str

            if fired:
                # Entry trigger — enter at NEXT candle's PE open
                spot_price = spot_row["close"]
                pending_entry_strike = self._get_atm_strike(spot_price)

        # Safety net: force close if still in position at end of day
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
                exit_price = entry_price  # flat if no data

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
    ) -> BBReversalTrade:
        """Create a BBReversalTrade record (buying PE: profit = exit - entry)."""
        pnl_points = round(exit_price - entry_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        pnl_inr = round(pnl_points * self.lot_size, 2)
        return BBReversalTrade(
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
