"""
Boom ST Entry Backtest Engine.

Strategy:
  Signal (T's close vs T's indicators, no lag):
    - Option close > SMA(13) AND close > ST(4,11) value AND close > ST(3,10) value
    - Buy direction only
    - CE and PE tracked independently (can overlap)

  Entry (T-1 lag fix):
    - Signal at candle T → limit at T's ST(3,10) value
    - Check from T+1: low[T+1] <= ST_prev <= high[T+1] → enter at option open of T+1
    - Implementation: opt_st_3_10_value_prev = T's ST value

  Exit:
    - SL: fixed % below entry
    - TP: fixed % above entry
    - EOD force exit
"""

import logging
import math
import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

import pandas as pd

from config import DATA_PATH, LOT_SIZE
from engine.data_loader import calculate_indicators, load_data

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


@dataclass
class BoomStTrade:
    date: str
    option_type: str
    strike: float
    expiry_type: str
    expiry_code: int

    signal_time: str
    entry_time: str
    entry_price: float

    sma_at_signal: float
    st_sl_at_signal: float
    st_signal_at_signal: float

    exit_time: str
    exit_price: float
    exit_reason: str            # "SL" / "TP" / "EOD"

    sl_pct: float
    tp_pct: float
    qty: int
    pnl_points: float
    pnl_pct: float
    pnl_inr: float


def trades_to_dataframe(trades: List[BoomStTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


class BoomStBacktestEngine:
    """Boom ST Entry strategy with T-1 lag fixes. CE and PE independent."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        sma_period: int = 13,
        st_signal_factor: int = 4,
        st_signal_atr: int = 11,
        st_entry_factor: int = 3,
        st_entry_atr: int = 10,
        sl_pct: float = 5.0,
        tp_pct: float = 7.5,
        trading_start: str = "09:30",
        trading_end: str = "14:45",
        instrument: str = "NIFTY",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.sma_period = sma_period
        self.st_signal_factor = st_signal_factor
        self.st_signal_atr = st_signal_atr
        self.st_entry_factor = st_entry_factor
        self.st_entry_atr = st_entry_atr
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct
        self.trading_start = trading_start
        self.trading_end = trading_end
        self.instrument = instrument
        self.lot_size = LOT_SIZE.get(instrument, 75)

        # Column names
        self.sma_col = f"opt_sma_{sma_period}"
        self.sma_prev = f"opt_sma_{sma_period}_prev"
        self.st_sig_val = f"opt_st_{st_signal_factor}_{st_signal_atr}_value"
        self.st_entry_val = f"opt_st_{st_entry_factor}_{st_entry_atr}_value"
        self.st_entry_val_prev = f"opt_st_{st_entry_factor}_{st_entry_atr}_value_prev"
        self.st_entry_dir = f"opt_st_{st_entry_factor}_{st_entry_atr}_direction"

        self._df: Optional[pd.DataFrame] = None

    def _prepare_data(self):
        indicator_configs = [
            {"type": "SMA", "name": self.sma_col, "price_source": "option", "period": self.sma_period},
            {"type": "SUPERTREND", "name": f"opt_st_{self.st_signal_factor}_{self.st_signal_atr}",
             "price_source": "option", "factor": self.st_signal_factor, "atr_period": self.st_signal_atr},
            {"type": "SUPERTREND", "name": f"opt_st_{self.st_entry_factor}_{self.st_entry_atr}",
             "price_source": "option", "factor": self.st_entry_factor, "atr_period": self.st_entry_atr},
        ]
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        df = load_data(options_path, self.start_date, self.end_date, "weekly")
        df = calculate_indicators(df, indicator_configs)
        self._df = df

    def _check_signal(self, row) -> bool:
        close = row["close"]
        sma = row.get(self.sma_col)
        st_sig = row.get(self.st_sig_val)
        st_entry = row.get(self.st_entry_val)
        if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in [sma, st_sig, st_entry]):
            return False
        return close > sma and close > st_sig and close > st_entry

    def _get_contract_candle(self, minute_data, strike, option_type):
        match = minute_data[
            (minute_data["strike"] == strike) & (minute_data["option_type"] == option_type)
        ]
        return match.iloc[0] if len(match) > 0 else None

    def _make_trade(self, date, opt_type, strike, exp_type, exp_code,
                    signal_time, entry_time, entry_price,
                    sma_val, st_sl_val, st_sig_val,
                    exit_time, exit_price, exit_reason) -> BoomStTrade:
        pnl_points = round(exit_price - entry_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        return BoomStTrade(
            date=str(date), option_type=opt_type, strike=strike,
            expiry_type=exp_type, expiry_code=exp_code,
            signal_time=signal_time, entry_time=entry_time,
            entry_price=round(entry_price, 2),
            sma_at_signal=round(sma_val, 2),
            st_sl_at_signal=round(st_sl_val, 2),
            st_signal_at_signal=round(st_sig_val, 2),
            exit_time=exit_time, exit_price=round(exit_price, 2),
            exit_reason=exit_reason,
            sl_pct=self.sl_pct, tp_pct=self.tp_pct, qty=self.lot_size,
            pnl_points=pnl_points, pnl_pct=pnl_pct,
            pnl_inr=round(pnl_points * self.lot_size, 2),
        )

    def run(self, progress_callback=None) -> List[BoomStTrade]:
        self._prepare_data()
        all_dates = sorted(self._df["date"].unique())
        trades: List[BoomStTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))
            trades.extend(self._process_day(trading_date))

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    def _process_day(self, trading_date) -> List[BoomStTrade]:
        day_data = self._df[self._df["date"] == trading_date]
        if day_data.empty:
            return []

        minutes = sorted(day_data["datetime"].unique())
        data_by_dt = {dt: day_data[day_data["datetime"] == dt] for dt in minutes}
        trades: List[BoomStTrade] = []

        # Independent CE and PE state
        tracks = {}
        for ot in ("CE", "PE"):
            tracks[ot] = {
                "in_position": False, "pending_entry": False,
                "entry_price": None, "sl_price": None, "tp_price": None,
                "strike": None, "expiry_type": None, "expiry_code": None,
                "signal_time": None, "entry_time": None,
                "sma_val": 0.0, "st_sl_val": 0.0, "st_sig_val": 0.0,
            }

        for minute in minutes:
            t = pd.Timestamp(minute)
            t_str = t.strftime("%H:%M")
            if t_str < self.trading_start or t_str > self.trading_end:
                continue

            is_exit_time = t_str >= self.trading_end
            minute_data = data_by_dt.get(t)
            if minute_data is None or minute_data.empty:
                continue

            for ot in ("CE", "PE"):
                tr = tracks[ot]

                # ----- 1. EXIT CHECKS -----
                if tr["in_position"]:
                    candle = self._get_contract_candle(minute_data, tr["strike"], ot)
                    if candle is not None:
                        # SL
                        if candle["low"] <= tr["sl_price"]:
                            trades.append(self._make_trade(
                                trading_date, ot, tr["strike"], tr["expiry_type"], tr["expiry_code"],
                                tr["signal_time"], tr["entry_time"], tr["entry_price"],
                                tr["sma_val"], tr["st_sl_val"], tr["st_sig_val"],
                                t_str, tr["sl_price"], "SL",
                            ))
                            tr["in_position"] = False
                            tr["entry_price"] = None
                            continue
                        # TP
                        if candle["high"] >= tr["tp_price"]:
                            trades.append(self._make_trade(
                                trading_date, ot, tr["strike"], tr["expiry_type"], tr["expiry_code"],
                                tr["signal_time"], tr["entry_time"], tr["entry_price"],
                                tr["sma_val"], tr["st_sl_val"], tr["st_sig_val"],
                                t_str, tr["tp_price"], "TP",
                            ))
                            tr["in_position"] = False
                            tr["entry_price"] = None
                            continue
                        # EOD
                        if is_exit_time:
                            trades.append(self._make_trade(
                                trading_date, ot, tr["strike"], tr["expiry_type"], tr["expiry_code"],
                                tr["signal_time"], tr["entry_time"], tr["entry_price"],
                                tr["sma_val"], tr["st_sl_val"], tr["st_sig_val"],
                                t_str, candle["close"], "EOD",
                            ))
                            tr["in_position"] = False
                            tr["entry_price"] = None
                            continue

                # ----- 2. PENDING ENTRY -----
                if tr["pending_entry"] and not tr["in_position"]:
                    if is_exit_time:
                        tr["pending_entry"] = False
                        continue

                    candle = self._get_contract_candle(minute_data, tr["strike"], ot)
                    if candle is not None:
                        # Limit = previous candle's ST(3,10) value
                        limit_price = candle.get(self.st_entry_val_prev)
                        if limit_price is not None and not (isinstance(limit_price, float) and math.isnan(limit_price)):
                            if candle["low"] <= limit_price <= candle["high"]:
                                # Limit order fills at the limit price (ST value)
                                tr["entry_price"] = round(limit_price, 2)
                                tr["entry_time"] = t_str
                                tr["sl_price"] = round(tr["entry_price"] * (1 - self.sl_pct / 100), 2)
                                tr["tp_price"] = round(tr["entry_price"] * (1 + self.tp_pct / 100), 2)
                                tr["in_position"] = True
                                tr["pending_entry"] = False

                                # Check SL/TP on entry candle (price can move after limit fill)
                                if candle["low"] <= tr["sl_price"]:
                                    trades.append(self._make_trade(
                                        trading_date, ot, tr["strike"], tr["expiry_type"], tr["expiry_code"],
                                        tr["signal_time"], tr["entry_time"], tr["entry_price"],
                                        tr["sma_val"], tr["st_sl_val"], tr["st_sig_val"],
                                        t_str, tr["sl_price"], "SL",
                                    ))
                                    tr["in_position"] = False
                                    tr["entry_price"] = None
                                elif candle["high"] >= tr["tp_price"]:
                                    trades.append(self._make_trade(
                                        trading_date, ot, tr["strike"], tr["expiry_type"], tr["expiry_code"],
                                        tr["signal_time"], tr["entry_time"], tr["entry_price"],
                                        tr["sma_val"], tr["st_sl_val"], tr["st_sig_val"],
                                        t_str, tr["tp_price"], "TP",
                                    ))
                                    tr["in_position"] = False
                                    tr["entry_price"] = None
                                continue

                        # Cancel only if ST(3,10) flipped bearish
                        st_dir = candle.get(self.st_entry_dir)
                        if st_dir is not None and not (isinstance(st_dir, float) and math.isnan(st_dir)):
                            if st_dir != -1:  # not bullish → cancel
                                tr["pending_entry"] = False

                # ----- 3. SIGNAL DETECTION -----
                if not tr["in_position"] and not tr["pending_entry"] and not is_exit_time:
                    atm_rows = minute_data[
                        (minute_data["moneyness"] == "ATM") & (minute_data["option_type"] == ot)
                    ]
                    for _, row in atm_rows.iterrows():
                        if self._check_signal(row):
                            tr["pending_entry"] = True
                            tr["strike"] = row["strike"]
                            tr["expiry_type"] = row["expiry_type"]
                            tr["expiry_code"] = row["expiry_code"]
                            tr["signal_time"] = t_str
                            tr["sma_val"] = row.get(self.sma_col, 0.0)
                            tr["st_sl_val"] = row.get(self.st_entry_val, 0.0)
                            tr["st_sig_val"] = row.get(self.st_sig_val, 0.0)
                            break

        # Safety net: force close any open positions
        for ot in ("CE", "PE"):
            tr = tracks[ot]
            if tr["in_position"]:
                contract = day_data[
                    (day_data["strike"] == tr["strike"]) & (day_data["option_type"] == ot)
                ]
                if not contract.empty:
                    last = contract.iloc[-1]
                    trades.append(self._make_trade(
                        trading_date, ot, tr["strike"], tr["expiry_type"], tr["expiry_code"],
                        tr["signal_time"], tr["entry_time"], tr["entry_price"],
                        tr["sma_val"], tr["st_sl_val"], tr["st_sig_val"],
                        last["time_only"].strftime("%H:%M") if hasattr(last["time_only"], "strftime") else "15:30",
                        last["close"], "EOD",
                    ))

        return trades
