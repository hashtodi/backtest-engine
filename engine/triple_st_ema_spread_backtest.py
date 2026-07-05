"""
Triple-SuperTrend (3m/5m/10m) Alignment + EMA9/21 Cross -> Credit-Spread Engine.

Translated from the user's TradingView Pine "Triple Supertrend + EMA9/21 Cross"
strategy, mapped onto NIFTY defined-risk credit spreads.

  SIGNAL series = true 1-min NIFTY spot OHLC (data/spot/nifty/NIFTY_1m.parquet).
  Indicators are CONTINUOUS across days (no daily reset); warm-up days are
  loaded before backtest_start so SuperTrend/EMA are seeded.

  Multi-timeframe regime (3m / 5m / 10m SuperTrend):
    direction == -1 -> uptrend (green) ; +1 -> downtrend (red).
    LONG  regime when all three == -1 ; SHORT regime when all three == +1.
    Two computation modes (config signal.htf_mode):
      "rolling" (default) -- at each 1-min bar T the tf-minute bar is the
        trailing window [T-tf+1 .. T] (high=trailing max, low=trailing min,
        close=close[T]); SuperTrend runs on this minute-by-minute series so the
        direction refreshes EVERY minute. The window uses only data <= T, so no
        look-ahead / no availability shift. A faster, smoother variant -- NOT
        identical to a real tf-chart SuperTrend (the windows overlap).
      "anchored" -- non-overlapping tf-min bars anchored at the 09:15 open; a
        bar's direction is only visible on the 1-min bar that OPENS at/after it
        CLOSED (TradingView request.security lookahead_off): usable at 1-min bar
        `T` iff `s + tf <= T`. Steps only at tf boundaries.

  Trigger (EMA9 / EMA21 on the continuous 1-min close):
    long_cross[T]  = ema9 > ema21 and prev ema9 <= prev ema21
    short_cross[T] = ema9 < ema21 and prev ema9 >= prev ema21

  Entry signal (evaluated at each 1-min bar T's close, in the entry window):
    LONG  = LONG  regime and long_cross   -> BULL-PUT  credit spread
    SHORT = SHORT regime and short_cross  -> BEAR-CALL credit spread

  Spread structure (anchored to the SIGNAL bar's spot close):
    atm = round(spot_close / strike_step) * strike_step
    LONG  -> SELL PE atm - sell_offset*step , BUY PE atm - buy_offset*step
    SHORT -> SELL CE atm + sell_offset*step , BUY CE atm + buy_offset*step
    (sell_offset=2 strikes = 100 pts OTM, buy_offset=6 = 300 pts -> 200-pt width.)

  Fills (no look-ahead):
    * ENTRY fills at the SIGNAL bar's own CLOSE premiums (the user's rule).
    * Only ONE spread open at a time; unlimited re-entries per day
      (max_trades_per_day = 0 -> unlimited).
    * TP / SL are ABSOLUTE INR per lot on the live spread mark-to-market:
        live_inr = ((sell_entry + buy_now) - (sell_now + buy_entry)) * lot_size * lots
        TP when live_inr >=  tp_inr * lots
        SL when live_inr <= -sl_inr * lots
      Detected on each 1-min option close after entry; the exit FILLS at the
      NEXT 1-min bar's OPEN. SL is checked before TP.
    * END-OF-DAY square-off at square_off_time fills at that minute's CLOSE.

  Expiry-day roll: on a weekly-expiry day trade the NEXT weekly (expiry_code 2);
  otherwise the nearest weekly (code 1).

  P&L (per spread):
    pnl_pts = (sell_entry + buy_exit) - (sell_exit + buy_entry)
    pnl_inr = pnl_pts * lot_size * lots
"""

import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date as _date, timedelta
from math import isnan
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import SPOT_DATA_PATH, STRIKE_ROUNDING, get_nearest_weekly_expiry
from indicators import get_indicator

logger = logging.getLogger(__name__)

LOT_SIZE_NIFTY = 65  # Mirrors config.LOT_SIZE['NIFTY']; pinned for unit tests.

SESSION_START = "09:15:00"
SPOT_SESSION_END = "15:29:00"
DEFAULT_ST_FACTOR = 3.0
DEFAULT_ST_ATR_PERIOD = 12
DEFAULT_TF1_MIN = 3
DEFAULT_TF2_MIN = 5
DEFAULT_TF3_MIN = 10
DEFAULT_HTF_MODE = "rolling"   # "rolling" (trailing window, per-minute) | "anchored"
DEFAULT_EMA_FAST = 9
DEFAULT_EMA_SLOW = 21
DEFAULT_WARMUP_DAYS = 10
DEFAULT_WINDOW_START = "09:30:00"
DEFAULT_WINDOW_END = "14:45:00"
DEFAULT_SQUARE_OFF_TIME = "15:15:00"
DEFAULT_TP_INR = 800.0
DEFAULT_SL_INR = 650.0
DEFAULT_SELL_OFFSET = 2     # strikes OTM (x strike_step pts)
DEFAULT_BUY_OFFSET = 6
DEFAULT_MAX_TRADES_PER_DAY = 0   # 0 -> unlimited

OPT_COLS = [
    "datetime", "underlying", "option_type", "expiry_type", "expiry_code",
    "strike", "open", "close",
]

# Reasons a qualifying signal is skipped (never produces a trade). These are
# tracked separately so the summary doesn't conflate "a far-OTM leg wasn't
# listed in the chain" with "the spread would have been a debit".
SKIP_REASONS = ("spot_missing", "sell_leg_missing", "buy_leg_missing",
                "nonpositive_credit")


def _empty_skips() -> Dict[str, int]:
    return {r: 0 for r in SKIP_REASONS}


def _norm_time(s) -> str:
    """Normalize 'H:MM', 'HH:MM', or 'HH:MM:SS' to 'HH:MM:SS'."""
    parts = str(s).strip().split(":")
    if len(parts) == 2:
        parts.append("0")
    h, m, sec = (int(p) for p in parts)
    return f"{h:02d}:{m:02d}:{sec:02d}"


# --------------------------------------------------------------------------- #
#  Dataclasses                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class LegFill:
    """Resolved leg of the spread: strike + entry/exit price."""
    option_type: str      # "CE" | "PE"
    side: str             # "BUY" | "SELL"
    lots: int
    strike_offset: int    # signed offset in STRIKES from ATM (e.g. -2, +6)
    strike: float
    entry_price: float
    exit_price: Optional[float] = None


@dataclass
class TripleStEmaTrade:
    date: str
    signal: str                 # "LONG" | "SHORT"
    spread: str                 # "BULL_PUT" | "BEAR_CALL"
    direction: str              # "LONG" (sell PE) | "SHORT" (sell CE)
    expiry_code: int            # 1 (nearest) | 2 (rolled, expiry day)

    entry_time: str             # HH:MM -- signal bar's close minute (entry fill)
    entry_spot: float           # spot close at the signal bar
    atm_strike: float

    exit_time: str              # HH:MM
    exit_reason: str            # "TP" | "SL" | "EOD"
    exit_spot: float
    fill_fallback: bool         # an exit fill used a different minute / field

    net_credit_pts: float       # sell_entry - buy_entry (per contract)
    net_credit_inr: float
    pnl_pts: float
    pnl_inr: float
    return_pct: float
    running_equity_inr: float

    legs: Dict[str, LegFill] = field(default_factory=dict)


@dataclass
class TripleStEmaDayContext:
    date: str
    expiry_code: int = 1
    lots: int = 1
    sell_offset_abs: int = DEFAULT_SELL_OFFSET
    buy_offset_abs: int = DEFAULT_BUY_OFFSET
    tp_inr: float = DEFAULT_TP_INR          # PER LOT; 0 disables
    sl_inr: float = DEFAULT_SL_INR          # PER LOT; 0 disables
    square_off_time: str = DEFAULT_SQUARE_OFF_TIME
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY   # 0 -> unlimited
    strike_step: int = 50
    lot_size: int = LOT_SIZE_NIFTY


# --------------------------------------------------------------------------- #
#  Signal series                                                              #
# --------------------------------------------------------------------------- #

def attach_last_completed(htf_dir: pd.Series, target_dt, tf_min: int) -> pd.Series:
    """Attach the last COMPLETED higher-timeframe direction onto 1-min bars.

    `htf_dir` is indexed by the HTF bar's START timestamp. The bar with start
    `s` closes at `s + tf_min` and -- matching TradingView's
    request.security(lookahead_off) -- only becomes visible on the 1-min bar
    `T` with `T >= s + tf_min`.  We therefore shift the index forward by
    `tf_min` (to the close instant) and forward-fill onto `target_dt`.

    Returns a Series indexed by `target_dt`.
    """
    d = htf_dir.copy()
    d.index = d.index + pd.Timedelta(minutes=tf_min)
    d = d[~d.index.duplicated(keep="last")].sort_index()
    return d.reindex(pd.DatetimeIndex(target_dt), method="ffill")


def _attach_htf_dir(spot: pd.DataFrame, tf_min: int, factor: float,
                    atr_period: int) -> np.ndarray:
    """Resample 1-min spot to `tf_min` bars (anchored 09:15), run SuperTrend on
    the continuous series, and attach the last-completed direction to each
    1-min bar. Returns an array aligned to `spot` row order."""
    s = spot.set_index("dt")
    intraday = s.between_time("09:15", "15:29")
    htf = intraday.resample(f"{tf_min}min", origin="start_day",
                            offset="9h15min").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).dropna(subset=["close"])
    if htf.empty:
        return np.full(len(spot), np.nan)
    st = get_indicator("SUPERTREND", name=f"st{tf_min}", factor=factor,
                       atr_period=atr_period).calculate(
        htf["close"], high=htf["high"], low=htf["low"])
    att = attach_last_completed(st["direction"], spot["dt"].values, tf_min)
    return att.values


def _attach_htf_dir_rolling(spot: pd.DataFrame, tf_min: int, factor: float,
                            atr_period: int) -> np.ndarray:
    """ROLLING (trailing-window) SuperTrend, recomputed every 1-min bar.

    At each 1-min bar `T` the `tf_min`-minute bar is the sliding window
    `[T-tf_min+1 .. T]`: high = trailing max, low = trailing min, close =
    close[T]. SuperTrend runs over this minute-by-minute series, so the
    direction refreshes every minute (vs. only at fixed boundaries in the
    anchored mode). The window only uses data <= T, so the direction at bar `T`
    is known at `T`'s close -- no look-ahead and no availability shift.

    NB: the windows overlap, so the True-Range / ATR is a rolling-range
    volatility -- this is a faster, smoother variant and is NOT identical to a
    real `tf_min`-chart SuperTrend. Rolling is by ROW over the continuous,
    time-sorted series (consistent with the continuous anchored SuperTrend); the
    first few minutes of each day blend the prior session's tail, but those bars
    are well before the entry window.
    """
    spot = spot.sort_values("dt")
    high = spot["high"].rolling(tf_min, min_periods=1).max().reset_index(drop=True)
    low = spot["low"].rolling(tf_min, min_periods=1).min().reset_index(drop=True)
    close = spot["close"].reset_index(drop=True)
    st = get_indicator("SUPERTREND", name=f"st_roll{tf_min}", factor=factor,
                       atr_period=atr_period).calculate(close, high=high, low=low)
    return st["direction"].values


def _finalize_signals(out: pd.DataFrame, window_start: str,
                      window_end: str) -> pd.DataFrame:
    """Add regime / cross / signal / window columns from dir1..3 + EMAs.

    Pure function over the dir1/dir2/dir3, ema_fast, ema_slow, _time columns --
    unit-tested independently of the SuperTrend/EMA computation.
    """
    d1, d2, d3 = out["dir1"], out["dir2"], out["dir3"]
    out["regime"] = np.where(
        (d1 == -1) & (d2 == -1) & (d3 == -1), "LONG",
        np.where((d1 == 1) & (d2 == 1) & (d3 == 1), "SHORT", "NONE"))
    ef, es = out["ema_fast"], out["ema_slow"]
    long_cross = (ef > es) & (ef.shift(1) <= es.shift(1))
    short_cross = (ef < es) & (ef.shift(1) >= es.shift(1))
    out["long_sig"] = ((out["regime"] == "LONG") & long_cross).fillna(False)
    out["short_sig"] = ((out["regime"] == "SHORT") & short_cross).fillna(False)
    ws, we = _norm_time(window_start), _norm_time(window_end)
    out["in_window"] = out["_time"].between(ws, we)
    return out


def build_signals(spot: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Build the per-1-min signal frame from the continuous spot OHLC.

    `spot` needs columns: dt (tz-aware datetime), _date, _time, open, high, low,
    close, sorted chronologically. Returns a frame with _date, _time, dt,
    spot_close, dir1/dir2/dir3, ema_fast, ema_slow, regime, long_sig, short_sig,
    in_window.
    """
    cols = ["_date", "_time", "dt", "spot_close", "dir1", "dir2", "dir3",
            "ema_fast", "ema_slow", "regime", "long_sig", "short_sig",
            "in_window"]
    if spot.empty:
        return pd.DataFrame(columns=cols)

    spot = spot.sort_values("dt").reset_index(drop=True)
    close = spot["close"]
    ema_f = get_indicator("EMA", name="ema_fast",
                          period=params["ema_fast"]).calculate(close)
    ema_s = get_indicator("EMA", name="ema_slow",
                          period=params["ema_slow"]).calculate(close)

    out = pd.DataFrame({
        "_date": spot["_date"].values,
        "_time": spot["_time"].values,
        "dt": spot["dt"].values,
        "spot_close": close.values,
        "ema_fast": ema_f.values,
        "ema_slow": ema_s.values,
    })
    factor, atr_p = params["st_factor"], params["st_atr_period"]
    attach = (_attach_htf_dir_rolling
              if str(params.get("htf_mode", "rolling")).lower() == "rolling"
              else _attach_htf_dir)
    out["dir1"] = attach(spot, params["tf1_min"], factor, atr_p)
    out["dir2"] = attach(spot, params["tf2_min"], factor, atr_p)
    out["dir3"] = attach(spot, params["tf3_min"], factor, atr_p)

    out = _finalize_signals(out, params["window_start"], params["window_end"])
    return out[cols]


# --------------------------------------------------------------------------- #
#  Per-day driver                                                             #
# --------------------------------------------------------------------------- #

def _leg_maps(day_options: pd.DataFrame, time_col: pd.Series, option_type: str,
              strike: float) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Minute -> open / close price maps for one contract (first row per minute)."""
    sub = day_options[(day_options["option_type"] == option_type)
                      & (day_options["strike"] == strike)]
    if sub.empty:
        return {}, {}
    sub = sub.assign(_t=time_col[sub.index].values).drop_duplicates("_t")
    open_map = dict(zip(sub["_t"], sub["open"].astype(float)))
    close_map = dict(zip(sub["_t"], sub["close"].astype(float)))
    return open_map, close_map


class _OpenPosition:
    """Mutable in-flight trade state while scanning a day."""

    def __init__(self, trade: TripleStEmaTrade, entry_minute: str,
                 option_type: str, sell_strike: float, buy_strike: float,
                 sell_entry: float, buy_entry: float):
        self.trade = trade
        self.entry_minute = entry_minute      # HH:MM:SS (fill minute)
        self.option_type = option_type
        self.sell_strike = sell_strike
        self.buy_strike = buy_strike
        self.sell_entry = sell_entry
        self.buy_entry = buy_entry
        self.sell_open: Dict[str, float] = {}
        self.sell_close: Dict[str, float] = {}
        self.buy_open: Dict[str, float] = {}
        self.buy_close: Dict[str, float] = {}


def _open_position(day_options: pd.DataFrame, time_col: pd.Series, t: str,
                   signal: str, ctx: TripleStEmaDayContext,
                   spot_close_map: Dict[str, float]
                   ) -> Tuple[Optional[_OpenPosition], Optional[str]]:
    """Resolve strikes from the SIGNAL bar's spot close and fill at the option
    CLOSE prices of the same minute.

    Returns (position, None) on success, or (None, reason) on a skip, where
    reason is one of SKIP_REASONS:
      * spot_missing       -- no spot close at this minute,
      * sell_leg_missing   -- the SELL strike isn't in the chain this minute,
      * buy_leg_missing    -- the BUY strike isn't in the chain this minute,
      * nonpositive_credit -- sell_close <= buy_close (would be a debit; the
        structure should always be a credit, so in practice this only fires on
        stale/illiquid far-OTM ticks).
    """
    spot = spot_close_map.get(t)
    if spot is None or isnan(spot):
        return None, "spot_missing"
    step = ctx.strike_step
    atm = round(spot / step) * step

    if signal == "LONG":
        opt, direction, spread = "PE", "LONG", "BULL_PUT"
        sell_off, buy_off = -ctx.sell_offset_abs, -ctx.buy_offset_abs
    else:  # SHORT
        opt, direction, spread = "CE", "SHORT", "BEAR_CALL"
        sell_off, buy_off = ctx.sell_offset_abs, ctx.buy_offset_abs

    sell_strike = float(atm + sell_off * step)
    buy_strike = float(atm + buy_off * step)

    sell_open, sell_close = _leg_maps(day_options, time_col, opt, sell_strike)
    buy_open, buy_close = _leg_maps(day_options, time_col, opt, buy_strike)
    if t not in sell_close or isnan(sell_close[t]):
        return None, "sell_leg_missing"
    if t not in buy_close or isnan(buy_close[t]):
        return None, "buy_leg_missing"
    sell_entry = sell_close[t]
    buy_entry = buy_close[t]

    net_credit_pts = sell_entry - buy_entry
    if net_credit_pts <= 0:
        return None, "nonpositive_credit"

    contracts = ctx.lot_size * ctx.lots
    trade = TripleStEmaTrade(
        date=ctx.date, signal=signal, spread=spread, direction=direction,
        expiry_code=ctx.expiry_code,
        entry_time=t[:5], entry_spot=float(spot), atm_strike=float(atm),
        exit_time="", exit_reason="", exit_spot=float("nan"),
        fill_fallback=False,
        net_credit_pts=net_credit_pts,
        net_credit_inr=net_credit_pts * contracts,
        pnl_pts=0.0, pnl_inr=0.0,
        return_pct=0.0, running_equity_inr=0.0,
        legs={
            "sell": LegFill(opt, "SELL", ctx.lots, sell_off, sell_strike,
                            sell_entry),
            "buy": LegFill(opt, "BUY", ctx.lots, buy_off, buy_strike,
                           buy_entry),
        },
    )
    pos = _OpenPosition(trade, t, opt, sell_strike, buy_strike,
                        sell_entry, buy_entry)
    pos.sell_open, pos.sell_close = sell_open, sell_close
    pos.buy_open, pos.buy_close = buy_open, buy_close
    return pos, None


def _close_position(pos: _OpenPosition, start_idx: int, reason: str,
                    minutes: List[str], square: str,
                    spot_map: Dict[str, float], ctx: TripleStEmaDayContext,
                    price_field: str = "open") -> None:
    """Fill the exit at the first minute >= start_idx (capped at square-off)
    where both legs have rows, using `price_field` prices. If none forward, fall
    back to the last known CLOSE before start_idx. Never drops the trade."""
    trade = pos.trade
    n = len(minutes)
    fill_minute = None
    used = price_field

    omap_s = pos.sell_open if price_field == "open" else pos.sell_close
    omap_b = pos.buy_open if price_field == "open" else pos.buy_close
    for j in range(start_idx, n):
        m = minutes[j]
        if m > square:
            break
        if m in omap_s and m in omap_b:
            fill_minute = m
            break

    if fill_minute is None:                     # backward fallback -> CLOSE
        used = "close"
        for j in range(min(start_idx, n) - 1, -1, -1):
            m = minutes[j]
            if m in pos.sell_close and m in pos.buy_close:
                fill_minute = m
                break

    sell_entry, buy_entry = pos.sell_entry, pos.buy_entry

    if fill_minute is None:
        anchor = min(start_idx, n - 1) if n else 0
        trade.exit_time = minutes[anchor][:5] if n else ""
        trade.exit_spot = float("nan")
        trade.fill_fallback = True
        sell_exit, buy_exit = sell_entry, buy_entry
    else:
        if used == "open":
            sell_exit, buy_exit = pos.sell_open[fill_minute], pos.buy_open[fill_minute]
        else:
            sell_exit, buy_exit = pos.sell_close[fill_minute], pos.buy_close[fill_minute]
        intended = minutes[start_idx] if start_idx < n else None
        trade.exit_time = fill_minute[:5]
        trade.exit_spot = float(spot_map.get(fill_minute, float("nan")))
        trade.fill_fallback = (trade.fill_fallback or (fill_minute != intended)
                               or (used != price_field))

    trade.legs["sell"].exit_price = sell_exit
    trade.legs["buy"].exit_price = buy_exit
    trade.exit_reason = reason

    contracts = ctx.lot_size * ctx.lots
    trade.pnl_pts = (sell_entry + buy_exit) - (sell_exit + buy_entry)
    trade.pnl_inr = trade.pnl_pts * contracts


def run_one_day(day_options: pd.DataFrame, day_signals: pd.DataFrame,
                ctx: TripleStEmaDayContext
                ) -> Tuple[List[TripleStEmaTrade], Dict[str, int]]:
    """Run the strategy over one trading day.

    `day_options` -- this day's option minute bars for the chosen expiry code.
    `day_signals` -- this day's signal rows (needs _time, in_window, long_sig,
    short_sig, spot_close). Returns (trades, skip_counts) where skip_counts maps
    each SKIP_REASONS entry to how many qualifying signals it dropped.
    """
    trades: List[TripleStEmaTrade] = []
    skips = _empty_skips()
    if day_options.empty or day_signals.empty:
        return trades, skips

    time_col = day_options["_time"] if "_time" in day_options.columns else \
        day_options["datetime"].str.slice(11, 19)
    square = _norm_time(ctx.square_off_time)
    minutes = sorted(m for m in time_col.unique() if m <= square)
    if not minutes:
        return trades, skips

    spot_map = {_norm_time(t): float(s) for t, s in
                zip(day_signals["_time"], day_signals["spot_close"])}

    entry_sig_at: Dict[str, str] = {}
    for _, r in day_signals.iterrows():
        if not bool(r["in_window"]):
            continue
        t = _norm_time(r["_time"])
        if bool(r["long_sig"]):
            entry_sig_at[t] = "LONG"
        elif bool(r["short_sig"]):
            entry_sig_at[t] = "SHORT"

    pos: Optional[_OpenPosition] = None
    pending_exit: Optional[str] = None
    trades_today = 0
    cap = ctx.max_trades_per_day            # 0 -> unlimited
    tp_thr = ctx.tp_inr * ctx.lots
    sl_thr = ctx.sl_inr * ctx.lots
    tp_active = ctx.tp_inr > 0
    sl_active = ctx.sl_inr > 0
    contracts = ctx.lot_size * ctx.lots

    for i, t in enumerate(minutes):
        is_square = (t == square)

        # --- (A) Pending EXIT fills at this minute's OPEN -------------------
        if pending_exit is not None and pos is not None:
            _close_position(pos, i, pending_exit, minutes, square, spot_map,
                            ctx, price_field="open")
            pos = None
            pending_exit = None

        # --- (B) Square-off: flatten at the deadline's CLOSE ----------------
        if is_square:
            if pos is not None:
                _close_position(pos, i, "EOD", minutes, square, spot_map,
                                ctx, price_field="close")
                pos = None
            break

        # --- (C) INR SL/TP detection on this 1-min option close -------------
        if pos is not None and pending_exit is None and t > pos.entry_minute:
            sc = pos.sell_close.get(t)
            bc = pos.buy_close.get(t)
            if (sc is not None and bc is not None
                    and not isnan(sc) and not isnan(bc)):
                live_pts = (pos.sell_entry + bc) - (sc + pos.buy_entry)
                live_inr = live_pts * contracts
                sl_hit = sl_active and live_inr <= -sl_thr
                tp_hit = tp_active and live_inr >= tp_thr
                if sl_hit or tp_hit:
                    reason = "SL" if sl_hit else "TP"
                    if i + 1 < len(minutes):
                        pending_exit = reason          # fill next minute's OPEN
                    else:
                        _close_position(pos, i, reason, minutes, square,
                                        spot_map, ctx, price_field="close")
                        pos = None

        # --- (D) Entry fills at THIS minute's CLOSE (the signal bar) --------
        if (t in entry_sig_at and pos is None and not is_square
                and (cap <= 0 or trades_today < cap)):
            sig = entry_sig_at[t]
            new_pos, reason = _open_position(day_options, time_col, t, sig,
                                             ctx, spot_map)
            if new_pos is None:
                skips[reason] += 1
                logger.warning(f"{ctx.date} {t}: {sig} entry skipped ({reason}).")
            else:
                pos = new_pos
                trades.append(new_pos.trade)
                trades_today += 1

    if pos is not None:
        # Data ended before the square-off bar: close on the last minute.
        _close_position(pos, len(minutes) - 1, "EOD", minutes, square,
                        spot_map, ctx, price_field="close")

    return trades, skips


# --------------------------------------------------------------------------- #
#  Data loading                                                               #
# --------------------------------------------------------------------------- #

def load_filtered_options(options_path: str, start_date: str, end_date: str,
                          window_start: str = DEFAULT_WINDOW_START,
                          square_off_time: str = DEFAULT_SQUARE_OFF_TIME
                          ) -> pd.DataFrame:
    """Predicate-pushdown load of NIFTY weekly options (codes 1 and 2) within
    [window_start, square_off_time]. Options need no warm-up (no continuous
    indicator runs on them)."""
    import pyarrow.parquet as pq

    filters = [
        ("underlying", "=", "NIFTY"),
        ("expiry_type", "=", "WEEK"),
        ("expiry_code", "in", [1, 2]),
    ]
    logger.info("Loading options parquet with predicate pushdown...")
    table = pq.read_table(options_path, columns=OPT_COLS, filters=filters)
    df = table.to_pandas()
    if df.empty:
        return df

    d = df["datetime"].str.slice(0, 10)
    df = df[(d >= start_date) & (d <= end_date)]
    if df.empty:
        return df

    lower = _norm_time(window_start)
    upper = _norm_time(square_off_time)
    tcol = df["datetime"].str.slice(11, 19)
    keep = (tcol >= lower) & (tcol <= upper)
    df = df[keep].copy()
    df["_time"] = tcol[keep].values
    df["_date"] = df["datetime"].str.slice(0, 10).values
    logger.info(f"Loaded {len(df):,} option rows for {df['_date'].nunique()} "
                "trading days (codes 1+2).")
    return df


def load_spot_1m(spot_path: str, start_date: str, end_date: str,
                 warmup_days: int = DEFAULT_WARMUP_DAYS,
                 session_start: str = SESSION_START,
                 session_end: str = SPOT_SESSION_END) -> pd.DataFrame:
    """Load the underlying's true 1-min OHLC -- the signal source. Loads
    [start_date - 2*warmup_days, end_date] within [session_start, session_end]
    so SuperTrend/EMA are seeded continuously. Returns dt (naive IST wall-clock),
    _date, _time, open, high, low, close."""
    cols = ["dt", "_date", "_time", "open", "high", "low", "close"]
    load_start = (_date.fromisoformat(start_date)
                  - timedelta(days=2 * warmup_days)).isoformat()
    df = pd.read_parquet(spot_path,
                         columns=["datetime", "open", "high", "low", "close"])
    d = df["datetime"].str.slice(0, 10)
    df = df[(d >= load_start) & (d <= end_date)]
    if df.empty:
        return pd.DataFrame(columns=cols)

    lower, upper = _norm_time(session_start), _norm_time(session_end)
    tcol = df["datetime"].str.slice(11, 19)
    keep = (tcol >= lower) & (tcol <= upper)
    df = df[keep].copy()
    # Keep the IST WALL-CLOCK time (drop the +05:30 tz). Calling .values on a
    # tz-aware series converts to UTC and shifts every bar by -5:30, which would
    # break the anchored HTF resample (`between_time`/`resample(offset=09:15)`).
    # Parsing the tz-less prefix keeps 09:15 as 09:15.
    df["dt"] = pd.to_datetime(df["datetime"].str.slice(0, 19)).values
    df["_date"] = df["datetime"].str.slice(0, 10).values
    df["_time"] = tcol[keep].values
    logger.info(f"Loaded {len(df):,} spot 1-min bars (incl. warm-up from "
                f"{load_start}).")
    return df[cols].sort_values("dt").reset_index(drop=True)


# --------------------------------------------------------------------------- #
#  Backtest orchestrator                                                      #
# --------------------------------------------------------------------------- #

def parse_config(config: dict) -> dict:
    signal = config.get("signal", {}) or {}
    entry = config.get("entry", {}) or {}
    exit_cfg = config.get("exit", {}) or {}
    structure = config.get("structure", {}) or {}
    expiry = config.get("expiry", {}) or {}
    return {
        "st_factor": float(signal.get("st_factor", DEFAULT_ST_FACTOR)),
        "st_atr_period": int(signal.get("st_atr_period", DEFAULT_ST_ATR_PERIOD)),
        "htf_mode": str(signal.get("htf_mode", DEFAULT_HTF_MODE)).lower(),
        "tf1_min": int(signal.get("tf1_min", DEFAULT_TF1_MIN)),
        "tf2_min": int(signal.get("tf2_min", DEFAULT_TF2_MIN)),
        "tf3_min": int(signal.get("tf3_min", DEFAULT_TF3_MIN)),
        "ema_fast": int(signal.get("ema_fast", DEFAULT_EMA_FAST)),
        "ema_slow": int(signal.get("ema_slow", DEFAULT_EMA_SLOW)),
        "warmup_days": int(signal.get("warmup_days", DEFAULT_WARMUP_DAYS)),
        "window_start": _norm_time(entry.get("window_start", DEFAULT_WINDOW_START)),
        "window_end": _norm_time(entry.get("window_end", DEFAULT_WINDOW_END)),
        "tp_inr": float(exit_cfg.get("tp_inr", DEFAULT_TP_INR)),
        "sl_inr": float(exit_cfg.get("sl_inr", DEFAULT_SL_INR)),
        "square_off_time": _norm_time(exit_cfg.get("square_off_time",
                                                   DEFAULT_SQUARE_OFF_TIME)),
        "lots": int(structure.get("lots", 1)),
        "sell_offset_abs": int(structure.get("sell_offset_abs", DEFAULT_SELL_OFFSET)),
        "buy_offset_abs": int(structure.get("buy_offset_abs", DEFAULT_BUY_OFFSET)),
        "max_trades_per_day": int(structure.get("max_trades_per_day",
                                                DEFAULT_MAX_TRADES_PER_DAY)),
        "strike_step": int(structure.get("strike_step",
                                         STRIKE_ROUNDING.get("NIFTY", 50))),
        "expiry_roll": bool(expiry.get("expiry_roll", True)),
        "reference_capital": float(config["sizing"]["reference_capital"]),
    }


def _expiry_code_for(date_str: str, expiry_roll: bool) -> int:
    """Roll to expiry_code 2 on a weekly-expiry day, else nearest weekly (1)."""
    if not expiry_roll:
        return 1
    d = _date.fromisoformat(date_str)
    return 2 if get_nearest_weekly_expiry(d) == d else 1


def run_backtest(options_df: pd.DataFrame, spot_df: pd.DataFrame,
                 config: dict) -> dict:
    """Run the strategy over [backtest_start, backtest_end].

    `spot_df` may include warm-up days before backtest_start (to seed the
    indicators). `options_df` carries weekly codes 1 and 2.
    """
    p = parse_config(config)
    bt_start = config["backtest_start"]
    bt_end = config["backtest_end"]
    capital = p["reference_capital"]

    trades: List[TripleStEmaTrade] = []
    skip_counts = _empty_skips()
    days_processed = 0
    running_equity = capital

    if options_df.empty or spot_df.empty:
        return {"trades": trades, "config": config,
                "signals_skipped": 0, "skip_reason_counts": _empty_skips(),
                "days_processed": 0}

    signals = build_signals(spot_df, p)

    bt_opt = options_df[(options_df["_date"] >= bt_start)
                        & (options_df["_date"] <= bt_end)]
    for date_str, day_opt_all in bt_opt.groupby("_date", sort=True):
        days_processed += 1
        code = _expiry_code_for(str(date_str), p["expiry_roll"])
        day_opt = day_opt_all[day_opt_all["expiry_code"] == code]
        if day_opt.empty:
            logger.warning(f"{date_str}: no rows for expiry_code {code}; skipping.")
            continue
        day_sig = signals[signals["_date"] == str(date_str)]
        if day_sig.empty:
            continue
        ctx = TripleStEmaDayContext(
            date=str(date_str), expiry_code=code, lots=p["lots"],
            sell_offset_abs=p["sell_offset_abs"], buy_offset_abs=p["buy_offset_abs"],
            tp_inr=p["tp_inr"], sl_inr=p["sl_inr"],
            square_off_time=p["square_off_time"],
            max_trades_per_day=p["max_trades_per_day"],
            strike_step=p["strike_step"],
        )
        day_trades, day_skips = run_one_day(day_opt, day_sig, ctx)
        for k, v in day_skips.items():
            skip_counts[k] += v
        for t in day_trades:
            running_equity += t.pnl_inr
            t.return_pct = t.pnl_inr / capital if capital else 0.0
            t.running_equity_inr = running_equity
        trades.extend(day_trades)

    return {"trades": trades, "config": config,
            "signals_skipped": sum(skip_counts.values()),
            "skip_reason_counts": skip_counts,
            "days_processed": days_processed}


# --------------------------------------------------------------------------- #
#  Reporting                                                                  #
# --------------------------------------------------------------------------- #

def build_equity_curve(trades: List[TripleStEmaTrade],
                       starting_capital: float) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=["date", "exit_time", "equity_inr",
                                     "drawdown_inr", "drawdown_pct"])
    rows = []
    peak = starting_capital
    for t in trades:
        equity = t.running_equity_inr
        peak = max(peak, equity)
        dd_inr = peak - equity
        rows.append({
            "date": t.date,
            "exit_time": t.exit_time,
            "equity_inr": equity,
            "drawdown_inr": dd_inr,
            "drawdown_pct": dd_inr / peak if peak else 0.0,
        })
    return pd.DataFrame(rows)


def max_consecutive_losses(pnls: List[float]) -> int:
    longest = 0
    current = 0
    for p in pnls:
        if p < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _count_by(items, keyfn):
    counts: Dict[str, int] = {}
    for it in items:
        k = keyfn(it)
        counts[k] = counts.get(k, 0) + 1
    return counts


def summarize_metrics(trades: List[TripleStEmaTrade],
                      starting_capital: float) -> dict:
    pnls = [t.pnl_inr for t in trades]
    wins = [t for t in trades if t.pnl_inr > 0]
    losses = [t for t in trades if t.pnl_inr < 0]

    equity_curve = build_equity_curve(trades, starting_capital)
    if not equity_curve.empty:
        max_dd_inr = float(equity_curve["drawdown_inr"].max())
        max_dd_pct = float(equity_curve["drawdown_pct"].max())
    else:
        max_dd_inr = 0.0
        max_dd_pct = 0.0

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "mean_pnl_inr": statistics.fmean(pnls) if pnls else 0.0,
        "median_pnl_inr": statistics.median(pnls) if pnls else 0.0,
        "total_pnl_inr": sum(pnls),
        "total_return_pct": sum(pnls) / starting_capital if starting_capital else 0.0,
        "max_drawdown_inr": max_dd_inr,
        "max_drawdown_pct": max_dd_pct,
        "max_consecutive_losses": max_consecutive_losses(pnls),
        "best_trade_inr": max(pnls) if pnls else 0.0,
        "worst_trade_inr": min(pnls) if pnls else 0.0,
        "long_trades": sum(1 for t in trades if t.signal == "LONG"),
        "short_trades": sum(1 for t in trades if t.signal == "SHORT"),
        "exit_reason_counts": _count_by(trades, lambda t: t.exit_reason),
        "rolled_trades": sum(1 for t in trades if t.expiry_code == 2),
        "fill_fallback_count": sum(1 for t in trades if t.fill_fallback),
        "trading_days": len({t.date for t in trades}),
    }


def trades_to_dataframe(trades: List[TripleStEmaTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    rows = []
    for t in trades:
        row = {k: v for k, v in asdict(t).items() if k != "legs"}
        for leg_key, leg in t.legs.items():
            row[f"{leg_key}_option_type"] = leg.option_type
            row[f"{leg_key}_strike"] = leg.strike
            row[f"{leg_key}_offset"] = leg.strike_offset
            row[f"{leg_key}_entry"] = leg.entry_price
            row[f"{leg_key}_exit"] = leg.exit_price
        rows.append(row)
    return pd.DataFrame(rows)


def write_trades_csv(trades: List[TripleStEmaTrade], path) -> None:
    trades_to_dataframe(trades).to_csv(path, index=False)


def write_equity_csv(trades: List[TripleStEmaTrade], capital: float, path) -> None:
    build_equity_curve(trades, capital).to_csv(path, index=False)


def print_summary(s: dict, days_processed: int, skip_counts: dict) -> None:
    total_skipped = sum(skip_counts.values())
    lines = [
        f"Days processed: {days_processed}    Days with trades: {s['trading_days']}",
        f"Trades: {s['total_trades']}  (LONG={s['long_trades']}, "
        f"SHORT={s['short_trades']}, rolled={s['rolled_trades']})",
        f"Signals skipped: {total_skipped}  "
        f"(buy_leg_missing={skip_counts['buy_leg_missing']}, "
        f"sell_leg_missing={skip_counts['sell_leg_missing']}, "
        f"spot_missing={skip_counts['spot_missing']}, "
        f"nonpositive_credit={skip_counts['nonpositive_credit']})",
        f"Wins / Losses: {s['wins']} / {s['losses']}  ({s['win_rate']*100:.2f}% win-rate)",
        f"Exit reasons: {s['exit_reason_counts']}",
        f"Fill fallbacks: {s['fill_fallback_count']}",
        f"Mean P&L: Rs {s['mean_pnl_inr']:.2f}    Median: Rs {s['median_pnl_inr']:.2f}",
        f"Total P&L: Rs {s['total_pnl_inr']:.2f}",
        f"Total return on reference capital: {s['total_return_pct']*100:.2f}%",
        f"Max drawdown: Rs {s['max_drawdown_inr']:.2f}  ({s['max_drawdown_pct']*100:.2f}%)",
        f"Max consecutive losing trades: {s['max_consecutive_losses']}",
        f"Best trade: Rs {s['best_trade_inr']:.2f}    Worst trade: Rs {s['worst_trade_inr']:.2f}",
    ]
    for line in lines:
        print(line)


def run(config: dict, options_path: str, output_dir: str,
        spot_path: Optional[str] = None) -> dict:
    """Top-level entrypoint. Loads parquet, runs backtest, writes CSVs."""
    p = parse_config(config)
    options_df = load_filtered_options(
        options_path,
        start_date=config["backtest_start"],
        end_date=config["backtest_end"],
        window_start=p["window_start"],
        square_off_time=p["square_off_time"],
    )
    spot_path = spot_path or SPOT_DATA_PATH.get("NIFTY")
    spot_df = load_spot_1m(
        spot_path, config["backtest_start"], config["backtest_end"],
        warmup_days=p["warmup_days"])

    result = run_backtest(options_df, spot_df, config)
    trades = result["trades"]
    capital = p["reference_capital"]
    summary = summarize_metrics(trades, capital)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    start_str = config["backtest_start"]
    end_str = config["backtest_end"]
    trades_path = out / f"triple_st_ema_spread_trades_{start_str}_{end_str}.csv"
    equity_path = out / f"triple_st_ema_spread_equity_{start_str}_{end_str}.csv"
    write_trades_csv(trades, trades_path)
    write_equity_csv(trades, capital, equity_path)
    print_summary(summary, result["days_processed"], result["skip_reason_counts"])
    return {
        "trades": trades, "summary": summary,
        "days_processed": result["days_processed"],
        "signals_skipped": result["signals_skipped"],
        "skip_reason_counts": result["skip_reason_counts"],
        "trades_csv": str(trades_path),
        "equity_csv": str(equity_path),
    }
