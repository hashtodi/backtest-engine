"""
PRT Strategy Backtest Engine.

Strategy (CE — bullish entry, all 4 on resampled spot candle):
  1. SuperTrend bullish (direction == -1)
  2. MACD histogram > 0 (state, not crossover)
  3. Spot candle close < VWAP
  4. PCR > threshold OR PCR uptrending (30-min change > 0)

Vice versa for PE (bearish).

Entry: 1 lot ATM option at next 1-min bar OPEN after resampled candle close.
Exits: TP / SL on option premium (immediate) or EOD.

No-lookahead: indicators forward-filled with +timeframe shift,
PCR uses T-1 (pcr_prev), entry/exit at OPEN of boundary bar.

Capital model: fixed 1-lot sizing.
roi_pct = pnl_inr / initial_capital × 100 (denominator never changes).
equity_after tracks running balance (initial_capital + cumulative pnl).
"""

import logging
import os
from dataclasses import asdict, dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from config import (
    DATA_PATH,
    STRIKE_ROUNDING,
    get_nearest_weekly_expiry,
)
from engine.data_loader import load_data
from indicators.macd import MACD
from indicators.supertrend import SuperTrend

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.dirname(__file__))


@dataclass
class PrtTrade:
    """Single PRT strategy trade (1 lot, TP/SL/EOD exit only)."""

    date: str
    option_type: str          # CE / PE
    strike: float
    expiry_date: str

    # Signal context
    signal_time: str
    st_direction: float
    macd_histogram: float
    candle_close: float       # resampled candle close (futures or spot per indicator_source)
    vwap_value: float
    pcr_value: float
    pcr_change: float

    # Entry
    spot_open: float          # spot open at entry bar (used for ATM strike calc)
    entry_time: str
    entry_price: float        # option OPEN
    entry_lots: int

    # Exit
    exit_time: str
    exit_price: float
    exit_lots: int
    exit_reason: str          # TP, SL, EOD

    # P&L
    pnl_points: float
    pnl_pct: float
    pnl_inr: float

    # Capital / ROI (fixed 1-lot sizing, fixed initial-capital denominator)
    equity_after: float       # running equity after this trade closes
    roi_pct: float            # pnl_inr / initial_capital * 100


def trades_to_dataframe(trades: List[PrtTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


class PrtBacktestEngine:
    """PRT strategy backtest engine."""

    # Default data paths per instrument
    _FUTURES_PATHS = {
        "NIFTY": "data/futures/NIFTY_FUT_NEAR_1m.parquet",
    }
    _SPOT_PCR_PATHS = {
        "NIFTY": "data/spot/nifty/NIFTY_1m_with_pcr.parquet",
    }

    def __init__(
        self,
        start_date: str,
        end_date: str,
        indicator_source: str = "futures",
        spot_timeframe: int = 5,
        st_period: int = 10,
        st_factor: float = 3.0,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        tp_pct: float = 30.0,
        sl_pct: float = 30.0,
        initial_capital: float = 25000.0,
        pcr_threshold: float = 1.0,
        pcr_lookback: int = 30,
        pcr_trend_min: float = 0.1,
        trading_start: str = "09:30",
        trading_end: str = "14:30",
        max_trades_per_day: int = 0,
        instrument: str = "NIFTY",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.indicator_source = indicator_source  # "futures" or "spot"
        self.spot_timeframe = spot_timeframe
        self.st_period = st_period
        self.st_factor = st_factor
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal_period = macd_signal
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.lots_per_trade = 1
        self.initial_capital = initial_capital
        self.pcr_threshold = pcr_threshold
        self.pcr_lookback = pcr_lookback
        self.pcr_trend_min = pcr_trend_min
        self.trading_start = trading_start
        self.trading_end = trading_end
        self.max_trades_per_day = max_trades_per_day
        self.instrument = instrument
        # PRT-only override: NIFTY lot size pinned to 65 regardless of config.LOT_SIZE
        self.lot_size = 65

        self._spot_1m: Optional[pd.DataFrame] = None
        self._options_1m: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Data loading & preparation
    # ------------------------------------------------------------------

    def _load_futures(self) -> pd.DataFrame:
        """Load 1-min futures OHLCV with 30-day warmup for indicators."""
        path = os.path.join(BASE_DIR, self._FUTURES_PATHS[self.instrument])
        return self._load_parquet_1m(path, self.start_date, self.end_date, warmup_days=30)

    @staticmethod
    def _load_parquet_1m(path: str, start_str: str, end_str: str, warmup_days: int = 0):
        """Load a 1-min parquet with optional warmup, return tz-aware df."""
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
        end = pd.to_datetime(end_str).tz_localize("Asia/Kolkata") + pd.Timedelta(days=1)
        if warmup_days:
            start = start - pd.Timedelta(days=warmup_days)
        df = df[(df["datetime"] >= start) & (df["datetime"] < end)]
        return df.sort_values("datetime").reset_index(drop=True)

    def _load_spot_pcr_raw(self, path: str) -> pd.DataFrame:
        """Load spot parquet for indicator calculation (with warmup, no PCR processing)."""
        return self._load_parquet_1m(path, self.start_date, self.end_date, warmup_days=30)

    def _load_spot_pcr(self) -> pd.DataFrame:
        """Load 1-min spot+PCR data (PCR values + spot prices for ATM strike)."""
        path = os.path.join(BASE_DIR, self._SPOT_PCR_PATHS[self.instrument])
        # Load with 1-day warmup so PCR lookback (30 min) has history
        # from previous trading day for early-morning bars
        df = self._load_parquet_1m(path, self.start_date, self.end_date, warmup_days=1)
        df["time_str"] = df["datetime"].dt.strftime("%H:%M")

        # PCR: forward-fill gaps, shift by 1 for no-lookahead
        df["pcr"] = df["pcr"].ffill()
        df["pcr_prev"] = df["pcr"].shift(1)
        df["pcr_change"] = df["pcr_prev"] - df["pcr_prev"].shift(self.pcr_lookback)

        # Trim warmup — keep only backtest date range
        start_dt = pd.to_datetime(self.start_date).date()
        df = df[df["date"] >= start_dt].reset_index(drop=True)
        return df

    def _resample(self, futures_1m: pd.DataFrame) -> pd.DataFrame:
        """Resample 1-min futures OHLCV to configured timeframe."""
        df = futures_1m.set_index("datetime").copy()
        df = df.between_time("09:15", "15:29")
        freq = f"{self.spot_timeframe}min"
        ohlcv = df.resample(freq).agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        ).dropna(subset=["close"])
        return ohlcv

    def _calculate_indicators(self, resampled: pd.DataFrame) -> pd.DataFrame:
        """Calculate SuperTrend, MACD, VWAP on resampled futures data."""
        df = resampled.copy()

        # SuperTrend
        st = SuperTrend(
            name="st", factor=self.st_factor, atr_period=self.st_period
        )
        st_result = st.calculate(df["close"], high=df["high"], low=df["low"])
        df["st_dir"] = st_result["direction"]
        df["st_val"] = st_result["value"]

        # MACD
        macd = MACD(
            name="macd",
            fast=self.macd_fast,
            slow=self.macd_slow,
            signal=self.macd_signal_period,
        )
        macd_result = macd.calculate(df["close"])
        df["macd_line"] = macd_result["macd"]
        df["macd_signal"] = macd_result["signal"]
        df["macd_hist"] = macd_result["histogram"]

        # VWAP — resets daily
        # Uses real volume when available (futures), falls back to
        # cumulative typical-price mean when volume is zero (spot index)
        df["_date"] = df.index.date
        vwap_parts = []
        for _, day_group in df.groupby("_date"):
            tp = (day_group["high"] + day_group["low"] + day_group["close"]) / 3.0
            if day_group["volume"].sum() > 0:
                cum_tpv = (tp * day_group["volume"]).cumsum()
                cum_vol = day_group["volume"].cumsum().replace(0, np.nan)
                vwap_parts.append(cum_tpv / cum_vol)
            else:
                vwap_parts.append(tp.expanding().mean())
        df["vwap"] = pd.concat(vwap_parts)
        df.drop(columns=["_date"], inplace=True)

        # Store resampled close for VWAP comparison (close < vwap)
        df["resampled_close"] = df["close"]

        return df

    def _forward_fill_to_1m(
        self, spot_1m: pd.DataFrame, resampled: pd.DataFrame
    ) -> pd.DataFrame:
        """Forward-fill resampled futures indicators onto spot 1-min timeline."""
        cols = [
            "st_dir", "st_val", "macd_line", "macd_signal", "macd_hist",
            "vwap", "resampled_close",
        ]
        ind = resampled[cols].copy()
        # Shift by timeframe: candle values available only after candle closes
        ind.index = ind.index + pd.Timedelta(minutes=self.spot_timeframe)

        spot_1m = spot_1m.copy()
        spot_idx = spot_1m.set_index("datetime").index
        ind_1m = ind.reindex(spot_idx, method="ffill")

        for col in cols:
            spot_1m[col] = ind_1m[col].values
        return spot_1m

    def _prepare_data(self):
        """Full data pipeline: indicators from futures or spot → merge onto spot+PCR."""
        spot_pcr_path = self._SPOT_PCR_PATHS[self.instrument]

        if self.indicator_source == "futures":
            # Load futures for indicators (ST, MACD, VWAP with real volume)
            futures_path = self._FUTURES_PATHS[self.instrument]
            logger.info(f"Loading futures data from {futures_path}...")
            indicator_1m = self._load_futures()
            logger.info(f"Futures 1m: {len(indicator_1m):,} rows")
        else:
            # Use spot for indicators (no volume → VWAP uses typical price mean)
            logger.info(f"Loading spot data for indicators from {spot_pcr_path}...")
            indicator_1m = self._load_spot_pcr_raw(spot_pcr_path)
            logger.info(f"Spot 1m: {len(indicator_1m):,} rows")

        logger.info(f"Resampling to {self.spot_timeframe}-min...")
        resampled = self._resample(indicator_1m)
        logger.info(f"Resampled: {len(resampled):,} rows")

        logger.info(f"Calculating indicators ({self.indicator_source})...")
        resampled = self._calculate_indicators(resampled)

        # Load spot+PCR (for PCR values + spot prices for ATM strike)
        logger.info("Loading spot+PCR data...")
        spot_1m = self._load_spot_pcr()
        logger.info(f"Spot+PCR 1m: {len(spot_1m):,} rows")

        logger.info("Forward-filling indicators onto spot timeline...")
        spot_1m = self._forward_fill_to_1m(spot_1m, resampled)

        self._spot_1m = spot_1m

        # Load options data
        logger.info("Loading options data...")
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        self._options_1m = load_data(
            options_path, self.start_date, self.end_date, "weekly"
        )
        logger.info(f"Options 1m: {len(self._options_1m):,} rows")

    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def _process_day(self, trading_date) -> List[PrtTrade]:
        """Process a single trading day. Returns 0 or more trades.

        Exits: SL > TP > EOD. No indicator-flip exits.
        """
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

        trades: List[PrtTrade] = []
        day_trade_count = 0

        # --- Position state ---
        in_position = False
        entry_price = 0.0
        option_strike = 0.0
        option_type = ""
        tp_price = 0.0
        sl_price = 0.0
        signal_time = ""
        entry_time = ""
        signal_indicators: dict = {}

        for _, candle in day_spot.iterrows():
            t_str = candle["time_str"]
            t_dt = candle["datetime"]
            st_dir = candle.get("st_dir", np.nan)
            macd_hist = candle.get("macd_hist", np.nan)
            vwap_val = candle.get("vwap", np.nan)
            resampled_close = candle.get("resampled_close", np.nan)
            pcr_prev_val = candle.get("pcr_prev", np.nan)
            pcr_change_val = candle.get("pcr_change", np.nan)

            # Skip bars with no indicator data (warmup) or pre-trading hours
            if pd.isna(st_dir) or pd.isna(macd_hist) or pd.isna(vwap_val):
                continue
            if t_str < self.trading_start:
                continue

            just_exited = False

            # ============ 1. SL / TP CHECK (every bar) ============
            if in_position:
                ohlc = self._get_option_ohlc(
                    opt_by_dt, t_dt, option_strike, option_type
                )
                if ohlc is not None:
                    _, opt_h, opt_l, _ = ohlc

                    # SL wins on same bar as TP (conservative)
                    if opt_l <= sl_price:
                        trade = self._make_trade(
                            trading_date, option_type, option_strike,
                            expiry_date, signal_indicators, signal_time,
                            entry_time, entry_price,
                            t_str, sl_price, "SL",
                        )
                        trades.append(trade)
                        in_position = False
                        just_exited = True
                        day_trade_count += 1
                    elif opt_h >= tp_price:
                        trade = self._make_trade(
                            trading_date, option_type, option_strike,
                            expiry_date, signal_indicators, signal_time,
                            entry_time, entry_price,
                            t_str, tp_price, "TP",
                        )
                        trades.append(trade)
                        in_position = False
                        just_exited = True
                        day_trade_count += 1

            # ============ 2. EOD CHECK ============
            if in_position and t_str >= self.trading_end:
                ohlc = self._get_option_ohlc(
                    opt_by_dt, t_dt, option_strike, option_type
                )
                if ohlc is not None:
                    _, _, _, opt_c = ohlc
                    trade = self._make_trade(
                        trading_date, option_type, option_strike,
                        expiry_date, signal_indicators, signal_time,
                        entry_time, entry_price,
                        t_str, opt_c, "EOD",
                    )
                    trades.append(trade)
                    in_position = False
                    just_exited = True
                    day_trade_count += 1

            # ============ 3. ENTRY CHECK (boundary bars) ============
            is_boundary = t_dt.minute % self.spot_timeframe == 0
            at_trade_cap = (
                self.max_trades_per_day > 0
                and day_trade_count >= self.max_trades_per_day
            )
            if (
                not in_position
                and not just_exited
                and is_boundary
                and t_str >= self.trading_start
                and t_str < self.trading_end
                and not at_trade_cap
            ):
                sig_type = self._check_signal(
                    st_dir, macd_hist, resampled_close, vwap_val,
                    pcr_prev_val, pcr_change_val,
                )
                if sig_type is not None:
                    rounding = STRIKE_ROUNDING.get(self.instrument, 50)
                    spot_open = candle["open"]
                    atm_strike = round(spot_open / rounding) * rounding

                    opt_open = self._get_option_open(
                        opt_by_dt, t_dt, atm_strike, sig_type
                    )
                    if opt_open is not None and opt_open > 0:
                        entry_price = opt_open
                        option_strike = atm_strike
                        option_type = sig_type
                        entry_time = t_str
                        # Signal candle closed 1 min before this entry bar
                        signal_dt = t_dt - pd.Timedelta(minutes=1)
                        signal_time = signal_dt.strftime("%H:%M")
                        in_position = True

                        tp_price = round(
                            entry_price * (1 + self.tp_pct / 100), 2
                        )
                        sl_price = round(
                            entry_price * (1 - self.sl_pct / 100), 2
                        )

                        signal_indicators = {
                            "st_dir": float(st_dir),
                            "macd_hist": round(float(macd_hist), 4),
                            "candle_close": round(float(resampled_close), 2),
                            "vwap": round(float(vwap_val), 2),
                            "spot_open": round(float(spot_open), 2),
                            "pcr": (
                                round(float(pcr_prev_val), 4)
                                if not pd.isna(pcr_prev_val)
                                else 0.0
                            ),
                            "pcr_change": (
                                round(float(pcr_change_val), 4)
                                if not pd.isna(pcr_change_val)
                                else 0.0
                            ),
                        }

                        logger.debug(
                            f"{trading_date} {t_str} ENTRY {option_type} "
                            f"strike={option_strike} price={entry_price} "
                            f"TP={tp_price} SL={sl_price}"
                        )

        # Safety net: force close if still in position at day end
        if in_position:
            exit_price, exit_time_str = self._last_option_price(
                day_options, option_strike, option_type
            )
            trade = self._make_trade(
                trading_date, option_type, option_strike,
                expiry_date, signal_indicators, signal_time,
                entry_time, entry_price,
                exit_time_str, exit_price, "EOD",
            )
            trades.append(trade)

        return trades

    # ------------------------------------------------------------------
    # Signal detection
    # ------------------------------------------------------------------

    def _check_signal(
        self,
        st_dir: float,
        macd_hist: float,
        resampled_close: float,
        vwap_val: float,
        pcr_prev_val: float,
        pcr_change_val: float,
    ) -> Optional[str]:
        """Check entry conditions. Returns 'CE', 'PE', or None."""
        if pd.isna(pcr_prev_val):
            return None

        pcr_up = pcr_prev_val > self.pcr_threshold or (
            not pd.isna(pcr_change_val) and pcr_change_val >= self.pcr_trend_min
        )
        pcr_down = pcr_prev_val < self.pcr_threshold or (
            not pd.isna(pcr_change_val) and pcr_change_val <= -self.pcr_trend_min
        )

        # CE (bullish)
        if (
            st_dir == -1
            and macd_hist > 0
            and resampled_close < vwap_val
            and pcr_up
        ):
            return "CE"

        # PE (bearish)
        if (
            st_dir == 1
            and macd_hist < 0
            and resampled_close > vwap_val
            and pcr_down
        ):
            return "PE"

        return None

    # ------------------------------------------------------------------
    # Option data helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_option_open(opt_by_dt, t_dt, strike, option_type) -> Optional[float]:
        """Get option open price for a given strike/type at a datetime."""
        minute_opts = opt_by_dt.get(t_dt)
        if minute_opts is None:
            return None
        match = minute_opts[
            (minute_opts["strike"] == strike)
            & (minute_opts["option_type"] == option_type)
        ]
        if match.empty:
            return None
        return round(match.iloc[0]["open"], 2)

    @staticmethod
    def _get_option_ohlc(opt_by_dt, t_dt, strike, option_type):
        """Get (open, high, low, close) for a given strike/type at a datetime."""
        minute_opts = opt_by_dt.get(t_dt)
        if minute_opts is None:
            return None
        match = minute_opts[
            (minute_opts["strike"] == strike)
            & (minute_opts["option_type"] == option_type)
        ]
        if match.empty:
            return None
        row = match.iloc[0]
        return (
            round(row["open"], 2),
            round(row["high"], 2),
            round(row["low"], 2),
            round(row["close"], 2),
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

    # ------------------------------------------------------------------
    # Trade builder
    # ------------------------------------------------------------------

    def _make_trade(
        self,
        trading_date,
        option_type,
        strike,
        expiry_date,
        indicators,
        signal_time,
        entry_time,
        entry_price,
        exit_time,
        exit_price,
        exit_reason,
    ) -> PrtTrade:
        lots = self.lots_per_trade  # always 1
        pnl_inr = round((exit_price - entry_price) * lots * self.lot_size, 2)
        pnl_points = round(exit_price - entry_price, 2)
        pnl_pct = (
            round((exit_price - entry_price) / entry_price * 100, 3)
            if entry_price > 0
            else 0.0
        )

        return PrtTrade(
            date=str(trading_date),
            option_type=option_type,
            strike=strike,
            expiry_date=str(expiry_date),
            signal_time=signal_time,
            st_direction=indicators.get("st_dir", 0.0),
            macd_histogram=indicators.get("macd_hist", 0.0),
            candle_close=indicators.get("candle_close", 0.0),
            vwap_value=indicators.get("vwap", 0.0),
            pcr_value=indicators.get("pcr", 0.0),
            pcr_change=indicators.get("pcr_change", 0.0),
            spot_open=indicators.get("spot_open", 0.0),
            entry_time=entry_time,
            entry_price=entry_price,
            entry_lots=lots,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_lots=lots,
            exit_reason=exit_reason,
            pnl_points=pnl_points,
            pnl_pct=pnl_pct,
            pnl_inr=pnl_inr,
            # equity_after stamped in run() once trade order is known.
            equity_after=0.0,
            roi_pct=(
                round(pnl_inr / self.initial_capital * 100, 3)
                if self.initial_capital > 0
                else 0.0
            ),
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[PrtTrade]:
        """Run backtest. Returns list of PrtTrade with compounding equity curve."""
        self._prepare_data()

        all_dates = sorted(self._spot_1m["date"].unique())
        trades: List[PrtTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))
            day_trades = self._process_day(trading_date)
            trades.extend(day_trades)

        # Running equity balance (fixed 1-lot sizing — no compounding of size).
        # roi_pct is already set per-trade against fixed initial_capital.
        equity = self.initial_capital
        for trade in trades:
            equity += trade.pnl_inr
            trade.equity_after = round(equity, 2)

        logger.info(
            f"Backtest complete: {len(trades)} trades, "
            f"final equity = {equity:.2f} "
            f"(initial = {self.initial_capital:.2f})"
        )
        return trades
