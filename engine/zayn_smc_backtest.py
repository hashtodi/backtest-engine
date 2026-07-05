"""
Zayn SMC Backtest Engine - 5-min spot SMC indicator drives NIFTY credit spreads.

Strategy:
  Indicator runs on 5-min resampled NIFTY 50 spot bars. It emits longSig /
  shortSig per bar:

    longSig  (bullish) -> SELL ATM-N PE, BUY ATM-M PE  (bull-put credit spread)
    shortSig (bearish) -> SELL ATM+N CE, BUY ATM+M CE  (bear-call credit spread)

  Indicator pieces (mirror of the Pine "Zayn SMC" indicator):
    - HTF bias: htf_close > EMA(htf_close, biasLen) on the bias TF (default 60m).
    - Session: 09:15-15:30 IST. No new entries after flat_window_start
      (default 15:15). Entries gated to bars at-or-after entry_earliest_time
      (default 09:20).
    - Opening range: high/low of the first OR_MINUTES of session locked once
      the OR window ends.
    - Prior-day H/L: previous trading day's high and low.
    - Swing pivots: ta.pivothigh / ta.pivotlow with lookback swing_lb.
    - Displacement: range > ATR(atr_len) * disp_mult AND body >= range * body_pct.
    - Sweep: bar's high pushes above a buy-side liquidity level (PDH, OR high,
      last swing high) by sweep_buf and CLOSES back below it -> sweep of highs.
      Mirror for sweep of lows.
    - Breaker arming: sweep + displacement (+ optional bias align) arms a
      retest window of retest_bars 5-min bars; the breaker box is the prior
      bar's body if it was opposite-coloured.
    - Entry trigger: while armed, low touches the breaker top and close stays
      above the breaker bottom (mirror for shorts).

  Execution (T+1 OPEN discipline like OI Wall / PCR Momentum):
    - Signal detected at 5-min close at time T -> entry at T+1 1-min OPEN of
      the option fills. ATM and leg strikes are resolved on the signal bar's
      option slice (no look-ahead). T = bar's end timestamp; e.g. the 5-min
      bar covering minutes [09:15..09:19] closes at 09:20; entry fills at
      09:21 1-min OPEN.

  Multiple trades per day. When in a position, three exits are scanned:
    - SL: live spread P&L <= -sl_inr * lots, detected at 1-min CLOSE, fills
      at next 1-min OPEN.
    - TP: live spread P&L >=  tp_inr * lots, same detection / fill rule.
    - OPP: an opposite indicator signal on a 5-min bar that closes after the
      entry's signal bar. Detected at the 5-min close, fills at the next
      1-min OPEN. When this fires we close the current spread AND open a
      fresh spread on the opposite side at the SAME next 1-min OPEN (auto-
      flip). Strikes for the new spread are resolved on the opposite-signal
      bar's option slice.
    - Priority within the same minute close: SL > TP. OPP fires only at
      5-min closes.

  Force exit at force_exit_time (default 15:20) close. All exits report
  reason in {"TP","SL","OPP","TIME"}. P&L scales with lots.
"""

import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date as _date, datetime as _dt, time as _time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from engine.oi_wall_backtest import (
    LOT_SIZE_NIFTY,
    LegFill,
    _norm_time,
    _plus_one_minute,
    atm_strike_from,
    build_equity_curve,
    lookup_by_offset,
    lookup_by_strike,
    max_consecutive_losses,
)

logger = logging.getLogger(__name__)

# Strategy defaults
DEFAULT_ENTRY_EARLIEST_TIME = "09:20:00"  # ignore signals before this
DEFAULT_FORCE_EXIT_TIME = "15:20:00"
DEFAULT_FLAT_WINDOW_START = "15:15:00"    # no new entries from here
DEFAULT_SESSION_START = "09:15:00"
DEFAULT_SESSION_END = "15:30:00"

# Indicator defaults (sweep-mode default; bias filter off)
DEFAULT_BIAS_TF_MIN = 60
DEFAULT_BIAS_LEN = 50
DEFAULT_USE_BIAS = False
DEFAULT_OR_MINUTES = 15
DEFAULT_USE_PDHL = True
DEFAULT_USE_ORHL = True
DEFAULT_SWING_LB = 10
DEFAULT_ATR_LEN = 14
DEFAULT_DISP_MULT = 1.5
DEFAULT_BODY_PCT = 0.5
DEFAULT_SWEEP_BUF = 0.0
DEFAULT_RETEST_BARS = 10
DEFAULT_SIGNAL_MODE = "sweep"  # "sweep" | "breaker"

# Option-side defaults
DEFAULT_SELL_OFFSET_ABS = 2
DEFAULT_BUY_OFFSET_ABS = 6

REQUIRED_OPT_COLS = [
    "datetime", "option_type", "expiry_type", "expiry_code",
    "strike_offset", "moneyness", "strike", "spot", "open", "close", "oi",
    "underlying",
]


# --------------------------------------------------------------------------- #
#  Dataclasses                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class ZaynSmcTrade:
    date: str
    trade_idx: int                 # 1-based within the day

    # Signal context
    signal_time: str               # 5-min bar close time, HH:MM
    signal_direction: str          # "LONG" | "SHORT"  (indicator side)
    side: str                      # "PE" (bull-put) | "CE" (bear-call)

    # Spread context
    atm_strike: float
    spot_at_entry: float
    spot_at_exit: float

    # Execution
    entry_time: str                # 1-min OPEN time, HH:MM
    exit_signal_time: str          # 1-min CLOSE (SL/TP) or 5-min CLOSE (OPP) or 15:20 (TIME)
    exit_time: str                 # fill time
    exit_reason: str               # "TP" | "SL" | "OPP" | "TIME"
    net_credit_pts: float
    net_credit_inr: float
    pnl_pts: float
    pnl_inr: float
    return_pct: float
    running_equity_inr: float

    skip_reason: Optional[str]
    legs: Dict[str, LegFill] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
#  Data loading + resampling                                                  #
# --------------------------------------------------------------------------- #

def load_spot_1m(path: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Load 1-min NIFTY spot OHLCV for [start_date, end_date] inclusive."""
    table = pq.read_table(path, columns=["datetime", "open", "high", "low", "close", "volume"])
    df = table.to_pandas()
    if df.empty:
        return df
    df["date"] = df["datetime"].str.slice(0, 10)
    df = df[(df["date"] >= start_date) & (df["date"] <= end_date)].copy()
    df["ts"] = pd.to_datetime(df["datetime"].str.slice(0, 19))
    df["time"] = df["datetime"].str.slice(11, 19)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def resample_intraday(df_1m: pd.DataFrame, minutes: int,
                      session_start: str = DEFAULT_SESSION_START,
                      session_end: str = DEFAULT_SESSION_END) -> pd.DataFrame:
    """Resample 1-min bars to N-minute bars, session-aligned (origin = session
    start of each day, so first 5-min bar = 09:15..09:19 closing at 09:20).
    The returned bar's `bar_close_ts` is the bar's CLOSE timestamp (i.e. one
    minute step after its last 1-min bar's stamp).
    """
    if df_1m.empty:
        return pd.DataFrame(columns=["bar_close_ts", "date", "time",
                                     "open", "high", "low", "close", "volume"])
    rows = []
    for date_str, day_df in df_1m.groupby("date", sort=True):
        ses_start = _dt.strptime(f"{date_str} {session_start[:8]}", "%Y-%m-%d %H:%M:%S")
        ses_end = _dt.strptime(f"{date_str} {session_end[:8]}", "%Y-%m-%d %H:%M:%S")
        day_df = day_df[(day_df["ts"] >= ses_start) & (day_df["ts"] < ses_end)]
        if day_df.empty:
            continue
        ts = day_df["ts"].to_numpy()
        offsets = ((day_df["ts"] - ses_start).dt.total_seconds().to_numpy() // 60).astype(int)
        bin_idx = offsets // minutes
        for b in np.unique(bin_idx):
            mask = bin_idx == b
            opens = day_df.loc[mask, "open"].to_numpy()
            highs = day_df.loc[mask, "high"].to_numpy()
            lows = day_df.loc[mask, "low"].to_numpy()
            closes = day_df.loc[mask, "close"].to_numpy()
            vols = day_df.loc[mask, "volume"].to_numpy()
            first_ts = ts[mask][0]
            # Close timestamp = first_ts + minutes (next bar's start).
            close_ts = pd.Timestamp(first_ts) + pd.Timedelta(minutes=minutes)
            rows.append({
                "bar_close_ts": close_ts,
                "date": date_str,
                "time": close_ts.strftime("%H:%M:%S"),
                "open": float(opens[0]),
                "high": float(highs.max()),
                "low": float(lows.min()),
                "close": float(closes[-1]),
                "volume": int(vols.sum()),
            })
    out = pd.DataFrame(rows).sort_values("bar_close_ts").reset_index(drop=True)
    return out


def daily_prior_high_low(df_1m: pd.DataFrame) -> Dict[str, Tuple[float, float]]:
    """Return {date_str: (pdh, pdl)} where (pdh, pdl) are PREVIOUS trading
    day's high/low."""
    if df_1m.empty:
        return {}
    daily = df_1m.groupby("date").agg(
        high=("high", "max"), low=("low", "min")
    ).reset_index().sort_values("date")
    dates = daily["date"].tolist()
    highs = daily["high"].tolist()
    lows = daily["low"].tolist()
    out = {}
    for i, d in enumerate(dates):
        if i == 0:
            out[d] = (float("nan"), float("nan"))
        else:
            out[d] = (highs[i - 1], lows[i - 1])
    return out


# --------------------------------------------------------------------------- #
#  Indicator state                                                            #
# --------------------------------------------------------------------------- #

@dataclass
class IndicatorParams:
    signal_mode: str = DEFAULT_SIGNAL_MODE  # "sweep" or "breaker"
    bias_tf_min: int = DEFAULT_BIAS_TF_MIN
    bias_len: int = DEFAULT_BIAS_LEN
    use_bias: bool = DEFAULT_USE_BIAS
    or_minutes: int = DEFAULT_OR_MINUTES
    use_pdhl: bool = DEFAULT_USE_PDHL
    use_orhl: bool = DEFAULT_USE_ORHL
    swing_lb: int = DEFAULT_SWING_LB
    atr_len: int = DEFAULT_ATR_LEN
    disp_mult: float = DEFAULT_DISP_MULT
    body_pct: float = DEFAULT_BODY_PCT
    sweep_buf: float = DEFAULT_SWEEP_BUF
    retest_bars: int = DEFAULT_RETEST_BARS
    session_start: str = DEFAULT_SESSION_START
    session_end: str = DEFAULT_SESSION_END
    entry_earliest_time: str = DEFAULT_ENTRY_EARLIEST_TIME
    flat_window_start: str = DEFAULT_FLAT_WINDOW_START


def _wilder_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                length: int) -> np.ndarray:
    """Wilder's ATR (RMA of true range). Matches Pine's ta.atr default."""
    n = len(highs)
    tr = np.zeros(n, dtype=float)
    for i in range(n):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(highs[i] - lows[i],
                        abs(highs[i] - closes[i - 1]),
                        abs(lows[i] - closes[i - 1]))
    atr = np.full(n, np.nan)
    if n == 0:
        return atr
    # Wilder seed = simple average of the first `length` TRs.
    if n >= length:
        atr[length - 1] = tr[:length].mean()
        for i in range(length, n):
            atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / length
    return atr


def _pivot_points(values: np.ndarray, lb: int, kind: str) -> np.ndarray:
    """Return `confirmed[i] = pivot value at (i - lb)` if a pivot is confirmed
    at index `i`, else NaN. Strict pivot (the centre must be the unique
    extreme of its window)."""
    n = len(values)
    out = np.full(n, np.nan)
    if n < 2 * lb + 1:
        return out
    for i in range(2 * lb, n):
        window = values[i - 2 * lb: i + 1]
        center_idx_local = lb
        centre = values[i - lb]
        if np.isnan(centre):
            continue
        if kind == "high":
            argmax = int(np.argmax(window))
            if argmax == center_idx_local:
                if all(values[i - 2 * lb + j] < centre
                       for j in range(2 * lb + 1) if j != center_idx_local):
                    out[i] = centre
        else:
            argmin = int(np.argmin(window))
            if argmin == center_idx_local:
                if all(values[i - 2 * lb + j] > centre
                       for j in range(2 * lb + 1) if j != center_idx_local):
                    out[i] = centre
    return out


def _ema(values: np.ndarray, length: int) -> np.ndarray:
    """EMA matching Pine's ta.ema (seeded with SMA of first `length`)."""
    n = len(values)
    out = np.full(n, np.nan)
    if n == 0 or length <= 0:
        return out
    if n < length:
        return out
    alpha = 2.0 / (length + 1)
    out[length - 1] = values[:length].mean()
    for i in range(length, n):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def compute_indicator_state(
    df_5m: pd.DataFrame,
    df_htf: pd.DataFrame,
    daily_hl: Dict[str, Tuple[float, float]],
    params: IndicatorParams,
) -> pd.DataFrame:
    """Compute per-bar indicator state on the 5-min frame.

    Returns df_5m augmented with columns:
      in_session, can_enter, bias_up, bias_dn, displacement_up, displacement_dn,
      swept_high, swept_low, long_sig, short_sig,
      long_brk_top, long_brk_bot, short_brk_top, short_brk_bot.
    """
    df = df_5m.copy().reset_index(drop=True)
    n = len(df)
    if n == 0:
        return df

    ses_start = _norm_time(params.session_start)
    ses_end = _norm_time(params.session_end)
    flat_start = _norm_time(params.flat_window_start)
    entry_start = _norm_time(params.entry_earliest_time)

    # in_session uses the bar's CLOSE time. A 5-min bar closing at 09:20
    # belongs to the session (its data is 09:15..09:19).
    bar_close_time = df["time"].astype(str)
    in_session = (bar_close_time > ses_start) & (bar_close_time <= ses_end)
    in_flat = (bar_close_time > flat_start) & (bar_close_time <= ses_end)
    earliest_ok = bar_close_time >= entry_start
    can_enter = in_session & ~in_flat & earliest_ok
    df["in_session"] = in_session.values
    df["can_enter"] = can_enter.values

    # ---- HTF bias ----
    if not df_htf.empty and params.use_bias:
        htf_close = df_htf["close"].to_numpy()
        htf_ema = _ema(htf_close, params.bias_len)
        htf_ts = df_htf["bar_close_ts"].to_numpy()
        # For each 5-min bar at time T, find the most recently CLOSED HTF bar
        # (close_ts <= T) -- lookahead_off equivalent.
        bar_ts = df["bar_close_ts"].to_numpy()
        idx = np.searchsorted(htf_ts, bar_ts, side="right") - 1
        bias_up = np.zeros(n, dtype=bool)
        bias_dn = np.zeros(n, dtype=bool)
        for i in range(n):
            j = idx[i]
            if j >= 0 and not np.isnan(htf_ema[j]):
                if htf_close[j] > htf_ema[j]:
                    bias_up[i] = True
                elif htf_close[j] < htf_ema[j]:
                    bias_dn[i] = True
    else:
        bias_up = np.ones(n, dtype=bool)
        bias_dn = np.ones(n, dtype=bool)
    df["bias_up"] = bias_up
    df["bias_dn"] = bias_dn

    # ---- Displacement ----
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    open_ = df["open"].to_numpy()
    close = df["close"].to_numpy()
    rng = high - low
    body = np.abs(close - open_)
    atr = _wilder_atr(high, low, close, params.atr_len)
    with np.errstate(invalid="ignore"):
        is_disp = (rng > atr * params.disp_mult) & (body >= rng * params.body_pct)
    is_disp = np.where(np.isnan(atr), False, is_disp)
    disp_up = is_disp & (close > open_)
    disp_dn = is_disp & (close < open_)
    df["displacement_up"] = disp_up
    df["displacement_dn"] = disp_dn

    # ---- Swing pivots (lastSwingH / lastSwingL after confirmation) ----
    ph = _pivot_points(high, params.swing_lb, "high")
    pl = _pivot_points(low, params.swing_lb, "low")
    last_swing_h = np.full(n, np.nan)
    last_swing_l = np.full(n, np.nan)
    cur_h = np.nan
    cur_l = np.nan
    for i in range(n):
        if not np.isnan(ph[i]):
            cur_h = ph[i]
        if not np.isnan(pl[i]):
            cur_l = pl[i]
        last_swing_h[i] = cur_h
        last_swing_l[i] = cur_l

    # ---- Opening range (per session) ----
    # Empirical match to TradingView: OR contains `int(orMins / tf_min)` bars
    # (3 bars when orMins=15 and tf=5). On the bar that triggers the lock,
    # the OR is FROZEN BEFORE that bar's high/low are added.
    or_h = np.full(n, np.nan)
    or_l = np.full(n, np.nan)
    or_locked = np.zeros(n, dtype=bool)
    cur_orh = np.nan
    cur_orl = np.nan
    cur_locked = False
    cur_date = None
    bar_idx_in_session = 0
    or_end_bar_offset = max(1, int(params.or_minutes // 5))
    for i in range(n):
        d = df.at[i, "date"]
        if d != cur_date:
            cur_orh = float(df.at[i, "high"])
            cur_orl = float(df.at[i, "low"])
            cur_locked = False
            cur_date = d
            bar_idx_in_session = 0
        else:
            bar_idx_in_session += 1
            if not cur_locked:
                # Lock CHECK happens before updating the OR on this bar.
                if bar_idx_in_session >= or_end_bar_offset:
                    cur_locked = True
                else:
                    cur_orh = max(cur_orh, float(df.at[i, "high"]))
                    cur_orl = min(cur_orl, float(df.at[i, "low"]))
        or_h[i] = cur_orh
        or_l[i] = cur_orl
        or_locked[i] = cur_locked

    # ---- Prior-day H/L per bar ----
    pdh = np.full(n, np.nan)
    pdl = np.full(n, np.nan)
    for i in range(n):
        h, l = daily_hl.get(df.at[i, "date"], (float("nan"), float("nan")))
        pdh[i] = h
        pdl[i] = l

    # ---- Sweep detection ----
    swept_high = np.zeros(n, dtype=bool)
    swept_low = np.zeros(n, dtype=bool)
    swept_h_lvl = np.full(n, np.nan)
    swept_l_lvl = np.full(n, np.nan)

    def _swept_high(i: int) -> Tuple[bool, float]:
        cands: List[float] = []
        if params.use_pdhl and not np.isnan(pdh[i]):
            cands.append(pdh[i])
        if params.use_orhl and or_locked[i] and not np.isnan(or_h[i]):
            cands.append(or_h[i])
        if not np.isnan(last_swing_h[i]):
            cands.append(last_swing_h[i])
        best = np.nan
        for lvl in cands:
            if high[i] > lvl + params.sweep_buf and close[i] < lvl:
                best = lvl if np.isnan(best) else max(best, lvl)
        return (not np.isnan(best)), best

    def _swept_low(i: int) -> Tuple[bool, float]:
        cands: List[float] = []
        if params.use_pdhl and not np.isnan(pdl[i]):
            cands.append(pdl[i])
        if params.use_orhl and or_locked[i] and not np.isnan(or_l[i]):
            cands.append(or_l[i])
        if not np.isnan(last_swing_l[i]):
            cands.append(last_swing_l[i])
        best = np.nan
        for lvl in cands:
            if low[i] < lvl - params.sweep_buf and close[i] > lvl:
                best = lvl if np.isnan(best) else min(best, lvl)
        return (not np.isnan(best)), best

    for i in range(n):
        sh, lh = _swept_high(i)
        sl, ll = _swept_low(i)
        swept_high[i] = sh
        swept_low[i] = sl
        swept_h_lvl[i] = lh
        swept_l_lvl[i] = ll

    # ---- Entry signals ----
    long_sig = np.zeros(n, dtype=bool)
    short_sig = np.zeros(n, dtype=bool)
    long_brk_top_arr = np.full(n, np.nan)
    long_brk_bot_arr = np.full(n, np.nan)
    short_brk_top_arr = np.full(n, np.nan)
    short_brk_bot_arr = np.full(n, np.nan)
    long_sweep_lo_arr = np.full(n, np.nan)
    short_sweep_hi_arr = np.full(n, np.nan)

    if params.signal_mode == "sweep":
        # Direct sweep -> signal. Gate by can_enter (session + earliest entry
        # + not in flat window). Optionally apply HTF bias as a directional
        # safety net (default off).
        can_enter_arr = can_enter.values
        long_mask = swept_low & can_enter_arr
        short_mask = swept_high & can_enter_arr
        if params.use_bias:
            long_mask = long_mask & bias_up
            short_mask = short_mask & bias_dn
        long_sig[:] = long_mask
        short_sig[:] = short_mask
        df["long_sig"] = long_sig
        df["short_sig"] = short_sig
        df["long_brk_top"] = long_brk_top_arr
        df["long_brk_bot"] = long_brk_bot_arr
        df["short_brk_top"] = short_brk_top_arr
        df["short_brk_bot"] = short_brk_bot_arr
        df["swept_high"] = swept_high
        df["swept_low"] = swept_low
        df["last_swing_h"] = last_swing_h
        df["last_swing_l"] = last_swing_l
        df["or_locked"] = or_locked
        df["or_h"] = or_h
        df["or_l"] = or_l
        return df

    # ---- Breaker arming + retest entry signals (Pine-faithful mode) ----
    long_brk_bar = -1
    long_armed = False
    short_brk_bar = -1
    short_armed = False

    for i in range(n):
        if i > 0:
            was_down = close[i - 1] < open_[i - 1]
            was_up = close[i - 1] > open_[i - 1]
            prev_body_top = max(open_[i - 1], close[i - 1])
            prev_body_bot = min(open_[i - 1], close[i - 1])
        else:
            was_down = was_up = False
            prev_body_top = prev_body_bot = np.nan

        # Arm long?
        if (in_session[i] and swept_low[i] and disp_up[i]
                and (not params.use_bias or bias_up[i])):
            if was_down:
                long_brk_top_v = prev_body_top
                long_brk_bot_v = prev_body_bot
            else:
                long_brk_top_v = low[i]
                long_brk_bot_v = low[i]
            long_sweep_lo_v = low[i] if np.isnan(swept_l_lvl[i]) else min(low[i], swept_l_lvl[i])
            long_brk_top_arr[i] = long_brk_top_v
            long_brk_bot_arr[i] = long_brk_bot_v
            long_sweep_lo_arr[i] = long_sweep_lo_v
            long_brk_bar = i
            long_armed = True
        if long_armed and (i - long_brk_bar > params.retest_bars or not in_session[i]):
            long_armed = False

        if (in_session[i] and swept_high[i] and disp_dn[i]
                and (not params.use_bias or bias_dn[i])):
            if was_up:
                short_brk_top_v = prev_body_top
                short_brk_bot_v = prev_body_bot
            else:
                short_brk_top_v = high[i]
                short_brk_bot_v = high[i]
            short_sweep_hi_v = high[i] if np.isnan(swept_h_lvl[i]) else max(high[i], swept_h_lvl[i])
            short_brk_top_arr[i] = short_brk_top_v
            short_brk_bot_arr[i] = short_brk_bot_v
            short_sweep_hi_arr[i] = short_sweep_hi_v
            short_brk_bar = i
            short_armed = True
        if short_armed and (i - short_brk_bar > params.retest_bars or not in_session[i]):
            short_armed = False

        # Entry triggers (on bars STRICTLY AFTER the arm bar).
        if long_armed and can_enter[i] and i > long_brk_bar:
            top_v = long_brk_top_arr[long_brk_bar]
            bot_v = long_brk_bot_arr[long_brk_bar]
            if not np.isnan(top_v) and not np.isnan(bot_v):
                if low[i] <= top_v and close[i] > bot_v:
                    long_sig[i] = True
                    long_armed = False  # mirror Pine: disarm on entry
        if short_armed and can_enter[i] and i > short_brk_bar:
            top_v = short_brk_top_arr[short_brk_bar]
            bot_v = short_brk_bot_arr[short_brk_bar]
            if not np.isnan(top_v) and not np.isnan(bot_v):
                if high[i] >= bot_v and close[i] < top_v:
                    short_sig[i] = True
                    short_armed = False

    df["long_sig"] = long_sig
    df["short_sig"] = short_sig
    df["long_brk_top"] = long_brk_top_arr
    df["long_brk_bot"] = long_brk_bot_arr
    df["short_brk_top"] = short_brk_top_arr
    df["short_brk_bot"] = short_brk_bot_arr
    df["swept_high"] = swept_high
    df["swept_low"] = swept_low
    df["last_swing_h"] = last_swing_h
    df["last_swing_l"] = last_swing_l
    df["or_locked"] = or_locked
    df["or_h"] = or_h
    df["or_l"] = or_l
    return df


# --------------------------------------------------------------------------- #
#  Per-day strategy driver                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class DayContext:
    date: _date
    force_exit_time: str = DEFAULT_FORCE_EXIT_TIME
    entry_earliest_time: str = DEFAULT_ENTRY_EARLIEST_TIME
    flat_window_start: str = DEFAULT_FLAT_WINDOW_START
    lots: int = 1
    sell_offset_abs: int = DEFAULT_SELL_OFFSET_ABS
    buy_offset_abs: int = DEFAULT_BUY_OFFSET_ABS
    tp_inr: float = 1200.0
    sl_inr: float = 2000.0
    lot_size: int = LOT_SIZE_NIFTY


def _build_trade(
    ctx: DayContext, trade_idx: int, signal_time: str, signal_direction: str,
    side: str, atm_strike: float, spot_at_entry: float, spot_at_exit: float,
    entry_time: str, exit_signal_time: str, exit_time: str, exit_reason: str,
    sell_off: int, buy_off: int, sell_strike: float, buy_strike: float,
    sell_entry: float, buy_entry: float, sell_exit: float, buy_exit: float,
) -> ZaynSmcTrade:
    contracts = ctx.lot_size * ctx.lots
    net_credit_pts = sell_entry - buy_entry
    pnl_pts = (sell_entry + buy_exit) - (sell_exit + buy_entry)
    pnl_inr = pnl_pts * contracts
    sell_key = f"{side.lower()}_short"
    buy_key = f"{side.lower()}_long"
    return ZaynSmcTrade(
        date=ctx.date.isoformat(), trade_idx=trade_idx,
        signal_time=signal_time, signal_direction=signal_direction, side=side,
        atm_strike=atm_strike, spot_at_entry=spot_at_entry,
        spot_at_exit=spot_at_exit,
        entry_time=entry_time, exit_signal_time=exit_signal_time,
        exit_time=exit_time, exit_reason=exit_reason,
        net_credit_pts=net_credit_pts, net_credit_inr=net_credit_pts * contracts,
        pnl_pts=pnl_pts, pnl_inr=pnl_inr,
        return_pct=0.0, running_equity_inr=0.0,
        skip_reason=None,
        legs={
            sell_key: LegFill(option_type=side, side="SELL", lots=ctx.lots,
                              strike_offset=sell_off, strike=sell_strike,
                              entry_price=sell_entry, exit_price=sell_exit),
            buy_key: LegFill(option_type=side, side="BUY", lots=ctx.lots,
                             strike_offset=buy_off, strike=buy_strike,
                             entry_price=buy_entry, exit_price=buy_exit),
        },
    )


def _resolve_spread(slice_signal: pd.DataFrame, side: str,
                    sell_abs: int, buy_abs: int) -> Optional[Tuple[float, float, int, int]]:
    """At the signal bar's option slice, resolve (sell_strike, buy_strike, sell_off, buy_off)."""
    sell_off = sell_abs if side == "CE" else -sell_abs
    buy_off = buy_abs if side == "CE" else -buy_abs
    sell_row = lookup_by_offset(slice_signal, side, sell_off)
    buy_row = lookup_by_offset(slice_signal, side, buy_off)
    if sell_row is None or buy_row is None:
        return None
    return float(sell_row["strike"]), float(buy_row["strike"]), sell_off, buy_off


def _slice_at(opt_df: pd.DataFrame, time_str: str) -> pd.DataFrame:
    return opt_df[opt_df["_time"] == time_str]


def run_one_day(
    day_5m: pd.DataFrame,
    day_options: pd.DataFrame,
    ctx: DayContext,
) -> List[ZaynSmcTrade]:
    """Walk the 5-min indicator output and 1-min option frame; produce trades.

    `day_5m` must be pre-augmented with the indicator state columns and
    restricted to one trading day. `day_options` is the option-data slice
    for the same day with a `_time` column (HH:MM:SS)."""
    trades: List[ZaynSmcTrade] = []
    if day_5m.empty or day_options.empty:
        return trades

    force_exit_time = _norm_time(ctx.force_exit_time)
    trade_idx = 0
    open_trade: Optional[Dict] = None  # active position state

    # Pre-build list of 5-min signal bars in chronological order.
    bars = day_5m.sort_values("bar_close_ts").reset_index(drop=True)

    # Pre-build a sorted list of all 1-min times that exist in the options
    # slice, so we can iterate close-by-close for SL/TP between two 5-min
    # bars.
    option_times = sorted(day_options["_time"].unique().tolist())

    def _open_position(side: str, sig_bar_time: str, sig_dir: str):
        """Resolve strikes at sig_bar_time on options, fill at sig_bar+1m open.
        Returns position dict or None if data is missing.
        """
        slice_signal = _slice_at(day_options, sig_bar_time)
        if slice_signal.empty:
            return None
        atm = atm_strike_from(slice_signal)
        if atm is None:
            return None
        atm_strike, spot_at_entry = atm
        spread = _resolve_spread(slice_signal, side,
                                 ctx.sell_offset_abs, ctx.buy_offset_abs)
        if spread is None:
            return None
        sell_strike, buy_strike, sell_off, buy_off = spread

        fill_time = _plus_one_minute(sig_bar_time)
        slice_fill = _slice_at(day_options, fill_time)
        if slice_fill.empty:
            return None
        sell_row_fill = lookup_by_strike(slice_fill, side, sell_strike)
        buy_row_fill = lookup_by_strike(slice_fill, side, buy_strike)
        if sell_row_fill is None or buy_row_fill is None:
            return None
        return {
            "side": side,
            "signal_direction": sig_dir,
            "signal_time": sig_bar_time,
            "entry_time": fill_time,
            "atm_strike": atm_strike,
            "spot_at_entry": spot_at_entry,
            "sell_off": sell_off, "buy_off": buy_off,
            "sell_strike": sell_strike, "buy_strike": buy_strike,
            "sell_entry": float(sell_row_fill["open"]),
            "buy_entry": float(buy_row_fill["open"]),
        }

    def _close_position(position: Dict, exit_signal_time: str, exit_fill_time: str,
                        sell_exit: float, buy_exit: float, spot_exit: float,
                        reason: str) -> ZaynSmcTrade:
        nonlocal trade_idx
        trade_idx += 1
        return _build_trade(
            ctx, trade_idx,
            signal_time=position["signal_time"][:5],
            signal_direction=position["signal_direction"],
            side=position["side"],
            atm_strike=position["atm_strike"],
            spot_at_entry=position["spot_at_entry"],
            spot_at_exit=spot_exit,
            entry_time=position["entry_time"][:5],
            exit_signal_time=exit_signal_time[:5],
            exit_time=exit_fill_time[:5],
            exit_reason=reason,
            sell_off=position["sell_off"], buy_off=position["buy_off"],
            sell_strike=position["sell_strike"], buy_strike=position["buy_strike"],
            sell_entry=position["sell_entry"], buy_entry=position["buy_entry"],
            sell_exit=sell_exit, buy_exit=buy_exit,
        )

    tp_threshold = ctx.tp_inr * ctx.lots
    sl_threshold = ctx.sl_inr * ctx.lots
    tp_active = tp_threshold > 0
    sl_active = sl_threshold > 0

    def _scan_intraday_sl_tp(position: Dict, start_after: str, end_before: str):
        """Scan 1-min closes in (start_after, end_before) for SL/TP. Returns
        (exit_record_dict, None) on hit, else None. `start_after` and `end_before`
        are HH:MM:SS strings. The fill ALWAYS goes to T+1 OPEN; if T+1 doesn't
        exist in the slice, skip the hit and keep scanning.
        """
        if not (tp_active or sl_active):
            return None
        side = position["side"]
        sell_strike = position["sell_strike"]
        buy_strike = position["buy_strike"]
        sell_entry = position["sell_entry"]
        buy_entry = position["buy_entry"]
        for t in option_times:
            if t <= start_after or t >= end_before:
                continue
            slice_t = _slice_at(day_options, t)
            sr = lookup_by_strike(slice_t, side, sell_strike)
            br = lookup_by_strike(slice_t, side, buy_strike)
            if sr is None or br is None:
                continue
            s_t = float(sr["close"])
            b_t = float(br["close"])
            spot_t = float(sr["spot"])
            live_pts = (sell_entry + b_t) - (s_t + buy_entry)
            live_inr = live_pts * ctx.lot_size * ctx.lots
            tp_hit = tp_active and live_inr >= tp_threshold
            sl_hit = sl_active and live_inr <= -sl_threshold
            if not (tp_hit or sl_hit):
                continue
            reason = "SL" if sl_hit else "TP"
            fill_t = _plus_one_minute(t)
            slice_exit = _slice_at(day_options, fill_t)
            sr_exit = lookup_by_strike(slice_exit, side, sell_strike)
            br_exit = lookup_by_strike(slice_exit, side, buy_strike)
            if sr_exit is None or br_exit is None:
                continue
            return {
                "exit_signal_time": t,
                "exit_fill_time": fill_t,
                "sell_exit": float(sr_exit["open"]),
                "buy_exit": float(br_exit["open"]),
                "spot_exit": spot_t,
                "reason": reason,
            }
        return None

    def _force_exit(position: Dict):
        slice_forced = _slice_at(day_options, force_exit_time)
        if slice_forced.empty:
            return None
        side = position["side"]
        sr = lookup_by_strike(slice_forced, side, position["sell_strike"])
        br = lookup_by_strike(slice_forced, side, position["buy_strike"])
        if sr is None or br is None:
            return None
        return {
            "exit_signal_time": force_exit_time,
            "exit_fill_time": force_exit_time,
            "sell_exit": float(sr["close"]),
            "buy_exit": float(br["close"]),
            "spot_exit": float(sr["spot"]),
            "reason": "TIME",
        }

    # Walk 5-min bars in chronological order. Between consecutive bars,
    # scan 1-min option closes for SL/TP if there's an open position.
    prev_bar_close = ctx.entry_earliest_time
    for _, bar in bars.iterrows():
        bar_time = bar["time"]
        if bar_time > force_exit_time:
            break

        # ---- 1. Scan 1-min SL/TP between prev_bar_close and bar_time ----
        if open_trade is not None:
            hit = _scan_intraday_sl_tp(open_trade, prev_bar_close, bar_time)
            if hit is not None:
                trades.append(_close_position(
                    open_trade,
                    hit["exit_signal_time"], hit["exit_fill_time"],
                    hit["sell_exit"], hit["buy_exit"], hit["spot_exit"],
                    hit["reason"],
                ))
                open_trade = None

        # ---- 2. Evaluate signal at this 5-min bar close ----
        is_long = bool(bar["long_sig"])
        is_short = bool(bar["short_sig"])
        can_enter = bool(bar["can_enter"])

        if open_trade is None:
            if can_enter and (is_long or is_short):
                sig_dir = "LONG" if is_long else "SHORT"
                side = "PE" if is_long else "CE"
                new_pos = _open_position(side, bar_time, sig_dir)
                if new_pos is not None:
                    open_trade = new_pos
        else:
            # Check for opposite signal -> auto-flip.
            cur_dir = open_trade["signal_direction"]
            opposite_long = (cur_dir == "SHORT" and is_long)
            opposite_short = (cur_dir == "LONG" and is_short)
            if opposite_long or opposite_short:
                # Exit at next 1-min OPEN.
                fill_time = _plus_one_minute(bar_time)
                slice_exit = _slice_at(day_options, fill_time)
                side = open_trade["side"]
                sr_exit = lookup_by_strike(slice_exit, side, open_trade["sell_strike"])
                br_exit = lookup_by_strike(slice_exit, side, open_trade["buy_strike"])
                spot_exit = float(sr_exit["spot"]) if sr_exit is not None else float("nan")
                if sr_exit is not None and br_exit is not None:
                    trades.append(_close_position(
                        open_trade,
                        bar_time, fill_time,
                        float(sr_exit["open"]), float(br_exit["open"]),
                        spot_exit, "OPP",
                    ))
                    open_trade = None
                    if can_enter:
                        new_side = "PE" if opposite_long else "CE"
                        new_dir = "LONG" if opposite_long else "SHORT"
                        new_pos = _open_position(new_side, bar_time, new_dir)
                        if new_pos is not None:
                            open_trade = new_pos

        prev_bar_close = bar_time

    # ---- 3. If still in position, scan SL/TP from last bar to force-exit, then TIME-exit ----
    if open_trade is not None:
        hit = _scan_intraday_sl_tp(open_trade, prev_bar_close, force_exit_time)
        if hit is not None:
            trades.append(_close_position(
                open_trade,
                hit["exit_signal_time"], hit["exit_fill_time"],
                hit["sell_exit"], hit["buy_exit"], hit["spot_exit"],
                hit["reason"],
            ))
            open_trade = None
        else:
            forced = _force_exit(open_trade)
            if forced is not None:
                trades.append(_close_position(
                    open_trade,
                    forced["exit_signal_time"], forced["exit_fill_time"],
                    forced["sell_exit"], forced["buy_exit"], forced["spot_exit"],
                    forced["reason"],
                ))
            open_trade = None

    return trades


# --------------------------------------------------------------------------- #
#  Option data loader                                                         #
# --------------------------------------------------------------------------- #

def load_filtered_options(
    options_path: str, start_date: str, end_date: str,
    expiry_type: str = "WEEK", expiry_code: int = 1,
    earliest_time: str = DEFAULT_ENTRY_EARLIEST_TIME,
    force_exit_time: str = DEFAULT_FORCE_EXIT_TIME,
) -> pd.DataFrame:
    filters = [
        ("underlying", "=", "NIFTY"),
        ("expiry_type", "=", expiry_type),
        ("expiry_code", "=", int(expiry_code)),
    ]
    logger.info("Loading parquet with predicate pushdown...")
    table = pq.read_table(options_path, columns=REQUIRED_OPT_COLS, filters=filters)
    df = table.to_pandas()
    if df.empty:
        return df
    df = df[(df["datetime"].str.slice(0, 10) >= start_date)
            & (df["datetime"].str.slice(0, 10) <= end_date)]
    lower = _norm_time(earliest_time)
    upper = _norm_time(force_exit_time)
    time_col = df["datetime"].str.slice(11, 19)
    keep = (time_col >= lower) & (time_col <= upper)
    df = df[keep].copy()
    df["_time"] = time_col[keep].values
    df["_date"] = df["datetime"].str.slice(0, 10).values
    logger.info(f"Loaded {len(df):,} option rows for {df['_date'].nunique()} trading days.")
    return df


# --------------------------------------------------------------------------- #
#  Backtest orchestrator                                                      #
# --------------------------------------------------------------------------- #

def parse_config(config: dict) -> dict:
    entry = config.get("entry", {}) or {}
    exit_cfg = config.get("exit", {}) or {}
    structure = config.get("structure", {}) or {}
    indicator = config.get("indicator", {}) or {}
    return {
        "entry_earliest_time": _norm_time(entry.get("entry_earliest_time", DEFAULT_ENTRY_EARLIEST_TIME)),
        "force_exit_time": _norm_time(exit_cfg.get("force_exit_time", DEFAULT_FORCE_EXIT_TIME)),
        "flat_window_start": _norm_time(exit_cfg.get("flat_window_start", DEFAULT_FLAT_WINDOW_START)),
        "tp_inr": float(exit_cfg.get("tp_inr", 1200.0)),
        "sl_inr": float(exit_cfg.get("sl_inr", 2000.0)),
        "lots": int(structure.get("lots", 4)),
        "sell_offset_abs": int(structure.get("sell_offset_abs", DEFAULT_SELL_OFFSET_ABS)),
        "buy_offset_abs": int(structure.get("buy_offset_abs", DEFAULT_BUY_OFFSET_ABS)),
        "reference_capital": float(config["sizing"]["reference_capital"]),
        "indicator": IndicatorParams(
            signal_mode=str(indicator.get("signal_mode", DEFAULT_SIGNAL_MODE)),
            bias_tf_min=int(indicator.get("bias_tf_min", DEFAULT_BIAS_TF_MIN)),
            bias_len=int(indicator.get("bias_len", DEFAULT_BIAS_LEN)),
            use_bias=bool(indicator.get("use_bias", DEFAULT_USE_BIAS)),
            or_minutes=int(indicator.get("or_minutes", DEFAULT_OR_MINUTES)),
            use_pdhl=bool(indicator.get("use_pdhl", DEFAULT_USE_PDHL)),
            use_orhl=bool(indicator.get("use_orhl", DEFAULT_USE_ORHL)),
            swing_lb=int(indicator.get("swing_lb", DEFAULT_SWING_LB)),
            atr_len=int(indicator.get("atr_len", DEFAULT_ATR_LEN)),
            disp_mult=float(indicator.get("disp_mult", DEFAULT_DISP_MULT)),
            body_pct=float(indicator.get("body_pct", DEFAULT_BODY_PCT)),
            sweep_buf=float(indicator.get("sweep_buf", DEFAULT_SWEEP_BUF)),
            retest_bars=int(indicator.get("retest_bars", DEFAULT_RETEST_BARS)),
            entry_earliest_time=_norm_time(entry.get("entry_earliest_time", DEFAULT_ENTRY_EARLIEST_TIME)),
            flat_window_start=_norm_time(exit_cfg.get("flat_window_start", DEFAULT_FLAT_WINDOW_START)),
        ),
    }


def run_backtest(
    df_5m: pd.DataFrame,
    options_df: pd.DataFrame,
    config: dict,
) -> dict:
    """`df_5m` must already have the indicator state columns. `options_df`
    has a `_time` column (HH:MM:SS) and a `_date` column."""
    p = parse_config(config)
    bt_start = config["backtest_start"]
    bt_end = config["backtest_end"]
    capital = p["reference_capital"]

    all_trades: List[ZaynSmcTrade] = []
    running_equity = capital

    if df_5m.empty or options_df.empty:
        return {"trades": all_trades, "config": config}

    df_5m = df_5m[(df_5m["date"] >= bt_start) & (df_5m["date"] <= bt_end)]
    options_df = options_df[(options_df["_date"] >= bt_start)
                            & (options_df["_date"] <= bt_end)]

    for date_str, day_5m in df_5m.groupby("date", sort=True):
        try:
            d = _date.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue
        day_options = options_df[options_df["_date"] == date_str]
        if day_options.empty:
            continue
        ctx = DayContext(
            date=d,
            force_exit_time=p["force_exit_time"],
            entry_earliest_time=p["entry_earliest_time"],
            flat_window_start=p["flat_window_start"],
            lots=p["lots"],
            sell_offset_abs=p["sell_offset_abs"],
            buy_offset_abs=p["buy_offset_abs"],
            tp_inr=p["tp_inr"],
            sl_inr=p["sl_inr"],
        )
        day_trades = run_one_day(day_5m, day_options, ctx)
        for t in day_trades:
            running_equity += t.pnl_inr
            t.return_pct = t.pnl_inr / capital if capital else 0.0
            t.running_equity_inr = running_equity
            all_trades.append(t)

    return {"trades": all_trades, "config": config}


# --------------------------------------------------------------------------- #
#  Reporting                                                                  #
# --------------------------------------------------------------------------- #

def _count_by(items, keyfn):
    counts: Dict[str, int] = {}
    for it in items:
        k = keyfn(it)
        counts[k] = counts.get(k, 0) + 1
    return counts


def summarize_metrics(trades: List[ZaynSmcTrade], starting_capital: float) -> dict:
    placed = [t for t in trades if t.skip_reason is None]
    pnls = [t.pnl_inr for t in placed]
    wins = [t for t in placed if t.pnl_inr > 0]
    losses = [t for t in placed if t.pnl_inr < 0]

    # Equity curve at trade-level (one row per trade).
    equity_curve = pd.DataFrame()
    if trades:
        rows = []
        peak = starting_capital
        for t in trades:
            equity = t.running_equity_inr if t.running_equity_inr == t.running_equity_inr else starting_capital
            peak = max(peak, equity)
            dd_inr = peak - equity
            dd_pct = dd_inr / peak if peak else 0.0
            rows.append({
                "date": t.date, "trade_idx": t.trade_idx,
                "equity_inr": equity, "drawdown_inr": dd_inr,
                "drawdown_pct": dd_pct,
            })
        equity_curve = pd.DataFrame(rows)

    if not equity_curve.empty:
        max_dd_inr = float(equity_curve["drawdown_inr"].max())
        max_dd_pct = float(equity_curve["drawdown_pct"].max())
    else:
        max_dd_inr = 0.0
        max_dd_pct = 0.0

    days_with_trades = len({t.date for t in placed})

    return {
        "total_trades": len(trades),
        "trades_placed": len(placed),
        "trades_skipped": len(trades) - len(placed),
        "days_with_trades": days_with_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(placed) if placed else 0.0,
        "loss_rate": len(losses) / len(placed) if placed else 0.0,
        "mean_pnl_inr": statistics.fmean(pnls) if pnls else 0.0,
        "median_pnl_inr": statistics.median(pnls) if pnls else 0.0,
        "total_pnl_inr": sum(pnls),
        "total_return_pct": sum(pnls) / starting_capital if starting_capital else 0.0,
        "max_drawdown_inr": max_dd_inr,
        "max_drawdown_pct": max_dd_pct,
        "max_consecutive_losses": max_consecutive_losses(pnls),
        "best_trade_inr": max(pnls) if pnls else 0.0,
        "worst_trade_inr": min(pnls) if pnls else 0.0,
        "ce_trades": sum(1 for t in placed if t.side == "CE"),
        "pe_trades": sum(1 for t in placed if t.side == "PE"),
        "exit_reason_counts": _count_by(placed, lambda t: t.exit_reason),
    }


def trades_to_dataframe(trades: List[ZaynSmcTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    rows = []
    for t in trades:
        row = {k: v for k, v in asdict(t).items() if k != "legs"}
        for leg_key, leg in t.legs.items():
            row[f"{leg_key}_strike"] = leg.strike
            row[f"{leg_key}_offset"] = leg.strike_offset
            row[f"{leg_key}_entry"] = leg.entry_price
            row[f"{leg_key}_exit"] = leg.exit_price
        rows.append(row)
    return pd.DataFrame(rows)


def write_trades_csv(trades: List[ZaynSmcTrade], path) -> None:
    trades_to_dataframe(trades).to_csv(path, index=False)


def write_equity_csv(trades: List[ZaynSmcTrade], capital: float, path) -> None:
    if not trades:
        pd.DataFrame(columns=["date", "trade_idx", "equity_inr",
                              "drawdown_inr", "drawdown_pct"]).to_csv(path, index=False)
        return
    rows = []
    peak = capital
    for t in trades:
        equity = t.running_equity_inr if t.running_equity_inr == t.running_equity_inr else capital
        peak = max(peak, equity)
        dd_inr = peak - equity
        dd_pct = dd_inr / peak if peak else 0.0
        rows.append({
            "date": t.date, "trade_idx": t.trade_idx,
            "equity_inr": equity, "drawdown_inr": dd_inr, "drawdown_pct": dd_pct,
        })
    pd.DataFrame(rows).to_csv(path, index=False)


def print_summary(s: dict) -> None:
    lines = [
        f"Total trades: {s['total_trades']}  (placed={s['trades_placed']}, skipped={s['trades_skipped']})",
        f"Days with trades: {s['days_with_trades']}",
        f"Side breakdown: CE={s['ce_trades']}, PE={s['pe_trades']}",
        f"Wins / Losses: {s['wins']} / {s['losses']}  ({s['win_rate']*100:.2f}% win-rate)",
        f"Mean P&L: Rs {s['mean_pnl_inr']:.2f}    Median: Rs {s['median_pnl_inr']:.2f}",
        f"Total P&L: Rs {s['total_pnl_inr']:.2f}",
        f"Total return on capital: {s['total_return_pct']*100:.2f}%",
        f"Max drawdown: Rs {s['max_drawdown_inr']:.2f}  ({s['max_drawdown_pct']*100:.2f}%)",
        f"Max consecutive losing trades: {s['max_consecutive_losses']}",
        f"Best trade: Rs {s['best_trade_inr']:.2f}    Worst: Rs {s['worst_trade_inr']:.2f}",
        f"Exit reasons: {s['exit_reason_counts']}",
    ]
    for line in lines:
        print(line)


def run(config: dict, options_path: str, spot_path: str, output_dir: str) -> dict:
    """Top-level entrypoint. Loads spot + options, computes indicator, runs backtest."""
    p = parse_config(config)
    bt_start = config["backtest_start"]
    bt_end = config["backtest_end"]
    expiry_cfg = config.get("expiry", {}) or {}

    spot_1m = load_spot_1m(spot_path, bt_start, bt_end)
    df_5m = resample_intraday(spot_1m, minutes=5)
    df_htf = resample_intraday(spot_1m, minutes=p["indicator"].bias_tf_min)
    pdh_pdl = daily_prior_high_low(spot_1m)
    df_5m = compute_indicator_state(df_5m, df_htf, pdh_pdl, p["indicator"])

    options_df = load_filtered_options(
        options_path, bt_start, bt_end,
        expiry_type=str(expiry_cfg.get("expiry_type", "WEEK")).upper(),
        expiry_code=int(expiry_cfg.get("expiry_code", 1)),
        earliest_time=p["entry_earliest_time"],
        force_exit_time=p["force_exit_time"],
    )

    result = run_backtest(df_5m, options_df, config)
    trades = result["trades"]
    capital = p["reference_capital"]
    summary = summarize_metrics(trades, capital)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    trades_path = out / f"zayn_smc_trades_{bt_start}_{bt_end}.csv"
    equity_path = out / f"zayn_smc_equity_{bt_start}_{bt_end}.csv"
    write_trades_csv(trades, trades_path)
    write_equity_csv(trades, capital, equity_path)
    print_summary(summary)
    return {
        "trades": trades, "summary": summary,
        "trades_csv": str(trades_path),
        "equity_csv": str(equity_path),
    }
