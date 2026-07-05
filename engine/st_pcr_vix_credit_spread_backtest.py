"""
SuperTrend(15m) + PCR-pre-flip + VIX-direction -> CREDIT-spread engine on NIFTY.

This is the CREDIT-SPREAD variant of Sanket's "major strategy" (the original was a
debit spread).  Three changes vs the major strategy were requested:

  1. Exit set is TP, hard SL, and force-EOD ONLY -- the SuperTrend-reversal exit
     (criterion 3 of the major strategy) is removed.
  2. Debit spread -> CREDIT spread.  We now COLLECT a credit at entry:
       * BULL signal -> bull-put credit spread  : SELL ATM-100 PE, BUY ATM-200 PE
       * BEAR signal -> bear-call credit spread : SELL ATM+100 CE, BUY ATM+200 CE
     (sell_offset=2 strikes=100 pts OTM, buy_offset=4=200 pts -> 100-pt width.)
  3. Grid-search exits: TP = 20% of the entry CREDIT (profit captured), SL = 25 pts
     (absolute spread-points loss).  Everything else mirrors the major strategy.

SIGNAL (15-minute, on continuous spot OHLC; indicators are NOT reset per day):
  Signal candle  S = a 15-min spot bar (anchored to the 09:15 session open).
  Previous candle P = the immediately-preceding 15-min bar (S - 15min).
  SuperTrend     : ATR period 7, multiplier 3, on the 15-min spot close/high/low.
                   (codebase convention: direction == -1 -> bullish / uptrend,
                    +1 -> bearish / downtrend.)

  The SuperTrend flip is the TRIGGER; PCR and VIX are optional FILTERS, each
  independently toggled by use_pcr_filter / use_vix_filter.

  BULL signal at candle S requires:
    * SuperTrend FLIPPED to bullish:  dir[P] == +1 (bearish) and dir[S] == -1.
    * PCR pre-flip (if use_pcr_filter):  pcr_ref > pcr_bull_min  (default 1.1).
    * VIX flat/falling (if use_vix_filter): dvix <= dvix_bull_max (default +0.3).

  BEAR signal at candle S requires:
    * SuperTrend FLIPPED to bearish:  dir[P] == -1 (bullish) and dir[S] == +1.
    * PCR pre-flip (if use_pcr_filter):  pcr_ref < pcr_bear_max  (default 0.9).
    * VIX flat/rising (if use_vix_filter): dvix >= dvix_bear_min  (default -0.3).

  pcr_ref = the FULL-chain put/call OI ratio at the LAST 1-min bar of the
            PREVIOUS candle P (e.g. for the 11:15 signal candle, the 11:00
            candle's last value = the 11:14 PCR).  PCR = sum(PE oi)/sum(CE oi)
            over the traded expiry's whole chain.
  dvix    = VIX(close of candle S) - VIX(close of candle P)  (one full 15-min
            move; e.g. VIX@11:29 - VIX@11:14).  Both the PCR and VIX filters are
            independently config-gated (use_pcr_filter / use_vix_filter); turn
            either off to trade on the flip alone or flip + the other filter.

ENTRY (no look-ahead):
  Signal is CONFIRMED at the close of candle S (clock = S + 15min) and the order
  is placed on the FIRST 1-min bar of the NEXT 15-min candle (= the bar labeled
  S + 15min) at its OPEN -- exactly what you'd do live ("see 11:15 close at
  11:29, place at 11:30 open").
    * entry bar must fall in [window_start, window_end] (default 09:46..14:15).
    * ATM = floor(entry_bar_open_spot / step + 0.5) * step  (round-half-up to the
      nearest 50), from the 11:30-open spot -- NOT the 11:29 close.
    * Entry premiums = both legs' OPEN at the entry bar.
    * net_credit = sell_open - buy_open  (must satisfy the credit band).
    * Skip (logged/counted) on missing spot/legs or an out-of-band credit.

POSITION MANAGEMENT:
  One spread open at a time; unlimited sequential re-entries per day
  (max_trades_per_day = 0 -> unlimited).  A signal that fires while in a trade
  is ignored (not queued).

EXITS (priority SL > TP > EOD; checked on every 1-min option CLOSE after entry):
  spread_value = sell_close - buy_close          (cost to buy the spread back)
  pnl_pts      = net_credit - spread_value        (credit kept minus buy-back)
    1. SL  : pnl_pts <= -sl_pts                    (lose sl_pts; default 25)
    2. TP  : pnl_pts >=  tp_credit_frac*net_credit (capture 20% of the credit)
    3. EOD : force square-off at square_off_time (default 15:15) close.
  TP/SL fill at the SAME 1-min bar's CLOSE (Sanket's "exit at prevailing 1-min
  close" rule -- no next-bar shift).  Missing leg data on a monitoring bar is
  skipped (not an exit); >max_missing_bars consecutive missing -> force close at
  the last valid close.

EXPIRY ROLL: trade weekly code 1 normally; on a weekly-expiry day trade code 2
  (next weekly), which keeps time value.

P&L (per spread, no brokerage/costs applied):
  pnl_pts = (sell_entry + buy_exit) - (sell_exit + buy_entry)
  pnl_inr = pnl_pts * lot_size * lots
"""

import logging
import math
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
SIGNAL_TF_MIN = 15

DEFAULT_ST_FACTOR = 3.0
DEFAULT_ST_ATR_PERIOD = 7
DEFAULT_PCR_BULL_MIN = 1.1
DEFAULT_PCR_BEAR_MAX = 0.9
DEFAULT_USE_PCR_FILTER = True
DEFAULT_USE_VIX_FILTER = True
DEFAULT_DVIX_BULL_MAX = 0.3
DEFAULT_DVIX_BEAR_MIN = -0.3
DEFAULT_WARMUP_DAYS = 10

DEFAULT_WINDOW_START = "09:46:00"
DEFAULT_WINDOW_END = "14:15:00"
DEFAULT_SQUARE_OFF_TIME = "15:15:00"

DEFAULT_SELL_OFFSET = 2     # strikes OTM  (x strike_step pts) -> 100-pt OTM
DEFAULT_BUY_OFFSET = 4      # -> 200-pt OTM ; 100-pt-wide spread
# TP = capture tp_credit_frac of the entry credit: take profit once realized
# profit >= frac * credit (equivalently, the spread falls to (1-frac) * credit).
DEFAULT_TP_CREDIT_FRAC = 0.20
DEFAULT_SL_PTS = 25.0           # absolute spread-points stop
DEFAULT_MIN_CREDIT_PTS = 0.0    # exclusive lower bound (must be a real credit)
DEFAULT_MAX_CREDIT_PTS = 0.0    # 0 -> no upper bound
DEFAULT_MAX_TRADES_PER_DAY = 0      # 0 -> unlimited
DEFAULT_MAX_MISSING_BARS = 30

OPT_COLS = [
    "datetime", "underlying", "option_type", "expiry_type", "expiry_code",
    "strike_offset", "strike", "open", "close", "oi",
]

# Reasons a qualifying signal is skipped (never produces a trade).
SKIP_REASONS = ("spot_missing", "sell_leg_missing", "buy_leg_missing",
                "credit_out_of_band")


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
    strike_offset: int    # signed offset in STRIKES from ATM (e.g. -2, +4)
    strike: float
    entry_price: float
    exit_price: Optional[float] = None


@dataclass
class StPcrVixTrade:
    date: str
    signal: str                 # "LONG" | "SHORT"
    spread: str                 # "BULL_PUT" | "BEAR_CALL"
    direction: str              # "LONG" (sell PE) | "SHORT" (sell CE)
    expiry_code: int            # 1 (nearest) | 2 (rolled, expiry day)

    signal_candle: str          # HH:MM -- 15m signal candle start
    pcr_ref: float              # previous-candle PCR used by the filter
    dvix: float                 # VIX(close S) - VIX(close P)

    entry_time: str             # HH:MM -- entry bar (next 15m candle open)
    entry_spot: float           # spot OPEN at the entry bar
    atm_strike: float

    exit_time: str              # HH:MM
    exit_reason: str            # "TP" | "SL" | "EOD"
    exit_spot: float
    fill_fallback: bool         # an exit fill used a fallback minute / field

    net_credit_pts: float       # sell_entry - buy_entry (per contract)
    net_credit_inr: float
    tp_threshold_pts: float     # profit pts needed to hit TP
    sl_threshold_pts: float     # loss pts that triggers SL

    pnl_pts: float
    pnl_inr: float
    return_pct: float
    running_equity_inr: float

    legs: Dict[str, LegFill] = field(default_factory=dict)


@dataclass
class StPcrVixDayContext:
    date: str
    expiry_code: int = 1
    lots: int = 1
    sell_offset_abs: int = DEFAULT_SELL_OFFSET
    buy_offset_abs: int = DEFAULT_BUY_OFFSET
    tp_credit_frac: float = DEFAULT_TP_CREDIT_FRAC   # 0 disables TP
    sl_pts: float = DEFAULT_SL_PTS                   # 0 disables SL
    min_credit_pts: float = DEFAULT_MIN_CREDIT_PTS
    max_credit_pts: float = DEFAULT_MAX_CREDIT_PTS   # 0 -> no upper bound
    square_off_time: str = DEFAULT_SQUARE_OFF_TIME
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY
    max_missing_bars: int = DEFAULT_MAX_MISSING_BARS
    strike_step: int = 50
    lot_size: int = LOT_SIZE_NIFTY


# --------------------------------------------------------------------------- #
#  15-minute signal frame (continuous across days)                            #
# --------------------------------------------------------------------------- #

def _resample_15m(spot: pd.DataFrame) -> pd.DataFrame:
    """Resample the continuous 1-min spot to 15-min OHLC anchored at 09:15.

    Returns a frame indexed by the candle START timestamp with columns
    open/high/low/close and _date (the candle's calendar date)."""
    s = spot.set_index("dt")
    intraday = s.between_time("09:15", "15:29")
    htf = intraday.resample(f"{SIGNAL_TF_MIN}min", origin="start_day",
                            offset="9h15min").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).dropna(subset=["close"])
    htf["_date"] = htf.index.strftime("%Y-%m-%d")
    return htf


def _vix_15m_close(vix: pd.DataFrame) -> pd.Series:
    """15-min VIX CLOSE anchored at 09:15, indexed by candle start.

    `vix` carries dt (naive IST) + close.  Empty -> empty Series."""
    if vix is None or vix.empty:
        return pd.Series(dtype=float)
    v = vix.set_index("dt").sort_index()
    intraday = v.between_time("09:15", "15:29")
    out = intraday.resample(f"{SIGNAL_TF_MIN}min", origin="start_day",
                            offset="9h15min")["close"].last()
    return out.dropna()


def build_signal_frame(spot: pd.DataFrame, vix: pd.DataFrame,
                       params: dict) -> pd.DataFrame:
    """Per-15m-candle signal scaffold (continuous; no daily reset).

    Returns a frame indexed by candle-start datetime with:
      _date, open, high, low, close, dir, vix_close, prev_dir, dvix,
      is_contiguous
    where prev_dir is the immediately-preceding 15m candle's SuperTrend
    direction and is_contiguous flags that that candle is exactly S-15min (same
    session).  PCR (per-expiry, per-day) is joined later in the day loop.
    """
    cols = ["_date", "open", "high", "low", "close", "dir", "vix_close",
            "prev_dir", "dvix", "is_contiguous"]
    if spot.empty:
        return pd.DataFrame(columns=cols)

    htf = _resample_15m(spot)
    if htf.empty:
        return pd.DataFrame(columns=cols)

    st = get_indicator("SUPERTREND", name="st15",
                       factor=params["st_factor"],
                       atr_period=params["st_atr_period"]).calculate(
        htf["close"], high=htf["high"], low=htf["low"])
    htf["dir"] = st["direction"].values

    vix_close = _vix_15m_close(vix)
    htf["vix_close"] = vix_close.reindex(htf.index).values if not vix_close.empty \
        else np.nan

    htf["prev_dir"] = htf["dir"].shift(1)
    prev_vix = htf["vix_close"].shift(1)
    htf["dvix"] = htf["vix_close"] - prev_vix

    prev_start = pd.Series(htf.index, index=htf.index).shift(1)
    htf["is_contiguous"] = (
        (htf.index - prev_start) == pd.Timedelta(minutes=SIGNAL_TF_MIN)
    ).fillna(False).values
    return htf[cols]


# --------------------------------------------------------------------------- #
#  PCR (full-chain put/call OI ratio) per candle                              #
# --------------------------------------------------------------------------- #

def pcr_by_minute(day_options: pd.DataFrame) -> Dict[str, float]:
    """Full-chain PCR per minute for the day's chosen-expiry options.

    PCR(t) = sum(PE oi at t) / sum(CE oi at t) across ALL strikes. Minutes
    with zero CE OI (or no rows) are omitted.
    """
    if day_options.empty:
        return {}
    g = (day_options.groupby(["_time", "option_type"])["oi"].sum()
         .unstack(fill_value=0.0))
    if "CE" not in g.columns or "PE" not in g.columns:
        return {}
    out: Dict[str, float] = {}
    for t, row in g.iterrows():
        ce = float(row["CE"])
        if ce > 0:
            out[str(t)] = float(row["PE"]) / ce
    return out


def _candle_close_pcr(pcr_min: Dict[str, float], candle_start: pd.Timestamp
                      ) -> float:
    """PCR at the LAST 1-min bar within candle [start, start+14min].

    Picks the latest available minute in the window (Sanket's "the candle's
    last PCR value"). NaN if no minute in the window has PCR."""
    if not pcr_min:
        return float("nan")
    lo = candle_start.strftime("%H:%M:%S")
    hi = (candle_start + pd.Timedelta(minutes=SIGNAL_TF_MIN - 1)
          ).strftime("%H:%M:%S")
    best_t, best_v = None, float("nan")
    for t, v in pcr_min.items():
        if lo <= t <= hi and (best_t is None or t > best_t):
            best_t, best_v = t, v
    return best_v


def build_day_entry_signals(day_candles: pd.DataFrame,
                            pcr_min: Dict[str, float],
                            params: dict
                            ) -> Dict[str, Tuple[str, float, float, str]]:
    """Resolve this day's entry signals.

    Returns {entry_time_str: (signal, pcr_ref, dvix, signal_candle_hhmm)} where
    signal is "LONG"/"SHORT" and entry_time_str (HH:MM:SS) is the next-candle
    open minute (= S + 15min). Only candles whose entry bar lies in the entry
    window are emitted.
    """
    out: Dict[str, Tuple[str, float, float, str]] = {}
    ws, we = params["window_start"], params["window_end"]
    use_pcr = bool(params["use_pcr_filter"])
    use_vix = bool(params["use_vix_filter"])
    pcr_bull, pcr_bear = params["pcr_bull_min"], params["pcr_bear_max"]
    dvix_bull, dvix_bear = params["dvix_bull_max"], params["dvix_bear_min"]

    for s_start, r in day_candles.iterrows():
        if not bool(r["is_contiguous"]):
            continue
        dir_s, dir_p = r["dir"], r["prev_dir"]
        if isnan(dir_s) or isnan(dir_p):
            continue
        entry_ts = s_start + pd.Timedelta(minutes=SIGNAL_TF_MIN)
        entry_t = entry_ts.strftime("%H:%M:%S")
        if not (ws <= entry_t <= we):
            continue

        pcr_ref = _candle_close_pcr(pcr_min, s_start - pd.Timedelta(
            minutes=SIGNAL_TF_MIN))  # previous candle P = S - 15min
        dvix = r["dvix"]

        bull_flip = (dir_p == 1) and (dir_s == -1)
        bear_flip = (dir_p == -1) and (dir_s == 1)
        sig_hhmm = s_start.strftime("%H:%M")

        if bull_flip \
                and ((not use_pcr) or (not isnan(pcr_ref) and pcr_ref > pcr_bull)) \
                and ((not use_vix) or (not isnan(dvix) and dvix <= dvix_bull)):
            out[entry_t] = ("LONG", float(pcr_ref),
                            float(dvix) if not isnan(dvix) else float("nan"),
                            sig_hhmm)
        elif bear_flip \
                and ((not use_pcr) or (not isnan(pcr_ref) and pcr_ref < pcr_bear)) \
                and ((not use_vix) or (not isnan(dvix) and dvix >= dvix_bear)):
            out[entry_t] = ("SHORT", float(pcr_ref),
                            float(dvix) if not isnan(dvix) else float("nan"),
                            sig_hhmm)
    return out


# --------------------------------------------------------------------------- #
#  Per-day execution                                                          #
# --------------------------------------------------------------------------- #

def _leg_maps(day_options: pd.DataFrame, time_col: pd.Series, option_type: str,
              strike: float) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Minute -> open / close price maps for one contract (first row/minute)."""
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

    def __init__(self, trade: StPcrVixTrade, entry_minute: str,
                 option_type: str, sell_strike: float, buy_strike: float,
                 sell_entry: float, buy_entry: float):
        self.trade = trade
        self.entry_minute = entry_minute
        self.option_type = option_type
        self.sell_strike = sell_strike
        self.buy_strike = buy_strike
        self.sell_entry = sell_entry
        self.buy_entry = buy_entry
        self.sell_open: Dict[str, float] = {}
        self.sell_close: Dict[str, float] = {}
        self.buy_open: Dict[str, float] = {}
        self.buy_close: Dict[str, float] = {}
        self.last_valid_minute: Optional[str] = None  # last both-legs-present min


def _open_position(day_options: pd.DataFrame, time_col: pd.Series, t: str,
                   signal: str, pcr_ref: float, dvix: float, sig_hhmm: str,
                   ctx: StPcrVixDayContext, spot_open_map: Dict[str, float]
                   ) -> Tuple[Optional[_OpenPosition], Optional[str]]:
    """Resolve strikes from the ENTRY bar's OPEN spot and fill both legs at
    their OPEN at the same minute.  Returns (position, None) or (None, reason)."""
    spot = spot_open_map.get(t)
    if spot is None or isnan(spot):
        return None, "spot_missing"
    step = ctx.strike_step
    atm = math.floor(spot / step + 0.5) * step  # round-half-up to nearest step

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
    if t not in sell_open or isnan(sell_open[t]):
        return None, "sell_leg_missing"
    if t not in buy_open or isnan(buy_open[t]):
        return None, "buy_leg_missing"
    sell_entry = sell_open[t]
    buy_entry = buy_open[t]

    net_credit_pts = sell_entry - buy_entry
    if net_credit_pts <= ctx.min_credit_pts:
        return None, "credit_out_of_band"
    if ctx.max_credit_pts > 0 and net_credit_pts > ctx.max_credit_pts:
        return None, "credit_out_of_band"

    tp_thr = ctx.tp_credit_frac * net_credit_pts if ctx.tp_credit_frac > 0 else 0.0
    contracts = ctx.lot_size * ctx.lots
    trade = StPcrVixTrade(
        date=ctx.date, signal=signal, spread=spread, direction=direction,
        expiry_code=ctx.expiry_code,
        signal_candle=sig_hhmm, pcr_ref=pcr_ref, dvix=dvix,
        entry_time=t[:5], entry_spot=float(spot), atm_strike=float(atm),
        exit_time="", exit_reason="", exit_spot=float("nan"),
        fill_fallback=False,
        net_credit_pts=net_credit_pts, net_credit_inr=net_credit_pts * contracts,
        tp_threshold_pts=tp_thr, sl_threshold_pts=ctx.sl_pts,
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
    pos.last_valid_minute = t
    return pos, None


def _settle(pos: _OpenPosition, reason: str, exit_minute: Optional[str],
            sell_exit: float, buy_exit: float, spot_close_map: Dict[str, float],
            ctx: StPcrVixDayContext, fallback: bool) -> None:
    """Stamp exit prices + P&L onto the trade."""
    trade = pos.trade
    trade.legs["sell"].exit_price = sell_exit
    trade.legs["buy"].exit_price = buy_exit
    trade.exit_reason = reason
    trade.exit_time = exit_minute[:5] if exit_minute else ""
    trade.exit_spot = float(spot_close_map.get(exit_minute, float("nan"))) \
        if exit_minute else float("nan")
    trade.fill_fallback = fallback

    contracts = ctx.lot_size * ctx.lots
    trade.pnl_pts = (pos.sell_entry + buy_exit) - (sell_exit + pos.buy_entry)
    trade.pnl_inr = trade.pnl_pts * contracts


def run_one_day(day_options: pd.DataFrame,
                entry_sig: Dict[str, Tuple[str, float, float, str]],
                spot_open_map: Dict[str, float],
                spot_close_map: Dict[str, float],
                ctx: StPcrVixDayContext
                ) -> Tuple[List[StPcrVixTrade], Dict[str, int]]:
    """Run the strategy over one trading day.

    `entry_sig` maps an entry-bar minute (HH:MM:SS) -> (signal, pcr_ref, dvix,
    signal_candle). `spot_open_map`/`spot_close_map` are minute -> spot price.
    """
    trades: List[StPcrVixTrade] = []
    skips = _empty_skips()
    if day_options.empty:
        return trades, skips

    time_col = day_options["_time"] if "_time" in day_options.columns else \
        day_options["datetime"].str.slice(11, 19)
    square = _norm_time(ctx.square_off_time)
    minutes = sorted(m for m in time_col.unique() if m <= square)
    if not minutes:
        return trades, skips

    pos: Optional[_OpenPosition] = None
    trades_today = 0
    cap = ctx.max_trades_per_day
    tp_active = ctx.tp_credit_frac > 0
    sl_active = ctx.sl_pts > 0
    missing_streak = 0

    for i, t in enumerate(minutes):
        is_square = (t == square)

        # --- (A) Square-off: flatten any open spread at this CLOSE -----------
        if is_square:
            if pos is not None:
                sc = pos.sell_close.get(t)
                bc = pos.buy_close.get(t)
                if sc is not None and bc is not None and not isnan(sc) \
                        and not isnan(bc):
                    _settle(pos, "EOD", t, sc, bc, spot_close_map, ctx, False)
                else:  # no data at deadline -> last valid close
                    lm = pos.last_valid_minute
                    _settle(pos, "EOD", lm, pos.sell_close.get(lm, pos.sell_entry),
                            pos.buy_close.get(lm, pos.buy_entry),
                            spot_close_map, ctx, True)
                trades.append(pos.trade)
                pos = None
            break

        # --- (B) TP / SL detection on this 1-min option CLOSE ----------------
        if pos is not None and t > pos.entry_minute:
            sc = pos.sell_close.get(t)
            bc = pos.buy_close.get(t)
            if sc is None or bc is None or isnan(sc) or isnan(bc):
                missing_streak += 1
                if missing_streak > ctx.max_missing_bars:
                    lm = pos.last_valid_minute
                    _settle(pos, "EOD", lm,
                            pos.sell_close.get(lm, pos.sell_entry),
                            pos.buy_close.get(lm, pos.buy_entry),
                            spot_close_map, ctx, True)
                    trades.append(pos.trade)
                    pos = None
            else:
                missing_streak = 0
                pos.last_valid_minute = t
                spread_value = sc - bc
                pnl_pts = pos.sell_entry - pos.buy_entry - spread_value
                sl_hit = sl_active and pnl_pts <= -ctx.sl_pts
                tp_hit = tp_active and pnl_pts >= pos.trade.tp_threshold_pts
                if sl_hit or tp_hit:
                    reason = "SL" if sl_hit else "TP"   # SL has priority
                    _settle(pos, reason, t, sc, bc, spot_close_map, ctx, False)
                    trades.append(pos.trade)
                    pos = None

        # --- (C) Entry fills at THIS minute's OPEN (the entry bar) -----------
        if (t in entry_sig and pos is None and not is_square
                and (cap <= 0 or trades_today < cap)):
            sig, pcr_ref, dvix, sig_hhmm = entry_sig[t]
            new_pos, reason = _open_position(
                day_options, time_col, t, sig, pcr_ref, dvix, sig_hhmm, ctx,
                spot_open_map)
            if new_pos is None:
                skips[reason] += 1
                logger.warning(f"{ctx.date} {t}: {sig} entry skipped ({reason}).")
            else:
                pos = new_pos
                trades_today += 1
                missing_streak = 0

    if pos is not None:  # data ended before square-off
        lm = pos.last_valid_minute
        _settle(pos, "EOD", lm, pos.sell_close.get(lm, pos.sell_entry),
                pos.buy_close.get(lm, pos.buy_entry), spot_close_map, ctx, True)
        trades.append(pos.trade)

    return trades, skips


# --------------------------------------------------------------------------- #
#  Data loading                                                               #
# --------------------------------------------------------------------------- #

def load_filtered_options(options_path: str, start_date: str, end_date: str,
                          session_start: str = SESSION_START,
                          square_off_time: str = DEFAULT_SQUARE_OFF_TIME
                          ) -> pd.DataFrame:
    """Predicate-pushdown load of NIFTY weekly options (codes 1 and 2). Keeps
    the WHOLE strike chain (needed for full-chain PCR) within
    [session_start, square_off_time]."""
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

    lower = _norm_time(session_start)
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
    """Load the underlying's true 1-min OHLC. Loads
    [start_date - 2*warmup_days, end_date] so SuperTrend is seeded. Returns dt
    (naive IST wall-clock), _date, _time, open, high, low, close.

    NB: parsing the tz-less prefix keeps IST wall-clock; calling .values on a
    tz-aware series shifts to UTC and would break the anchored 15m resample.
    """
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
    df["dt"] = pd.to_datetime(df["datetime"].str.slice(0, 19)).values
    df["_date"] = df["datetime"].str.slice(0, 10).values
    df["_time"] = tcol[keep].values
    logger.info(f"Loaded {len(df):,} spot 1-min bars (incl. warm-up from "
                f"{load_start}).")
    return df[cols].sort_values("dt").reset_index(drop=True)


def load_vix_1m(vix_path: str, start_date: str, end_date: str,
                warmup_days: int = DEFAULT_WARMUP_DAYS,
                session_start: str = SESSION_START,
                session_end: str = SPOT_SESSION_END) -> pd.DataFrame:
    """Load India VIX 1-min CLOSE. Returns dt (naive IST), _date, _time, close.
    Empty frame if the file is missing (the VIX filter then no-ops to NaN)."""
    cols = ["dt", "_date", "_time", "close"]
    if not vix_path or not Path(vix_path).exists():
        logger.warning(f"VIX file not found at {vix_path}; VIX filter disabled.")
        return pd.DataFrame(columns=cols)
    load_start = (_date.fromisoformat(start_date)
                  - timedelta(days=2 * warmup_days)).isoformat()
    df = pd.read_parquet(vix_path, columns=["datetime", "close"])
    d = df["datetime"].str.slice(0, 10)
    df = df[(d >= load_start) & (d <= end_date)]
    if df.empty:
        return pd.DataFrame(columns=cols)
    lower, upper = _norm_time(session_start), _norm_time(session_end)
    tcol = df["datetime"].str.slice(11, 19)
    keep = (tcol >= lower) & (tcol <= upper)
    df = df[keep].copy()
    df["dt"] = pd.to_datetime(df["datetime"].str.slice(0, 19)).values
    df["_date"] = df["datetime"].str.slice(0, 10).values
    df["_time"] = tcol[keep].values
    logger.info(f"Loaded {len(df):,} VIX 1-min bars.")
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
    sizing = config.get("sizing", {}) or {}
    return {
        "st_factor": float(signal.get("st_factor", DEFAULT_ST_FACTOR)),
        "st_atr_period": int(signal.get("st_atr_period", DEFAULT_ST_ATR_PERIOD)),
        "pcr_bull_min": float(signal.get("pcr_bull_min", DEFAULT_PCR_BULL_MIN)),
        "pcr_bear_max": float(signal.get("pcr_bear_max", DEFAULT_PCR_BEAR_MAX)),
        "use_pcr_filter": bool(signal.get("use_pcr_filter", DEFAULT_USE_PCR_FILTER)),
        "use_vix_filter": bool(signal.get("use_vix_filter", DEFAULT_USE_VIX_FILTER)),
        "dvix_bull_max": float(signal.get("dvix_bull_max", DEFAULT_DVIX_BULL_MAX)),
        "dvix_bear_min": float(signal.get("dvix_bear_min", DEFAULT_DVIX_BEAR_MIN)),
        "warmup_days": int(signal.get("warmup_days", DEFAULT_WARMUP_DAYS)),
        "window_start": _norm_time(entry.get("window_start", DEFAULT_WINDOW_START)),
        "window_end": _norm_time(entry.get("window_end", DEFAULT_WINDOW_END)),
        "tp_credit_frac": float(exit_cfg.get("tp_credit_frac", DEFAULT_TP_CREDIT_FRAC)),
        "sl_pts": float(exit_cfg.get("sl_pts", DEFAULT_SL_PTS)),
        "square_off_time": _norm_time(exit_cfg.get("square_off_time",
                                                   DEFAULT_SQUARE_OFF_TIME)),
        "max_missing_bars": int(exit_cfg.get("max_missing_bars",
                                             DEFAULT_MAX_MISSING_BARS)),
        "lots": int(structure.get("lots", 1)),
        "sell_offset_abs": int(structure.get("sell_offset_abs", DEFAULT_SELL_OFFSET)),
        "buy_offset_abs": int(structure.get("buy_offset_abs", DEFAULT_BUY_OFFSET)),
        "min_credit_pts": float(structure.get("min_credit_pts", DEFAULT_MIN_CREDIT_PTS)),
        "max_credit_pts": float(structure.get("max_credit_pts", DEFAULT_MAX_CREDIT_PTS)),
        "max_trades_per_day": int(structure.get("max_trades_per_day",
                                                DEFAULT_MAX_TRADES_PER_DAY)),
        "strike_step": int(structure.get("strike_step",
                                         STRIKE_ROUNDING.get("NIFTY", 50))),
        "expiry_roll": bool(expiry.get("expiry_roll", True)),
        "reference_capital": float(sizing.get("reference_capital", 200000)),
    }


def _expiry_code_for(date_str: str, expiry_roll: bool) -> int:
    """Roll to expiry_code 2 on a weekly-expiry day, else nearest weekly (1)."""
    if not expiry_roll:
        return 1
    d = _date.fromisoformat(date_str)
    return 2 if get_nearest_weekly_expiry(d) == d else 1


def run_backtest(options_df: pd.DataFrame, spot_df: pd.DataFrame,
                 vix_df: pd.DataFrame, config: dict) -> dict:
    """Run the strategy over [backtest_start, backtest_end]."""
    p = parse_config(config)
    bt_start = config["backtest_start"]
    bt_end = config["backtest_end"]
    capital = p["reference_capital"]

    trades: List[StPcrVixTrade] = []
    skip_counts = _empty_skips()
    days_processed = 0
    running_equity = capital

    if options_df.empty or spot_df.empty:
        return {"trades": trades, "config": config, "signals_skipped": 0,
                "skip_reason_counts": _empty_skips(), "days_processed": 0,
                "running_equity": running_equity}

    sig_frame = build_signal_frame(spot_df, vix_df, p)

    # Per-date spot minute maps (open for ATM/entry, close for exit-spot log).
    spot_open_by_date: Dict[str, Dict[str, float]] = {}
    spot_close_by_date: Dict[str, Dict[str, float]] = {}
    for d, t, o, c in zip(spot_df["_date"], spot_df["_time"],
                          spot_df["open"], spot_df["close"]):
        spot_open_by_date.setdefault(d, {})[t] = float(o)
        spot_close_by_date.setdefault(d, {})[t] = float(c)

    bt_opt = options_df[(options_df["_date"] >= bt_start)
                        & (options_df["_date"] <= bt_end)]
    for date_str, day_opt_all in bt_opt.groupby("_date", sort=True):
        days_processed += 1
        date_str = str(date_str)
        code = _expiry_code_for(date_str, p["expiry_roll"])
        day_opt = day_opt_all[day_opt_all["expiry_code"] == code]
        if day_opt.empty:
            logger.warning(f"{date_str}: no rows for expiry_code {code}; skipping.")
            continue

        day_candles = sig_frame[sig_frame["_date"] == date_str]
        if day_candles.empty:
            continue
        pcr_min = pcr_by_minute(day_opt)
        entry_sig = build_day_entry_signals(day_candles, pcr_min, p)
        if not entry_sig:
            continue

        spot_open_map = spot_open_by_date.get(date_str, {})
        spot_close_map = spot_close_by_date.get(date_str, {})

        ctx = StPcrVixDayContext(
            date=date_str, expiry_code=code, lots=p["lots"],
            sell_offset_abs=p["sell_offset_abs"], buy_offset_abs=p["buy_offset_abs"],
            tp_credit_frac=p["tp_credit_frac"], sl_pts=p["sl_pts"],
            min_credit_pts=p["min_credit_pts"], max_credit_pts=p["max_credit_pts"],
            square_off_time=p["square_off_time"],
            max_trades_per_day=p["max_trades_per_day"],
            max_missing_bars=p["max_missing_bars"], strike_step=p["strike_step"],
        )
        day_trades, day_skips = run_one_day(
            day_opt, entry_sig, spot_open_map, spot_close_map, ctx)
        for k, v in day_skips.items():
            skip_counts[k] += v
        for tr in day_trades:
            running_equity += tr.pnl_inr
            tr.return_pct = tr.pnl_inr / capital if capital else 0.0
            tr.running_equity_inr = running_equity
        trades.extend(day_trades)

    return {"trades": trades, "config": config,
            "signals_skipped": sum(skip_counts.values()),
            "skip_reason_counts": skip_counts,
            "days_processed": days_processed,
            "running_equity": running_equity}


# --------------------------------------------------------------------------- #
#  Reporting                                                                  #
# --------------------------------------------------------------------------- #

def build_equity_curve(trades: List[StPcrVixTrade],
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
            "date": t.date, "exit_time": t.exit_time, "equity_inr": equity,
            "drawdown_inr": dd_inr,
            "drawdown_pct": dd_inr / peak if peak else 0.0,
        })
    return pd.DataFrame(rows)


def max_consecutive_losses(pnls: List[float]) -> int:
    longest = current = 0
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


def summarize_metrics(trades: List[StPcrVixTrade],
                      starting_capital: float) -> dict:
    pnls = [t.pnl_inr for t in trades]
    wins = [t for t in trades if t.pnl_inr > 0]
    losses = [t for t in trades if t.pnl_inr < 0]

    equity_curve = build_equity_curve(trades, starting_capital)
    if not equity_curve.empty:
        max_dd_inr = float(equity_curve["drawdown_inr"].max())
        max_dd_pct = float(equity_curve["drawdown_pct"].max())
    else:
        max_dd_inr = max_dd_pct = 0.0

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


def trades_to_dataframe(trades: List[StPcrVixTrade]) -> pd.DataFrame:
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


def write_trades_csv(trades: List[StPcrVixTrade], path) -> None:
    trades_to_dataframe(trades).to_csv(path, index=False)


def write_equity_csv(trades: List[StPcrVixTrade], capital: float, path) -> None:
    build_equity_curve(trades, capital).to_csv(path, index=False)


def print_summary(s: dict, days_processed: int, skip_counts: dict) -> None:
    total_skipped = sum(skip_counts.values())
    lines = [
        f"Days processed: {days_processed}    Days with trades: {s['trading_days']}",
        f"Trades: {s['total_trades']}  (LONG={s['long_trades']}, "
        f"SHORT={s['short_trades']}, rolled={s['rolled_trades']})",
        f"Signals skipped: {total_skipped}  "
        f"(sell_leg_missing={skip_counts['sell_leg_missing']}, "
        f"buy_leg_missing={skip_counts['buy_leg_missing']}, "
        f"spot_missing={skip_counts['spot_missing']}, "
        f"credit_out_of_band={skip_counts['credit_out_of_band']})",
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
        spot_path: Optional[str] = None, vix_path: Optional[str] = None) -> dict:
    """Top-level entrypoint. Loads parquet, runs backtest, writes CSVs."""
    p = parse_config(config)
    options_df = load_filtered_options(
        options_path, start_date=config["backtest_start"],
        end_date=config["backtest_end"], session_start=SESSION_START,
        square_off_time=p["square_off_time"])
    spot_path = spot_path or SPOT_DATA_PATH.get("NIFTY")
    spot_df = load_spot_1m(spot_path, config["backtest_start"],
                           config["backtest_end"], warmup_days=p["warmup_days"])
    vix_path = vix_path or config.get("vix_path", "data/vix/VIX_1m.parquet")
    vix_df = load_vix_1m(vix_path, config["backtest_start"],
                         config["backtest_end"], warmup_days=p["warmup_days"]) \
        if p["use_vix_filter"] else pd.DataFrame(columns=["dt", "_date", "_time", "close"])

    result = run_backtest(options_df, spot_df, vix_df, config)
    trades = result["trades"]
    capital = p["reference_capital"]
    summary = summarize_metrics(trades, capital)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    start_str, end_str = config["backtest_start"], config["backtest_end"]
    trades_path = out / f"st_pcr_vix_credit_spread_trades_{start_str}_{end_str}.csv"
    equity_path = out / f"st_pcr_vix_credit_spread_equity_{start_str}_{end_str}.csv"
    write_trades_csv(trades, trades_path)
    write_equity_csv(trades, capital, equity_path)
    print_summary(summary, result["days_processed"], result["skip_reason_counts"])
    return {
        "trades": trades, "summary": summary,
        "days_processed": result["days_processed"],
        "signals_skipped": result["signals_skipped"],
        "skip_reason_counts": result["skip_reason_counts"],
        "trades_csv": str(trades_path), "equity_csv": str(equity_path),
    }
