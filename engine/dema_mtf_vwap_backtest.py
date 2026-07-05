"""
DEMA Trend + Multi-Timeframe EMA + ATM Option VWAP Crossover Backtest Engine.

Strategy (long options only):
  Trend filter (5-min spot):
    - Spot close > DEMA(20) -> CE bias | spot close < DEMA(20) -> PE bias

  Multi-timeframe confirmation (1-hour spot, candles anchored at 09:15):
    - CE: last COMPLETED 1H candle close > EMA(20) and > EMA(50) on the 1H series
    - PE: close below both. Mixed -> no trade.

  Entry trigger (5-min ATM option, strictly per-contract):
    - Option 5m close > its intraday VWAP (cumulative close*volume from 09:15)
    - Previous 5m candle close <= VWAP at that candle (fresh crossover only)
    - Buy at open of the next 5m candle, ATM strike, nearest weekly expiry
    - On the expiry day itself, trade the NEXT weekly expiry (expiry_code 2)
      instead of the expiring contract, to keep decent time value

  Exit (priority order, checked on each 1-min option candle):
    1. SL: low touches entry * (1 - sl_pct/100) -> exact fill at SL
    2. TP: high touches entry * (1 + tp_pct/100) -> exact fill at TP
    3. EOD force exit at force_exit_time (at 1-min close)

  One open position at a time, unlimited re-entries per day.
  All 5m/1H indicators are evaluated on closed candles only (no lookahead).
"""

import logging
import os
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import List, Optional

import numpy as np
import pandas as pd

from config import (
    DATA_PATH,
    LOT_SIZE,
    SPOT_DATA_PATH,
    STRIKE_ROUNDING,
    get_nearest_weekly_expiry,
)
from indicators.dema import DEMA
from indicators.ema import EMA

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------

@dataclass
class DemaMtfVwapTrade:
    date: str                   # "YYYY-MM-DD"
    option_type: str            # "CE" / "PE"
    strike: float
    expiry_date: str

    # Context at signal candle close
    spot_at_signal: float
    dema_at_signal: float
    mtf_close: float            # last completed 1H candle close
    vwap_at_signal: float       # option intraday VWAP at signal candle
    option_close_at_signal: float

    signal_time: str            # "HH:MM" signal candle close
    entry_time: str             # "HH:MM" next candle open
    entry_price: float          # option open of entry candle
    qty: int
    sl_price: float
    tp_price: float

    exit_time: str
    exit_price: float
    exit_reason: str            # "SL" / "TP" / "EOD"

    pnl_points: float
    pnl_pct: float
    pnl_inr: float


def trades_to_dataframe(trades: List[DemaMtfVwapTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class DemaMtfVwapBacktestEngine:
    """DEMA trend + 1H EMA confirmation + ATM option VWAP crossover engine."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        dema_period: int = 20,
        mtf_ema_fast: int = 20,
        mtf_ema_slow: int = 50,
        sl_pct: float = 30.0,
        tp_pct: float = 50.0,
        entry_start: str = "09:30",
        entry_end: str = "14:45",
        force_exit_time: str = "15:15",
        instrument: str = "NIFTY",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.dema_period = dema_period
        self.mtf_ema_fast = mtf_ema_fast
        self.mtf_ema_slow = mtf_ema_slow
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct
        self.entry_start = entry_start
        self.entry_end = entry_end
        self.force_exit_time = force_exit_time
        self.instrument = instrument
        self.lot_size = LOT_SIZE.get(instrument, 75)

        self._spot_5m: Optional[pd.DataFrame] = None
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

        start = pd.to_datetime(self.start_date).tz_localize("Asia/Kolkata")
        end = pd.to_datetime(self.end_date).tz_localize("Asia/Kolkata") + pd.Timedelta(days=1)
        # Extra history so 1H EMA(50) and 5m DEMA(20) are warm from day one
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

    def _resample_1h(self, spot_1m: pd.DataFrame) -> pd.DataFrame:
        """Resample 1-min spot to 1H candles anchored at 09:15 (NSE style)."""
        df = spot_1m.set_index("datetime").copy()
        df = df.between_time("09:15", "15:29")
        ohlcv = df.resample("60min", origin="start_day", offset="9h15min").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna(subset=["close"])
        return ohlcv

    def _attach_mtf(self, spot_5m: pd.DataFrame, mtf_1h: pd.DataFrame) -> pd.DataFrame:
        """Attach last COMPLETED 1H candle values onto each 5m candle.

        A 1H candle starting at t completes at t+60min; a 5m candle labelled
        t decides at t+5min. The attached values are the latest 1H candle
        whose end <= the 5m decision time (no lookahead).
        """
        avail = mtf_1h[["close", "ema_fast", "ema_slow"]].copy()
        avail.index = avail.index + pd.Timedelta(minutes=60)

        decision_idx = spot_5m.index + pd.Timedelta(minutes=5)
        att = avail.reindex(decision_idx, method="ffill")

        out = spot_5m.copy()
        out["mtf_close"] = att["close"].values
        out["mtf_ema_fast"] = att["ema_fast"].values
        out["mtf_ema_slow"] = att["ema_slow"].values
        return out

    def _build_contract_5m(self, contract_1m: pd.DataFrame) -> pd.DataFrame:
        """Build 5m candles + cumulative intraday VWAP for one contract/day.

        VWAP = cumsum(close * volume) / cumsum(volume) over the contract's
        own 1-min data only (strictly per-contract, strikes never mixed).
        """
        c = contract_1m.sort_values("datetime").copy()
        cum_pv = (c["close"] * c["volume"]).cumsum()
        cum_v = c["volume"].cumsum().replace(0, np.nan)
        c["vwap"] = (cum_pv / cum_v).ffill()
        c["bucket"] = c["datetime"].dt.floor("5min")

        g = c.groupby("bucket")
        return pd.DataFrame({
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "vwap": g["vwap"].last(),
        })

    def _load_options(self) -> pd.DataFrame:
        """Load 1-min weekly options with expiry codes 1 and 2.

        Code 1 (nearest weekly) is traded on normal days; code 2 (next
        weekly) is traded on expiry days so positions keep time value.
        """
        path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        df = pd.read_parquet(path)
        df["datetime"] = pd.to_datetime(df["datetime"])

        start = pd.to_datetime(self.start_date).tz_localize("Asia/Kolkata")
        end = pd.to_datetime(self.end_date).tz_localize("Asia/Kolkata") + pd.Timedelta(days=1)
        df = df[(df["datetime"] >= start) & (df["datetime"] < end)]
        df = df[(df["expiry_type"] == "WEEK") & (df["expiry_code"].isin([1, 2]))]

        df = df.sort_values("datetime").reset_index(drop=True)
        df["date"] = df["datetime"].dt.date
        return df

    def _prepare_data(self):
        """Full data pipeline: load, resample, indicators, MTF merge."""
        logger.info("Loading spot data...")
        spot_1m = self._load_spot()
        logger.info(f"Spot 1m: {len(spot_1m):,} rows")

        spot_5m = self._resample_5min(spot_1m)
        spot_5m["dema"] = DEMA(name="dema", period=self.dema_period).calculate(spot_5m["close"])

        mtf_1h = self._resample_1h(spot_1m)
        mtf_1h["ema_fast"] = EMA(name="ema_fast", period=self.mtf_ema_fast).calculate(mtf_1h["close"])
        mtf_1h["ema_slow"] = EMA(name="ema_slow", period=self.mtf_ema_slow).calculate(mtf_1h["close"])
        logger.info(f"Spot 5m: {len(spot_5m):,} rows | 1H: {len(mtf_1h):,} candles")

        spot_5m = self._attach_mtf(spot_5m, mtf_1h)
        spot_5m["date"] = spot_5m.index.date

        # Trim warmup days
        start_dt = pd.to_datetime(self.start_date).date()
        self._spot_5m = spot_5m[spot_5m["date"] >= start_dt]

        logger.info("Loading options data...")
        self._options_1m = self._load_options()
        logger.info(f"Options 1m: {len(self._options_1m):,} rows")

    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[DemaMtfVwapTrade]:
        """Run backtest. Returns list of DemaMtfVwapTrade."""
        self._prepare_data()

        all_dates = sorted(self._spot_5m["date"].unique())
        trades: List[DemaMtfVwapTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))
            trades.extend(self._process_day(trading_date))

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    def _process_day(self, trading_date) -> List[DemaMtfVwapTrade]:
        """Process a single trading day. Returns 0 or more trades."""
        day_spot = self._spot_5m[self._spot_5m["date"] == trading_date]
        if day_spot.empty:
            return []

        # On expiry day, roll to the next weekly expiry (code 2);
        # otherwise trade the nearest weekly (code 1)
        nearest = get_nearest_weekly_expiry(trading_date)
        if nearest is None:
            return []
        if nearest == trading_date:
            expiry_code = 2
            expiry_date = get_nearest_weekly_expiry(trading_date + timedelta(days=1))
        else:
            expiry_code = 1
            expiry_date = nearest
        if expiry_date is None:
            return []

        day_options = self._options_1m[
            (self._options_1m["date"] == trading_date)
            & (self._options_1m["expiry_code"] == expiry_code)
        ]
        if day_options.empty:
            return []

        rounding = STRIKE_ROUNDING.get(self.instrument, 50)

        # Per-contract 5m candles + VWAP, built lazily and cached for the day
        c5_cache, c1_cache = {}, {}

        def get_contract(strike, otype):
            key = (strike, otype)
            if key not in c5_cache:
                c1 = day_options[
                    (day_options["strike"] == strike)
                    & (day_options["option_type"] == otype)
                ]
                if c1.empty:
                    c5_cache[key], c1_cache[key] = None, None
                else:
                    c1 = c1.sort_values("datetime")
                    c1_cache[key] = c1
                    c5_cache[key] = self._build_contract_5m(c1)
            return c5_cache[key], c1_cache[key]

        trades: List[DemaMtfVwapTrade] = []
        buckets = day_spot.index

        pending = None   # signal awaiting fill at next bucket open
        pos = None       # open position

        for i, t in enumerate(buckets):
            row = day_spot.loc[t]
            bucket_end = t + pd.Timedelta(minutes=5)
            close_str = bucket_end.strftime("%H:%M")

            # ============ 1. FILL PENDING ENTRY at this bucket's open ============
            if pending is not None and t == pending["entry_bucket"]:
                _, c1 = get_contract(pending["strike"], pending["option_type"])
                bucket_rows = c1[(c1["datetime"] >= t) & (c1["datetime"] < bucket_end)]
                if bucket_rows.empty:
                    pending = None  # no contract data at entry candle -> no fill
                else:
                    first = bucket_rows.iloc[0]
                    entry_price = round(float(first["open"]), 2)
                    pos = {
                        **pending,
                        "entry_time": first["datetime"].strftime("%H:%M"),
                        "entry_price": entry_price,
                        "sl_price": round(entry_price * (1 - self.sl_pct / 100), 2),
                        "tp_price": round(entry_price * (1 + self.tp_pct / 100), 2),
                    }
                    pending = None
                    logger.debug(
                        f"{trading_date} {pos['entry_time']} ENTRY {pos['option_type']} "
                        f"strike={pos['strike']} premium={entry_price} "
                        f"SL={pos['sl_price']} TP={pos['tp_price']}"
                    )
            elif pending is not None and t > pending["entry_bucket"]:
                pending = None  # entry bucket missing from spot data -> drop

            # ============ 2. EXIT CHECKS on this bucket's 1-min candles ============
            if pos is not None:
                _, c1 = get_contract(pos["strike"], pos["option_type"])
                bucket_rows = c1[
                    (c1["datetime"] >= max(t, pd.Timestamp(0, tz=t.tz)))
                    & (c1["datetime"] < bucket_end)
                ]
                for _, m in bucket_rows.iterrows():
                    m_str = m["datetime"].strftime("%H:%M")
                    if m_str < pos["entry_time"]:
                        continue
                    if m["low"] <= pos["sl_price"]:
                        trades.append(self._make_trade(pos, m_str, pos["sl_price"], "SL"))
                        pos = None
                        break
                    if m["high"] >= pos["tp_price"]:
                        trades.append(self._make_trade(pos, m_str, pos["tp_price"], "TP"))
                        pos = None
                        break
                    if m_str >= self.force_exit_time:
                        trades.append(
                            self._make_trade(pos, m_str, round(float(m["close"]), 2), "EOD")
                        )
                        pos = None
                        break

            # ============ 3. SIGNAL DETECTION at this bucket's close ============
            if pos is not None or pending is not None:
                continue
            if close_str < self.entry_start or close_str > self.entry_end:
                continue
            if (pd.isna(row["dema"]) or pd.isna(row["mtf_close"])
                    or pd.isna(row["mtf_ema_fast"]) or pd.isna(row["mtf_ema_slow"])):
                continue

            spot_close = row["close"]
            if spot_close > row["dema"]:
                bias = "CE"
                mtf_ok = (row["mtf_close"] > row["mtf_ema_fast"]
                          and row["mtf_close"] > row["mtf_ema_slow"])
            elif spot_close < row["dema"]:
                bias = "PE"
                mtf_ok = (row["mtf_close"] < row["mtf_ema_fast"]
                          and row["mtf_close"] < row["mtf_ema_slow"])
            else:
                continue
            if not mtf_ok:
                continue

            strike = round(spot_close / rounding) * rounding
            c5, _ = get_contract(strike, bias)
            if c5 is None:
                continue

            prev_t = t - pd.Timedelta(minutes=5)
            if t not in c5.index or prev_t not in c5.index:
                continue
            cur, prev = c5.loc[t], c5.loc[prev_t]
            if pd.isna(cur["vwap"]) or pd.isna(prev["vwap"]):
                continue

            # Fresh crossover only: prev candle at/below VWAP, current above
            if not (cur["close"] > cur["vwap"] and prev["close"] <= prev["vwap"]):
                continue

            # Entry at the open of the immediately following 5m candle
            if i + 1 >= len(buckets) or buckets[i + 1] != bucket_end:
                continue
            if bucket_end.strftime("%H:%M") >= self.force_exit_time:
                continue

            pending = {
                "option_type": bias,
                "strike": strike,
                "expiry_date": expiry_date,
                "trading_date": trading_date,
                "signal_time": close_str,
                "spot_at_signal": round(float(spot_close), 2),
                "dema_at_signal": round(float(row["dema"]), 2),
                "mtf_close": round(float(row["mtf_close"]), 2),
                "vwap_at_signal": round(float(cur["vwap"]), 2),
                "option_close_at_signal": round(float(cur["close"]), 2),
                "entry_bucket": bucket_end,
            }
            logger.debug(
                f"{trading_date} {close_str} SIGNAL {bias} strike={strike} "
                f"(entry queued for next candle)"
            )

        # Safety net: force close at last available contract price
        if pos is not None:
            _, c1 = get_contract(pos["strike"], pos["option_type"])
            last = c1.iloc[-1]
            trades.append(self._make_trade(
                pos, last["datetime"].strftime("%H:%M"),
                round(float(last["close"]), 2), "EOD",
            ))

        return trades

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_trade(self, pos, exit_time, exit_price, exit_reason) -> DemaMtfVwapTrade:
        entry_price = pos["entry_price"]
        pnl_points = round(exit_price - entry_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        pnl_inr = round(pnl_points * self.lot_size, 2)
        return DemaMtfVwapTrade(
            date=str(pos["trading_date"]),
            option_type=pos["option_type"],
            strike=float(pos["strike"]),
            expiry_date=str(pos["expiry_date"]),
            spot_at_signal=pos["spot_at_signal"],
            dema_at_signal=pos["dema_at_signal"],
            mtf_close=pos["mtf_close"],
            vwap_at_signal=pos["vwap_at_signal"],
            option_close_at_signal=pos["option_close_at_signal"],
            signal_time=pos["signal_time"],
            entry_time=pos["entry_time"],
            entry_price=entry_price,
            qty=self.lot_size,
            sl_price=pos["sl_price"],
            tp_price=pos["tp_price"],
            exit_time=exit_time,
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl_points=pnl_points,
            pnl_pct=pnl_pct,
            pnl_inr=pnl_inr,
        )
