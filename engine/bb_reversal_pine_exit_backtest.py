"""
BB Reversal PE Buy — PineScript-aligned with spot-based exit.

Same signal logic as bb_reversal_pine_backtest.py:
  - close-based trigger (close < redLow)
  - ANY close > BB upper resets setup
  - After exit: always IDLE

Exit difference: SL/TP on SPOT price (not PE price):
  - Track highestHigh from breakout through entry
  - SL = highestHigh (spot high breaches → exit)
  - TP = entry_spot - 2 × (highestHigh - entry_spot) (1:2 RR)
  - When spot SL/TP is hit, exit PE at next candle's PE open
  - Force exit at 15:20 at PE close
  - Same candle SL + TP on spot → take SL
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
class BBReversalPineExitTrade:
    date: str
    spot_strike: float
    pe_strike: float
    expiry_date: str
    signal_step: str
    signal_time: str
    entry_time: str
    entry_price: float           # PE open at entry candle
    spot_at_entry: float         # spot open at entry candle
    highest_high: float          # highest high during setup
    spot_sl: float               # = highest_high
    spot_tp: float               # = spot_at_entry - 2 * risk
    risk_points: float           # highest_high - spot_at_entry
    qty: int
    exit_time: str
    exit_price: float            # PE price at exit
    exit_reason: str             # "SL" / "TP" / "EOD"
    pnl_points: float            # exit_price - entry_price (buying PE)
    pnl_pct: float
    pnl_inr: float


def trades_to_dataframe(trades: List[BBReversalPineExitTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


# ---------------------------------------------------------------------------
# Signal State Machine — same as pine variant
# ---------------------------------------------------------------------------

class _Phase:
    IDLE = "IDLE"
    WATCHING = "WATCHING"
    RED_FOUND = "RED_FOUND"


class SignalStatePine:
    def __init__(self):
        self.phase: str = _Phase.IDLE
        self.red_low: Optional[float] = None
        self.highest_high: Optional[float] = None

    def reset(self):
        self.phase = _Phase.IDLE
        self.red_low = None
        self.highest_high = None


def check_signal_state_pine(
    state: SignalStatePine,
    spot_close: float,
    spot_open: float,
    spot_high: float,
    spot_low: float,
    bb_upper: float,
) -> bool:
    """PineScript-aligned state machine with highestHigh tracking."""
    is_red = spot_close < spot_open

    if state.phase == _Phase.IDLE:
        if spot_close > bb_upper:
            state.phase = _Phase.WATCHING
            state.highest_high = spot_high
        return False

    # Track highest high while watching/waiting (and flat)
    if state.highest_high is not None:
        state.highest_high = max(state.highest_high, spot_high)

    if state.phase == _Phase.WATCHING:
        if is_red:
            state.phase = _Phase.RED_FOUND
            state.red_low = spot_low
        return False

    if state.phase == _Phase.RED_FOUND:
        if spot_close < state.red_low:
            # Signal fires — don't reset highest_high yet, engine will read it
            return True

        if spot_close > bb_upper:
            state.phase = _Phase.WATCHING
            state.red_low = None
            # highest_high continues tracking (not reset on new breakout)
            return False

        return False

    return False


# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------

class BBReversalPineExitBacktestEngine:
    """BB Reversal PE-buy with spot-based highestHigh SL and 1:2 TP."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        rr_ratio: float = 2.0,
        bb_period: int = 20,
        bb_std: float = 2.0,
        entry_start: str = "09:18",
        entry_end: str = "15:19",
        force_exit_time: str = "15:20",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.rr_ratio = rr_ratio
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.entry_start = entry_start
        self.entry_end = entry_end
        self.force_exit_time = force_exit_time
        self.instrument = "NIFTY"
        self.lot_size = LOT_SIZE.get("NIFTY", 75)

        self._spot_1m: Optional[pd.DataFrame] = None
        self._options_1m: Optional[pd.DataFrame] = None

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

    def run(self, progress_callback=None) -> List[BBReversalPineExitTrade]:
        self._prepare_data()

        all_dates = sorted(self._options_1m["date"].unique())
        trades: List[BBReversalPineExitTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))
            day_trades = self._process_day(trading_date)
            trades.extend(day_trades)

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    def _process_day(self, trading_date) -> List[BBReversalPineExitTrade]:
        day_spot = self._spot_1m[self._spot_1m["date"] == trading_date]
        if day_spot.empty:
            return []

        day_options = self._options_1m[self._options_1m["date"] == trading_date]
        if day_options.empty:
            return []

        expiry_date = get_nearest_weekly_expiry(trading_date)
        if expiry_date is None:
            return []

        trades: List[BBReversalPineExitTrade] = []

        # State
        signal_state = SignalStatePine()
        in_position = False
        pending_entry_strike = None
        pending_highest_high = None
        signal_time = None
        pe_strike = None
        entry_price = None
        entry_time = None
        spot_at_entry = None
        highest_high = None
        spot_sl = None
        spot_tp = None
        risk_points = None
        # When spot SL/TP is hit, we exit PE at NEXT candle's PE open
        pending_exit_reason = None

        for _, spot_row in day_spot.iterrows():
            t_str = spot_row["time_str"]
            bb_upper = spot_row["bb_upper"]

            if pd.isna(bb_upper):
                continue

            # ============ PENDING PE EXIT: exit at this candle's PE open ============
            if pending_exit_reason is not None and in_position:
                pe_candle = day_options[
                    (day_options["datetime"] == spot_row["datetime"])
                    & (day_options["strike"] == pe_strike)
                    & (day_options["option_type"] == "PE")
                ]

                if not pe_candle.empty:
                    exit_price = pe_candle.iloc[0]["open"]
                else:
                    exit_price = entry_price  # flat if no data

                trade = self._make_trade(
                    trading_date, pe_strike, expiry_date,
                    signal_time, entry_time, entry_price, spot_at_entry,
                    highest_high, spot_sl, spot_tp, risk_points,
                    t_str, exit_price, pending_exit_reason,
                )
                trades.append(trade)
                in_position = False
                pending_exit_reason = None
                signal_state = SignalStatePine()

            # ============ PENDING ENTRY: enter at this candle's PE open ============
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
                    entry_time = t_str
                    spot_at_entry = spot_row["open"]
                    highest_high = pending_highest_high

                    # Risk calc on spot open of entry candle
                    risk_points = round(highest_high - spot_at_entry, 2)
                    if risk_points <= 0:
                        # highestHigh <= entry spot — invalid setup, skip
                        pending_entry_strike = None
                        pending_highest_high = None
                        continue

                    spot_sl = highest_high
                    spot_tp = round(spot_at_entry - self.rr_ratio * risk_points, 2)
                    in_position = True
                    signal_state = SignalStatePine()

                    logger.debug(
                        f"{trading_date} signal={signal_time} entry={t_str} BUY PE "
                        f"strike={pe_strike} @ {entry_price} | "
                        f"spot={spot_at_entry} HH={highest_high} "
                        f"risk={risk_points} SL={spot_sl} TP={spot_tp}"
                    )

                pending_entry_strike = None
                pending_highest_high = None

            # ============ EXIT CHECKS on SPOT (if in position) ============
            if in_position:
                # SL: spot high >= spot_sl (price went UP, bad for our PE buy)
                # TP: spot low <= spot_tp (price went DOWN, good for our PE buy)
                # Same candle both hit → SL

                if spot_row["high"] >= spot_sl:
                    # SL hit on spot — exit PE at next candle's PE open
                    pending_exit_reason = "SL"

                elif spot_row["low"] <= spot_tp:
                    # TP hit on spot — exit PE at next candle's PE open
                    pending_exit_reason = "TP"

                elif t_str >= self.force_exit_time:
                    # EOD — exit PE at PE close now
                    pe_candle = day_options[
                        (day_options["datetime"] == spot_row["datetime"])
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
                        highest_high, spot_sl, spot_tp, risk_points,
                        t_str, exit_price, "EOD",
                    )
                    trades.append(trade)
                    in_position = False
                    signal_state = SignalStatePine()

                if in_position:
                    continue

            # ============ SIGNAL DETECTION ============
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

            if signal_state.phase == _Phase.WATCHING and prev_phase != _Phase.WATCHING:
                signal_time = t_str

            if fired:
                spot_price = spot_row["close"]
                pending_entry_strike = self._get_atm_strike(spot_price)
                pending_highest_high = signal_state.highest_high
                # State was reset by check_signal_state_pine on fire,
                # but we captured highest_high before reset

        # Safety net: force close if still in position
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
                highest_high, spot_sl, spot_tp, risk_points,
                last_spot["time_str"], exit_price, "EOD",
            )
            trades.append(trade)

        return trades

    def _make_trade(
        self, trading_date, pe_strike, expiry_date,
        signal_time, entry_time, entry_price, spot_at_entry,
        highest_high, spot_sl, spot_tp, risk_points,
        exit_time, exit_price, exit_reason,
    ) -> BBReversalPineExitTrade:
        pnl_points = round(exit_price - entry_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        pnl_inr = round(pnl_points * self.lot_size, 2)
        return BBReversalPineExitTrade(
            date=str(trading_date),
            spot_strike=pe_strike,
            pe_strike=pe_strike,
            expiry_date=str(expiry_date),
            signal_step="break_red_low",
            signal_time=signal_time,
            entry_time=entry_time,
            entry_price=entry_price,
            spot_at_entry=spot_at_entry,
            highest_high=highest_high,
            spot_sl=spot_sl,
            spot_tp=spot_tp,
            risk_points=risk_points,
            qty=self.lot_size,
            exit_time=exit_time,
            exit_price=round(exit_price, 2),
            exit_reason=exit_reason,
            pnl_points=pnl_points,
            pnl_pct=pnl_pct,
            pnl_inr=pnl_inr,
        )
