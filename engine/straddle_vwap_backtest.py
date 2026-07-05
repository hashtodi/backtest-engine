"""
ATM Straddle VWAP Sell Backtest Engine.

Strategy:
  Sell ATM straddle (CE + PE) at nearest weekly expiry when straddle
  price crosses VWAP from either direction. Entry at VWAP(T-1) value.

  VWAP: calculated on straddle_close (CE close + PE close) with combined
  volume (CE vol + PE vol). Session reset daily, no bands.

  Exit:
    1. SL: straddle_close >= entry x 1.035 -> exit at exact SL level
    2. TP: straddle_close <= entry x 0.98  -> exit at exact TP level
    3. EOD: 14:30 -> exit at straddle_close

  Time: Entry 11:00-14:30. VWAP builds from 09:15.
  Re-entry allowed after exit.
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
    STRIKE_ROUNDING,
    get_nearest_weekly_expiry,
)
from engine.data_loader import load_data

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------

@dataclass
class StraddleVwapTrade:
    date: str                    # "YYYY-MM-DD"
    strike: float
    expiry_date: str

    entry_time: str              # "HH:MM" when crossover detected
    entry_price: float           # VWAP(T-1) value (straddle combined)
    vwap_at_entry: float         # same as entry_price
    straddle_at_entry: float     # straddle_close at crossover candle
    ce_entry_price: float        # CE close at entry for reference
    pe_entry_price: float        # PE close at entry for reference
    qty: int

    tp_level: float              # entry x 0.98
    sl_level: float              # entry x 1.035

    exit_time: str
    exit_price: float            # exact SL/TP level or straddle_close at EOD
    exit_reason: str             # "TP" / "SL" / "EOD"

    pnl_points: float            # entry_price - exit_price (selling)
    pnl_pct: float
    pnl_inr: float               # pnl_points x qty


def trades_to_dataframe(trades: List[StraddleVwapTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class StraddleVwapBacktestEngine:
    """ATM Straddle VWAP sell strategy backtest engine."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        tp_pct: float = 2.0,
        sl_pct: float = 3.5,
        entry_start: str = "11:00",
        force_exit_time: str = "14:30",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.entry_start = entry_start
        self.force_exit_time = force_exit_time
        self.instrument = "NIFTY"
        self.lot_size = LOT_SIZE.get("NIFTY", 75)

        self._options_1m: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _prepare_data(self):
        """Load options data."""
        logger.info("Loading options data...")
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        self._options_1m = load_data(
            options_path, self.start_date, self.end_date, "weekly"
        )
        logger.info(f"Options 1m: {len(self._options_1m):,} rows")

    def _build_per_strike_vwap(self, day_options: pd.DataFrame) -> dict:
        """Build straddle VWAP per strike for the entire day.

        For each strike that has both CE and PE data, builds:
          straddle_close = CE close + PE close
          straddle_volume = CE volume + PE volume
          VWAP = cumulative(straddle_close × straddle_volume) / cumulative(straddle_volume)

        Returns dict: { strike: DataFrame indexed by datetime with columns
            straddle_close, straddle_volume, vwap, ce_close, pe_close, time_str }
        """
        rounding = STRIKE_ROUNDING.get(self.instrument, 50)

        # Get all strikes that have both CE and PE
        strikes = day_options["strike"].unique()

        result = {}
        for strike in strikes:
            ce_data = day_options[
                (day_options["strike"] == strike)
                & (day_options["option_type"] == "CE")
            ].set_index("datetime").sort_index()

            pe_data = day_options[
                (day_options["strike"] == strike)
                & (day_options["option_type"] == "PE")
            ].set_index("datetime").sort_index()

            if ce_data.empty or pe_data.empty:
                continue

            common = ce_data.index.intersection(pe_data.index)
            if len(common) < 3:
                continue

            ce = ce_data.loc[common]
            pe = pe_data.loc[common]

            sdf = pd.DataFrame(index=common)
            sdf["straddle_close"] = (ce["close"] + pe["close"]).round(2)
            sdf["straddle_volume"] = ce["volume"] + pe["volume"]
            sdf["ce_close"] = ce["close"]
            sdf["pe_close"] = pe["close"]
            sdf["time_str"] = common.map(
                lambda dt: dt.strftime("%H:%M") if hasattr(dt, "strftime") else str(dt)[11:16]
            )

            # VWAP on combined straddle (matches TradingView)
            cum_pv = (sdf["straddle_close"] * sdf["straddle_volume"]).cumsum()
            cum_v = sdf["straddle_volume"].cumsum().replace(0, np.nan)
            sdf["vwap"] = (cum_pv / cum_v).ffill()

            result[strike] = sdf

        return result

    def _get_atm_strike(self, spot: float) -> float:
        """Get ATM strike from spot price."""
        rounding = STRIKE_ROUNDING.get(self.instrument, 50)
        return round(spot / rounding) * rounding

    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[StraddleVwapTrade]:
        """Run backtest. Returns list of StraddleVwapTrade."""
        self._prepare_data()

        all_dates = sorted(self._options_1m["date"].unique())
        trades: List[StraddleVwapTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))

            day_trades = self._process_day(trading_date)
            trades.extend(day_trades)

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    def _process_day(self, trading_date) -> List[StraddleVwapTrade]:
        """Process a single trading day. Returns 0 or more trades."""
        day_options = self._options_1m[self._options_1m["date"] == trading_date]
        if day_options.empty:
            return []

        expiry_date = get_nearest_weekly_expiry(trading_date)
        if expiry_date is None:
            return []

        # Build per-strike VWAP for all strikes
        strike_data = self._build_per_strike_vwap(day_options)
        if not strike_data:
            return []

        # Get sorted unique timestamps and spot prices
        opt_by_dt = {dt: grp for dt, grp in day_options.groupby("datetime")}
        all_times = sorted(opt_by_dt.keys())

        trades: List[StraddleVwapTrade] = []

        # State
        in_position = False
        entry_price = None
        entry_strike = None
        tp_level = None
        sl_level = None
        entry_time = None
        ce_entry = None
        pe_entry = None
        straddle_at_entry = None

        # Previous candle values (at ATM strike) for crossover detection
        prev_straddle_close = None
        prev_vwap = None
        prev_atm_strike = None

        for t_dt in all_times:
            minute_opts = opt_by_dt[t_dt]
            spot = minute_opts.iloc[0]["spot"]
            atm_strike = self._get_atm_strike(spot)
            t_str = t_dt.strftime("%H:%M") if hasattr(t_dt, "strftime") else str(t_dt)[11:16]

            # ============ 1. EXIT CHECKS (at fixed entry strike) ============
            if in_position:
                # Use the entry strike's per-strike data for exit monitoring
                if entry_strike in strike_data:
                    sdf = strike_data[entry_strike]
                    if t_dt in sdf.index:
                        fixed_straddle = sdf.loc[t_dt, "straddle_close"]

                        # 1a. SL hit
                        if fixed_straddle >= sl_level:
                            trade = self._make_trade(
                                trading_date, entry_strike, expiry_date,
                                entry_time, entry_price, straddle_at_entry,
                                ce_entry, pe_entry, tp_level, sl_level,
                                t_str, round(sl_level, 2), "SL",
                            )
                            trades.append(trade)
                            in_position = False

                        # 1b. TP hit
                        elif fixed_straddle <= tp_level:
                            trade = self._make_trade(
                                trading_date, entry_strike, expiry_date,
                                entry_time, entry_price, straddle_at_entry,
                                ce_entry, pe_entry, tp_level, sl_level,
                                t_str, round(tp_level, 2), "TP",
                            )
                            trades.append(trade)
                            in_position = False

                        # 1c. EOD force exit
                        elif t_str >= self.force_exit_time:
                            trade = self._make_trade(
                                trading_date, entry_strike, expiry_date,
                                entry_time, entry_price, straddle_at_entry,
                                ce_entry, pe_entry, tp_level, sl_level,
                                t_str, round(fixed_straddle, 2), "EOD",
                            )
                            trades.append(trade)
                            in_position = False

                if in_position:
                    # Update prev values using current ATM for next crossover check
                    if atm_strike in strike_data and t_dt in strike_data[atm_strike].index:
                        row = strike_data[atm_strike].loc[t_dt]
                        prev_straddle_close = row["straddle_close"]
                        prev_vwap = row["vwap"]
                        prev_atm_strike = atm_strike
                    continue

            # ============ 2. ENTRY DETECTION (using current ATM strike's VWAP) ============
            if atm_strike not in strike_data or t_dt not in strike_data[atm_strike].index:
                prev_straddle_close = None
                prev_vwap = None
                prev_atm_strike = None
                continue

            row = strike_data[atm_strike].loc[t_dt]
            sc = row["straddle_close"]
            vwap = row["vwap"]

            if (not in_position
                    and prev_straddle_close is not None
                    and prev_vwap is not None
                    and prev_atm_strike == atm_strike  # same strike for valid crossover
                    and t_str >= self.entry_start
                    and t_str < self.force_exit_time
                    and not pd.isna(vwap)
                    and not pd.isna(prev_vwap)):

                # Crossover: straddle_close crossed VWAP(T-1) from either direction
                cross_above = prev_straddle_close < prev_vwap and sc >= prev_vwap
                cross_below = prev_straddle_close > prev_vwap and sc <= prev_vwap
                crossed = cross_above or cross_below

                if crossed:
                    entry_price = round(prev_vwap, 2)
                    entry_strike = atm_strike
                    tp_level = round(entry_price * (1 - self.tp_pct / 100), 2)
                    sl_level = round(entry_price * (1 + self.sl_pct / 100), 2)
                    entry_time = t_str
                    ce_entry = row["ce_close"]
                    pe_entry = row["pe_close"]
                    straddle_at_entry = sc
                    in_position = True

                    logger.debug(
                        f"{trading_date} {t_str} SELL STRADDLE "
                        f"strike={entry_strike} at VWAP={entry_price} "
                        f"TP={tp_level} SL={sl_level}"
                    )

            prev_straddle_close = sc
            prev_vwap = vwap
            prev_atm_strike = atm_strike

        # Safety net: force close if still in position
        if in_position and entry_strike in strike_data:
            sdf = strike_data[entry_strike]
            if not sdf.empty:
                last_row = sdf.iloc[-1]
                trade = self._make_trade(
                    trading_date, entry_strike, expiry_date,
                    entry_time, entry_price, straddle_at_entry,
                    ce_entry, pe_entry, tp_level, sl_level,
                    last_row["time_str"], round(last_row["straddle_close"], 2), "EOD",
                )
                trades.append(trade)

        return trades

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_trade(
        self, trading_date, strike, expiry_date,
        entry_time, entry_price, straddle_at_entry,
        ce_entry, pe_entry, tp_level, sl_level,
        exit_time, exit_price, exit_reason,
    ) -> StraddleVwapTrade:
        # Selling: profit when price drops
        pnl_points = round(entry_price - exit_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        pnl_inr = round(pnl_points * self.lot_size, 2)
        return StraddleVwapTrade(
            date=str(trading_date),
            strike=strike,
            expiry_date=str(expiry_date),
            entry_time=entry_time,
            entry_price=entry_price,
            vwap_at_entry=entry_price,
            straddle_at_entry=straddle_at_entry,
            ce_entry_price=ce_entry,
            pe_entry_price=pe_entry,
            qty=self.lot_size,
            tp_level=tp_level,
            sl_level=sl_level,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl_points=pnl_points,
            pnl_pct=pnl_pct,
            pnl_inr=pnl_inr,
        )
