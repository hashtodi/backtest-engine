"""
Boom SMA Pullback Backtest Engine.

Strategy:
  Signal (T's close vs T's indicators, no lag):
    - Option close > SMA(13) AND close > ST(4,11) value AND close > ST(3,10) value
    - Buy direction only, exclusive (1 trade at a time)

  Entry (T-1 lag fix):
    - Signal at candle T → limit order at SMA[T]
    - Check from T+1: low[T+1] <= SMA[T] <= high[T+1] → enter at SMA[T]
    - If not filled: limit updates to SMA[T+1], check at T+2
    - Implementation: opt_sma_13_prev = previous candle's SMA = limit order price

  Exit (T-1 lag fix):
    1. ST(3,10) flips direction → exit at option OPEN of next candle
    2. SL: previous candle's ST(3,10) value, capped at max% below entry
    3. TP: dynamic, entry + (entry - SL) * ratio
    4. EOD force exit
    5. Daily loss cap
"""

import logging
import math
import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config import DATA_PATH, LOT_SIZE
from engine.data_loader import calculate_indicators, load_data

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------

@dataclass
class BoomSmaTrade:
    date: str
    option_type: str            # "CE" / "PE"
    strike: float
    expiry_type: str
    expiry_code: int

    signal_time: str            # "HH:MM"
    entry_time: str             # "HH:MM"
    entry_price: float          # SMA value at fill

    sma_at_entry: float
    st_sl_at_entry: float       # ST(3,10) value at entry
    st_signal_at_entry: float   # ST(4,11) value at signal

    exit_time: str
    exit_price: float
    exit_reason: str            # "ST_FLIP" / "SL" / "TP" / "EOD" / "DAILY_LOSS"
    sl_at_exit: float
    tp_at_exit: float

    qty: int
    pnl_points: float
    pnl_pct: float
    pnl_inr: float


def trades_to_dataframe(trades: List[BoomSmaTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BoomSmaBacktestEngine:
    """Boom SMA Pullback strategy with T-1 lag fixes."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        sma_period: int = 13,
        st_signal_factor: int = 4,
        st_signal_atr: int = 11,
        st_sl_factor: int = 3,
        st_sl_atr: int = 10,
        tp_ratio: float = 1.0,
        max_sl_pct: float = 20.0,
        max_loss_pct_per_day: float = 20.0,
        trading_start: str = "09:30",
        trading_end: str = "14:45",
        instrument: str = "NIFTY",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.sma_period = sma_period
        self.st_signal_factor = st_signal_factor
        self.st_signal_atr = st_signal_atr
        self.st_sl_factor = st_sl_factor
        self.st_sl_atr = st_sl_atr
        self.tp_ratio = tp_ratio
        self.max_sl_pct = max_sl_pct
        self.max_loss_pct_per_day = max_loss_pct_per_day
        self.trading_start = trading_start
        self.trading_end = trading_end
        self.instrument = instrument
        self.lot_size = LOT_SIZE.get(instrument, 75)

        # Build indicator column names
        self.sma_col = f"opt_sma_{sma_period}"
        self.sma_prev = f"opt_sma_{sma_period}_prev"
        self.st_sig_val = f"opt_st_{st_signal_factor}_{st_signal_atr}_value"
        self.st_sl_val = f"opt_st_{st_sl_factor}_{st_sl_atr}_value"
        self.st_sl_val_prev = f"opt_st_{st_sl_factor}_{st_sl_atr}_value_prev"
        self.st_sl_dir = f"opt_st_{st_sl_factor}_{st_sl_atr}_direction"
        self.st_sl_dir_prev = f"opt_st_{st_sl_factor}_{st_sl_atr}_direction_prev"

        self._df: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _prepare_data(self):
        """Load options data and calculate indicators."""
        indicator_configs = [
            {
                "type": "SMA",
                "name": self.sma_col,
                "price_source": "option",
                "period": self.sma_period,
            },
            {
                "type": "SUPERTREND",
                "name": f"opt_st_{self.st_signal_factor}_{self.st_signal_atr}",
                "price_source": "option",
                "factor": self.st_signal_factor,
                "atr_period": self.st_signal_atr,
            },
            {
                "type": "SUPERTREND",
                "name": f"opt_st_{self.st_sl_factor}_{self.st_sl_atr}",
                "price_source": "option",
                "factor": self.st_sl_factor,
                "atr_period": self.st_sl_atr,
            },
        ]

        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        logger.info("Loading options data...")
        df = load_data(options_path, self.start_date, self.end_date, "weekly")
        logger.info(f"Options: {len(df):,} rows")

        logger.info("Calculating indicators...")
        df = calculate_indicators(df, indicator_configs)
        self._df = df

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_contract_candle(self, minute_data, strike, option_type):
        """Find specific contract candle in minute data."""
        match = minute_data[
            (minute_data["strike"] == strike) &
            (minute_data["option_type"] == option_type)
        ]
        return match.iloc[0] if len(match) > 0 else None

    def _check_signal(self, row) -> bool:
        """Check signal conditions: close > SMA AND close > ST_signal AND close > ST_sl."""
        close = row["close"]
        sma = row.get(self.sma_col)
        st_sig = row.get(self.st_sig_val)
        st_sl = row.get(self.st_sl_val)

        if any(v is None or (isinstance(v, float) and math.isnan(v))
               for v in [sma, st_sig, st_sl]):
            return False

        return close > sma and close > st_sig and close > st_sl

    def _make_trade(
        self, date, option_type, strike, expiry_type, expiry_code,
        signal_time, entry_time, entry_price,
        sma_at_entry, st_sl_at_entry, st_signal_at_entry,
        exit_time, exit_price, exit_reason, sl_at_exit, tp_at_exit,
    ) -> BoomSmaTrade:
        pnl_points = round(exit_price - entry_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        pnl_inr = round(pnl_points * self.lot_size, 2)
        return BoomSmaTrade(
            date=str(date),
            option_type=option_type,
            strike=strike,
            expiry_type=expiry_type,
            expiry_code=expiry_code,
            signal_time=signal_time,
            entry_time=entry_time,
            entry_price=round(entry_price, 2),
            sma_at_entry=round(sma_at_entry, 2),
            st_sl_at_entry=round(st_sl_at_entry, 2),
            st_signal_at_entry=round(st_signal_at_entry, 2),
            exit_time=exit_time,
            exit_price=round(exit_price, 2),
            exit_reason=exit_reason,
            sl_at_exit=round(sl_at_exit, 2) if sl_at_exit else 0.0,
            tp_at_exit=round(tp_at_exit, 2) if tp_at_exit else 0.0,
            qty=self.lot_size,
            pnl_points=pnl_points,
            pnl_pct=pnl_pct,
            pnl_inr=pnl_inr,
        )

    # ------------------------------------------------------------------
    # Core backtest
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[BoomSmaTrade]:
        """Run backtest. Returns list of BoomSmaTrade."""
        self._prepare_data()

        all_dates = sorted(self._df["date"].unique())
        trades: List[BoomSmaTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))

            day_trades = self._process_day(trading_date)
            trades.extend(day_trades)

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    def _process_day(self, trading_date) -> List[BoomSmaTrade]:
        """Process a single trading day. Returns list of trades."""
        day_data = self._df[self._df["date"] == trading_date]
        if day_data.empty:
            return []

        minutes = sorted(day_data["datetime"].unique())
        data_by_dt = {dt: day_data[day_data["datetime"] == dt] for dt in minutes}

        trades: List[BoomSmaTrade] = []
        day_pnl_pct = 0.0
        day_loss_hit = False

        # Position state
        in_position = False
        pending_entry = False
        pending_flip_exit = False
        entry_price = None
        entry_time = None
        signal_time = None
        tracked_strike = None
        tracked_type = None
        tracked_expiry_type = None
        tracked_expiry_code = None
        sma_at_entry = None
        st_sl_at_entry = None
        st_signal_at_entry = None

        for minute in minutes:
            t = pd.Timestamp(minute)
            t_str = t.strftime("%H:%M")
            t_only = t.time()

            # Skip outside trading hours
            if t_str < self.trading_start or t_str > self.trading_end:
                continue

            is_exit_time = t_str >= self.trading_end
            minute_data = data_by_dt.get(t)
            if minute_data is None or minute_data.empty:
                continue

            # ----- 1. PENDING FLIP EXIT (from previous candle's detection) -----
            if pending_flip_exit and in_position:
                candle = self._get_contract_candle(minute_data, tracked_strike, tracked_type)
                if candle is not None:
                    exit_price = candle["open"]
                    trade = self._make_trade(
                        trading_date, tracked_type, tracked_strike,
                        tracked_expiry_type, tracked_expiry_code,
                        signal_time, entry_time, entry_price,
                        sma_at_entry, st_sl_at_entry, st_signal_at_entry,
                        t_str, exit_price, "ST_FLIP", 0.0, 0.0,
                    )
                    trades.append(trade)
                    day_pnl_pct += trade.pnl_pct
                    if self.max_loss_pct_per_day and day_pnl_pct <= -self.max_loss_pct_per_day:
                        day_loss_hit = True

                in_position = False
                pending_flip_exit = False
                pending_entry = False
                entry_price = None

            # ----- 2. EXIT CHECKS (SL/TP/EOD using T-1 indicators) -----
            if in_position and not pending_flip_exit:
                candle = self._get_contract_candle(minute_data, tracked_strike, tracked_type)
                if candle is not None:
                    # SL from previous candle's ST(3,10) value
                    sl_raw = candle.get(self.st_sl_val_prev)
                    sl_level = None
                    tp_level = None

                    if sl_raw is not None and not (isinstance(sl_raw, float) and math.isnan(sl_raw)):
                        if sl_raw >= entry_price:
                            # ST ratcheted above entry (trailing stop in profit)
                            # No TP needed — any pullback to ST exits with profit
                            sl_level = sl_raw
                        else:
                            # Normal case: ST below entry
                            max_sl = entry_price * (1 - self.max_sl_pct / 100)
                            sl_level = max(sl_raw, max_sl)
                            # Dynamic TP
                            sl_distance = entry_price - sl_level
                            tp_level = entry_price + sl_distance * self.tp_ratio

                    # Check SL
                    if sl_level is not None and candle["low"] <= sl_level:
                        trade = self._make_trade(
                            trading_date, tracked_type, tracked_strike,
                            tracked_expiry_type, tracked_expiry_code,
                            signal_time, entry_time, entry_price,
                            sma_at_entry, st_sl_at_entry, st_signal_at_entry,
                            t_str, sl_level, "SL", sl_level, tp_level or 0.0,
                        )
                        trades.append(trade)
                        day_pnl_pct += trade.pnl_pct
                        in_position = False
                        entry_price = None
                        if self.max_loss_pct_per_day and day_pnl_pct <= -self.max_loss_pct_per_day:
                            day_loss_hit = True
                        continue

                    # Check TP
                    if tp_level is not None and candle["high"] >= tp_level:
                        trade = self._make_trade(
                            trading_date, tracked_type, tracked_strike,
                            tracked_expiry_type, tracked_expiry_code,
                            signal_time, entry_time, entry_price,
                            sma_at_entry, st_sl_at_entry, st_signal_at_entry,
                            t_str, tp_level, "TP", sl_level or 0.0, tp_level,
                        )
                        trades.append(trade)
                        day_pnl_pct += trade.pnl_pct
                        in_position = False
                        entry_price = None
                        continue

                    # Check EOD
                    if is_exit_time:
                        trade = self._make_trade(
                            trading_date, tracked_type, tracked_strike,
                            tracked_expiry_type, tracked_expiry_code,
                            signal_time, entry_time, entry_price,
                            sma_at_entry, st_sl_at_entry, st_signal_at_entry,
                            t_str, candle["close"], "EOD",
                            sl_level or 0.0, tp_level or 0.0,
                        )
                        trades.append(trade)
                        day_pnl_pct += trade.pnl_pct
                        in_position = False
                        entry_price = None
                        continue

                    # ST flip detection (for NEXT candle's exit)
                    st_dir = candle.get(self.st_sl_dir)
                    st_dir_prev = candle.get(self.st_sl_dir_prev)
                    if (st_dir is not None and st_dir_prev is not None
                            and not math.isnan(st_dir) and not math.isnan(st_dir_prev)
                            and st_dir != st_dir_prev):
                        pending_flip_exit = True

            # ----- 3. PENDING ENTRY (limit order from previous candle's signal) -----
            if pending_entry and not in_position and not is_exit_time and not day_loss_hit:
                candle = self._get_contract_candle(minute_data, tracked_strike, tracked_type)
                if candle is not None:
                    # Limit price = previous candle's SMA (the order we placed)
                    limit_price = candle.get(self.sma_prev)

                    if limit_price is not None and not (isinstance(limit_price, float) and math.isnan(limit_price)):
                        if candle["low"] <= limit_price <= candle["high"]:
                            # Entry filled
                            entry_price = round(limit_price, 2)
                            entry_time = t_str
                            sma_at_entry = limit_price
                            st_sl_val = candle.get(self.st_sl_val_prev)
                            st_sl_at_entry = st_sl_val if st_sl_val and not math.isnan(st_sl_val) else 0.0
                            in_position = True
                            pending_entry = False

                            # Check if ST flipped on this same candle (entry + flip)
                            st_dir = candle.get(self.st_sl_dir)
                            st_dir_prev = candle.get(self.st_sl_dir_prev)
                            if (st_dir is not None and st_dir_prev is not None
                                    and not math.isnan(st_dir) and not math.isnan(st_dir_prev)
                                    and st_dir != st_dir_prev):
                                pending_flip_exit = True

                            continue

                    # Signal still valid? Re-check conditions on this candle
                    if not self._check_signal(candle):
                        pending_entry = False
                        tracked_strike = None
                        tracked_type = None

            # ----- 4. SIGNAL DETECTION (T's close vs T's indicators, no lag) -----
            if (not in_position and not pending_entry
                    and not is_exit_time and not day_loss_hit):
                atm_data = minute_data[minute_data["moneyness"] == "ATM"]
                for _, row in atm_data.iterrows():
                    if self._check_signal(row):
                        pending_entry = True
                        tracked_strike = row["strike"]
                        tracked_type = row["option_type"]
                        tracked_expiry_type = row["expiry_type"]
                        tracked_expiry_code = row["expiry_code"]
                        signal_time = t_str
                        st_signal_at_entry = row.get(self.st_sig_val, 0.0)
                        break  # exclusive, first one wins

        # Safety net: force close if still in position
        if in_position:
            last_candle = self._find_last_contract_candle(
                day_data, tracked_strike, tracked_type
            )
            if last_candle is not None:
                trade = self._make_trade(
                    trading_date, tracked_type, tracked_strike,
                    tracked_expiry_type, tracked_expiry_code,
                    signal_time, entry_time, entry_price,
                    sma_at_entry, st_sl_at_entry, st_signal_at_entry,
                    last_candle["time_only"].strftime("%H:%M") if hasattr(last_candle["time_only"], "strftime") else "15:30",
                    last_candle["close"], "EOD", 0.0, 0.0,
                )
                trades.append(trade)

        return trades

    @staticmethod
    def _find_last_contract_candle(day_data, strike, option_type):
        """Get the last available candle for a contract on this day."""
        contract = day_data[
            (day_data["strike"] == strike) &
            (day_data["option_type"] == option_type)
        ]
        return contract.iloc[-1] if len(contract) > 0 else None
