"""
Bollinger-Band + RSI + Pivot-Confluence Credit-Spread Backtest Engine.

A mean-reversion premium-selling strategy on NIFTY defined-risk credit spreads.
Signals are evaluated on 30-minute spot bars (anchored to the 09:15 open);
exits are checked on every 1-minute spot/option close after entry.

  SIGNAL series = 30-min NIFTY spot bars (OHLC aggregated from 1-min spot).
  Indicators are CONTINUOUS across days (no daily reset); warm-up days are
  loaded before backtest_start so Bollinger/RSI are seeded.

    bb     = Bollinger(bb_period, bb_std)   on 30m closes  -> upper/mid/lower
    rsi    = RSI(rsi_length)                on 30m closes  (Wilder's smoothing)
    pivots = PP, R1..R3, S1..S3 from the PRIOR 30-min bar's High/Low/Close

  Entry (all must hold on the 30m bar close, time in [window_start, window_end]):
    BEAR-CALL (sell a CALL spread) when:
        upper-band touch  : |close - upper| / close <= band_tol_pct
        RSI confirmation  : rsi > rsi_upper
        ADX filter        : adx < adx_max   (ranging, not strongly trending)
        pivot confluence  : nearest pivot within max(pivot_tol_pct*close, pivot_tol_pts)
      -> SELL ATM+sell_offset CE, BUY ATM+buy_offset CE.
    BULL-PUT (sell a PUT spread) when:
        lower-band touch  : |close - lower| / close <= band_tol_pct
        RSI confirmation  : rsi < rsi_lower
        ADX filter        : adx < adx_max   (ranging, not strongly trending)
        pivot confluence  : (same)
      -> SELL ATM-sell_offset PE, BUY ATM-buy_offset PE.
    (sell_offset=2 strikes = 100 pts, buy_offset=6 = 300 pts -> 200-pt width.)

  Gates:
    * Max ONE position open at a time (a new signal while in a trade is ignored).
    * Max `max_trades_per_day` entries per calendar day.
    * Only credit spreads with a positive net credit are entered.

  Fills (the user's execution rules, matching the codebase convention):
    * ENTRY fills at the 30m signal bar's OWN close minute (the last 1-min bar
      of the bucket) using that minute's CLOSE premium; ATM is also resolved
      from that same minute. No next-minute delay.
    * EXIT is value-based, scanned on EVERY 1-min option close after entry:
        live spread value V = sell_close - buy_close
        TAKE PROFIT : V <= tp_ratio * net_credit   (default 0.5 -> keep half)
        STOP LOSS   : V >= sl_ratio * net_credit   (default 1.5 -> lose half)
      When TP/SL triggers, the EXIT fills at the NEXT 1-min bar's OPEN premium.
    * END-OF-DAY square-off at square_off_time fills at that minute's CLOSE.

  Expiry-day roll: on a weekly-expiry day the strategy trades the NEXT weekly
  expiry (expiry_code 2); on all other days the nearest weekly (code 1).

  P&L (per contract):
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

import pandas as pd

from config import SPOT_DATA_PATH, get_nearest_weekly_expiry
from indicators import get_indicator

logger = logging.getLogger(__name__)

LOT_SIZE_NIFTY = 65  # Mirrors config.LOT_SIZE['NIFTY']; pinned for unit tests.

SESSION_START = "09:15:00"
SPOT_SESSION_END = "15:29:00"       # full NIFTY 1-min session for true-OHLC bars
DEFAULT_TIMEFRAME_MIN = 30
DEFAULT_BB_PERIOD = 20
DEFAULT_BB_STD = 2.0
DEFAULT_RSI_LENGTH = 14
DEFAULT_RSI_UPPER = 60.0
DEFAULT_RSI_LOWER = 30.0
DEFAULT_ADX_PERIOD = 14
DEFAULT_ADX_MAX = 25.0             # only trade when ADX is below this (ranging)
DEFAULT_BAND_TOL_PCT = 0.005       # within 0.5% of the band
DEFAULT_PIVOT_TOL_PCT = 0.001      # 0.1% of price ...
DEFAULT_PIVOT_TOL_PTS = 15.0       # ... or 15 points, whichever is looser
DEFAULT_WINDOW_START = "09:45:00"
DEFAULT_WINDOW_END = "14:00:00"
DEFAULT_SQUARE_OFF_TIME = "15:15:00"
DEFAULT_TP_RATIO = 0.5             # close when spread decays to 50% of credit
DEFAULT_SL_RATIO = 1.5             # close when spread widens to 150% of credit
DEFAULT_SELL_OFFSET = 2            # 100 pts OTM on a 50-pt grid
DEFAULT_BUY_OFFSET = 6             # 300 pts OTM
DEFAULT_MAX_TRADES_PER_DAY = 3
DEFAULT_WARMUP_DAYS = 10

REQUIRED_COLS = [
    "datetime", "option_type", "expiry_type", "expiry_code",
    "strike_offset", "moneyness", "strike", "spot", "open", "close",
    "underlying",
]


def _norm_time(s) -> str:
    """Normalize 'H:MM', 'HH:MM', or 'HH:MM:SS' to 'HH:MM:SS'."""
    parts = str(s).strip().split(":")
    if len(parts) == 2:
        parts.append("0")
    h, m, sec = (int(p) for p in parts)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _minute_of_day(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:5])


def _time_str(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}:00"


# --------------------------------------------------------------------------- #
#  Dataclasses                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class LegFill:
    """Resolved leg of the spread: strike + entry/exit price."""
    option_type: str      # "CE" | "PE"
    side: str             # "BUY" | "SELL"
    lots: int
    strike_offset: int
    strike: float
    entry_price: float
    exit_price: Optional[float] = None


@dataclass
class BbPivotTrade:
    date: str
    signal: str                 # "BEAR_CALL" | "BULL_PUT"
    direction: str              # "SHORT" (sell CE) | "LONG" (sell PE)
    expiry_code: int            # 1 (nearest) | 2 (rolled, expiry day)

    entry_time: str             # HH:MM -- entry fill minute (next-min open)
    entry_spot: float
    atm_strike: float

    exit_time: str              # HH:MM
    exit_reason: str            # "TP" | "SL" | "EOD"
    exit_spot: float
    fill_fallback: bool         # a fill used a different minute / close price

    net_credit_pts: float       # sell_entry - buy_entry (per contract)
    net_credit_inr: float
    pnl_pts: float
    pnl_inr: float
    return_pct: float
    running_equity_inr: float

    legs: Dict[str, LegFill] = field(default_factory=dict)


@dataclass
class BbPivotDayContext:
    date: str
    expiry_code: int = 1
    lots: int = 1
    sell_offset_abs: int = DEFAULT_SELL_OFFSET
    buy_offset_abs: int = DEFAULT_BUY_OFFSET
    tp_ratio: float = DEFAULT_TP_RATIO      # <= 0 disables
    sl_ratio: float = DEFAULT_SL_RATIO      # <= 0 disables
    square_off_time: str = DEFAULT_SQUARE_OFF_TIME
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY
    lot_size: int = LOT_SIZE_NIFTY


# --------------------------------------------------------------------------- #
#  Signal series                                                              #
# --------------------------------------------------------------------------- #

def build_signal_bars(spot_df: pd.DataFrame, timeframe_min: int = DEFAULT_TIMEFRAME_MIN,
                      anchor: str = SESSION_START) -> pd.DataFrame:
    """Aggregate a per-minute spot series into N-min OHLC bars anchored at
    `anchor`.

    `spot_df` needs columns _date, _time and EITHER full 1-min OHLC
    (open/high/low/close) OR a single `spot` snapshot column. With true OHLC the
    bar is open=first open, high=max high, low=min low, close=last close
    (TradingView semantics). With only a `spot` snapshot, OHLC is derived from
    the per-minute snapshots (high=max snapshot, low=min snapshot) -- coarser,
    used only as a fallback when no spot feed is available.

    Returns a frame sorted by (date, bar) with columns: date, bar_start
    (HH:MM:SS), close_minute (HH:MM:SS of the last 1-min bar in the bucket),
    open, high, low, close.
    """
    cols = ["date", "bar_start", "close_minute", "open", "high", "low", "close"]
    if spot_df.empty:
        return pd.DataFrame(columns=cols)
    anchor_min = _minute_of_day(_norm_time(anchor))
    df = spot_df.copy()
    if "close" not in df.columns:                    # snapshot-only -> synth OHLC
        df["open"] = df["high"] = df["low"] = df["close"] = df["spot"]
    df["_mod"] = df["_time"].map(_minute_of_day)
    df = df[df["_mod"] >= anchor_min]
    df["_bucket"] = (df["_mod"] - anchor_min) // timeframe_min
    df = df.sort_values(["_date", "_mod"])

    g = df.groupby(["_date", "_bucket"], sort=True)
    agg = g.agg(open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"),
                close_minute=("_time", "last"))
    buckets = agg.index.get_level_values("_bucket")
    return pd.DataFrame({
        "date": agg.index.get_level_values("_date"),
        "bar_start": [_time_str(anchor_min + int(b) * timeframe_min) for b in buckets],
        "close_minute": agg["close_minute"].values,
        "open": agg["open"].astype(float).values,
        "high": agg["high"].astype(float).values,
        "low": agg["low"].astype(float).values,
        "close": agg["close"].astype(float).values,
    }).reset_index(drop=True)


def generate_signals(bars: pd.DataFrame,
                     bb_period: int = DEFAULT_BB_PERIOD,
                     bb_std: float = DEFAULT_BB_STD,
                     rsi_length: int = DEFAULT_RSI_LENGTH,
                     rsi_upper: float = DEFAULT_RSI_UPPER,
                     rsi_lower: float = DEFAULT_RSI_LOWER,
                     adx_period: int = DEFAULT_ADX_PERIOD,
                     adx_max: float = DEFAULT_ADX_MAX,
                     band_tol_pct: float = DEFAULT_BAND_TOL_PCT,
                     pivot_tol_pct: float = DEFAULT_PIVOT_TOL_PCT,
                     pivot_tol_pts: float = DEFAULT_PIVOT_TOL_PTS,
                     timeframe_min: int = DEFAULT_TIMEFRAME_MIN,
                     window_start: str = DEFAULT_WINDOW_START,
                     window_end: str = DEFAULT_WINDOW_END) -> pd.DataFrame:
    """Add indicator + signal columns to a signal-bar frame.

    Indicators are computed over the WHOLE frame (continuous across days).
    Pivots use the immediately preceding 30-min bar (shift(1)). The trade
    window gate is on the bar's CLOSE time (bar_start + timeframe).
    """
    bars = bars.copy()
    if bars.empty:
        for c in ["rsi", "adx", "bb_upper", "bb_middle", "bb_lower", "pivot_ok",
                  "upper_touch", "lower_touch", "bear_call_sig", "bull_put_sig",
                  "close_time", "in_window"]:
            bars[c] = pd.Series(dtype="float64" if c not in (
                "pivot_ok", "upper_touch", "lower_touch", "bear_call_sig",
                "bull_put_sig", "in_window", "close_time") else "object")
        return bars

    close = bars["close"]

    bars["rsi"] = get_indicator("RSI", name="_rsi", period=rsi_length).calculate(close)
    bars["adx"] = get_indicator("ADX", name="_adx", period=adx_period).calculate(
        close, high=bars["high"], low=bars["low"])
    bb = get_indicator("BOLLINGER", name="_bb", period=bb_period,
                       std_dev=bb_std).calculate(close)
    bars["bb_upper"] = bb["upper"]
    bars["bb_middle"] = bb["middle"]
    bars["bb_lower"] = bb["lower"]

    # Pivots from the PRIOR 30-min bar.
    ph, pl, pc = bars["high"].shift(1), bars["low"].shift(1), bars["close"].shift(1)
    pp = (ph + pl + pc) / 3.0
    r1, s1 = 2 * pp - pl, 2 * pp - ph
    r2, s2 = pp + (ph - pl), pp - (ph - pl)
    r3, s3 = r1 + (ph - pl), s1 - (ph - pl)
    pivots = pd.concat([pp, r1, r2, r3, s1, s2, s3], axis=1)
    nearest_dist = pivots.sub(close, axis=0).abs().min(axis=1)
    pivot_tol = (close * pivot_tol_pct).clip(lower=pivot_tol_pts)
    bars["pivot_ok"] = (nearest_dist <= pivot_tol).fillna(False)

    bars["upper_touch"] = (
        ((close - bars["bb_upper"]).abs() / close) <= band_tol_pct
    ).fillna(False)
    bars["lower_touch"] = (
        ((close - bars["bb_lower"]).abs() / close) <= band_tol_pct
    ).fillna(False)

    # adx_max <= 0 disables the filter (always passes).
    adx_ok = (bars["adx"] < adx_max) if adx_max and adx_max > 0 \
        else pd.Series(True, index=bars.index)
    bars["bear_call_sig"] = (
        bars["upper_touch"] & (bars["rsi"] > rsi_upper) & adx_ok & bars["pivot_ok"]
    ).fillna(False)
    bars["bull_put_sig"] = (
        bars["lower_touch"] & (bars["rsi"] < rsi_lower) & adx_ok & bars["pivot_ok"]
    ).fillna(False)

    bars["close_time"] = [
        _time_str(_minute_of_day(_norm_time(bs)) + timeframe_min)
        for bs in bars["bar_start"]
    ]
    ws, we = _norm_time(window_start), _norm_time(window_end)
    bars["in_window"] = bars["close_time"].between(ws, we)
    return bars


# --------------------------------------------------------------------------- #
#  Per-day driver                                                             #
# --------------------------------------------------------------------------- #

def lookup_by_offset(slice_df: pd.DataFrame, option_type: str,
                     offset: int) -> Optional[pd.Series]:
    if slice_df.empty:
        return None
    rows = slice_df[(slice_df["option_type"] == option_type)
                    & (slice_df["strike_offset"] == offset)]
    if rows.empty:
        return None
    return rows.iloc[0]


def atm_strike_from(slice_df: pd.DataFrame) -> Optional[Tuple[float, float]]:
    """Return (atm_strike, spot) from a slice using moneyness=='ATM'."""
    if slice_df.empty:
        return None
    atm_rows = slice_df[slice_df["moneyness"] == "ATM"]
    if atm_rows.empty:
        return None
    if len(atm_rows) > 1:
        atm_rows = atm_rows.assign(
            _d=(atm_rows["strike"] - atm_rows["spot"]).abs()
        ).sort_values("_d")
    r = atm_rows.iloc[0]
    return float(r["strike"]), float(r["spot"])


def _leg_maps(day_df: pd.DataFrame, time_col: pd.Series, option_type: str,
              strike: float) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Minute -> open / close price maps for one contract (first row per minute)."""
    sub = day_df[(day_df["option_type"] == option_type)
                 & (day_df["strike"] == strike)]
    if sub.empty:
        return {}, {}
    sub = sub.assign(_t=time_col[sub.index].values).drop_duplicates("_t")
    open_map = dict(zip(sub["_t"], sub["open"].astype(float)))
    close_map = dict(zip(sub["_t"], sub["close"].astype(float)))
    return open_map, close_map


class _OpenPosition:
    """Mutable in-flight trade state while scanning a day."""

    def __init__(self, trade: BbPivotTrade, entry_minute: str,
                 option_type: str, sell_strike: float, buy_strike: float):
        self.trade = trade
        self.entry_minute = entry_minute      # HH:MM:SS (fill minute)
        self.option_type = option_type
        self.sell_strike = sell_strike
        self.buy_strike = buy_strike
        self.sell_open: Dict[str, float] = {}
        self.sell_close: Dict[str, float] = {}
        self.buy_open: Dict[str, float] = {}
        self.buy_close: Dict[str, float] = {}


def _open_position(slice_t: pd.DataFrame, t: str, signal: str,
                   ctx: BbPivotDayContext) -> Optional[_OpenPosition]:
    """Resolve strikes from a minute slice and fill at its CLOSE prices.

    Returns None only when the ATM / spread legs are missing (a data gap worth
    walking forward for). A non-positive net credit is NOT filtered here -- the
    caller discards those without walking forward.
    """
    atm = atm_strike_from(slice_t)
    if atm is None:
        return None
    atm_strike, spot = atm

    if signal == "BULL_PUT":
        direction = "LONG"
        opt, sell_off, buy_off = "PE", -ctx.sell_offset_abs, -ctx.buy_offset_abs
    else:  # BEAR_CALL
        direction = "SHORT"
        opt, sell_off, buy_off = "CE", ctx.sell_offset_abs, ctx.buy_offset_abs

    sell_row = lookup_by_offset(slice_t, opt, sell_off)
    buy_row = lookup_by_offset(slice_t, opt, buy_off)
    if sell_row is None or buy_row is None:
        return None

    sell_entry = float(sell_row["close"])   # next-min CLOSE premium
    buy_entry = float(buy_row["close"])
    net_credit_pts = sell_entry - buy_entry
    contracts = ctx.lot_size * ctx.lots

    trade = BbPivotTrade(
        date=ctx.date, signal=signal, direction=direction,
        expiry_code=ctx.expiry_code,
        entry_time=t[:5], entry_spot=spot, atm_strike=atm_strike,
        exit_time="", exit_reason="", exit_spot=float("nan"),
        fill_fallback=False,
        net_credit_pts=net_credit_pts,
        net_credit_inr=net_credit_pts * contracts,
        pnl_pts=0.0, pnl_inr=0.0,
        return_pct=0.0, running_equity_inr=0.0,
        legs={
            "sell": LegFill(opt, "SELL", ctx.lots, sell_off,
                            float(sell_row["strike"]), sell_entry),
            "buy": LegFill(opt, "BUY", ctx.lots, buy_off,
                           float(buy_row["strike"]), buy_entry),
        },
    )
    return _OpenPosition(trade, t, opt, float(sell_row["strike"]),
                         float(buy_row["strike"]))


def _find_entry_fill(day_df: pd.DataFrame, time_col: pd.Series,
                     minutes: List[str], start_idx: int, signal: str,
                     square: str, ctx: BbPivotDayContext
                     ) -> Optional[_OpenPosition]:
    """Fill the entry at the first minute >= start_idx (strictly before the
    square-off) where ATM + both legs exist. CLOSE prices are used. start_idx is
    the signal bar's close minute; fill_fallback is flagged only if a data gap
    forces the fill past that minute."""
    intended = minutes[start_idx] if start_idx < len(minutes) else None
    for j in range(start_idx, len(minutes)):
        m = minutes[j]
        if m >= square:
            break
        pos = _open_position(day_df[time_col == m], m, signal, ctx)
        if pos is not None:
            pos.trade.fill_fallback = (m != intended)
            return pos
    return None


def _close_position(pos: _OpenPosition, start_idx: int, reason: str,
                    minutes: List[str], square: str, spot_map: Dict[str, float],
                    ctx: BbPivotDayContext, price_field: str = "open") -> None:
    """Fill the exit at the first minute >= start_idx (capped at square-off)
    where both legs have rows, using `price_field` prices (default OPEN, the
    next-minute fill rule). If none, fall back to the last known CLOSE prices
    before start_idx. Never drops the trade."""
    trade = pos.trade
    n = len(minutes)
    fill_minute = None
    used_field = price_field

    for j in range(start_idx, n):
        m = minutes[j]
        if m > square:
            break
        if m in pos.sell_close and m in pos.buy_close:
            fill_minute = m
            break

    if fill_minute is None:
        # Backward fallback: last minute (< start_idx) with both legs -> CLOSE.
        for j in range(min(start_idx, n) - 1, -1, -1):
            m = minutes[j]
            if m in pos.sell_close and m in pos.buy_close:
                fill_minute = m
                used_field = "close"
                break

    sell_entry = trade.legs["sell"].entry_price
    buy_entry = trade.legs["buy"].entry_price

    if fill_minute is None:
        # No leg data anywhere -> exit at entry prices (flat P&L), flagged.
        anchor_idx = min(start_idx, n - 1) if n else 0
        trade.exit_time = minutes[anchor_idx][:5] if n else ""
        trade.exit_spot = float("nan")
        trade.fill_fallback = True
        sell_exit, buy_exit = sell_entry, buy_entry
    else:
        if used_field == "open":
            sell_exit = pos.sell_open[fill_minute]
            buy_exit = pos.buy_open[fill_minute]
        else:
            sell_exit = pos.sell_close[fill_minute]
            buy_exit = pos.buy_close[fill_minute]
        intended = minutes[start_idx] if start_idx < n else None
        trade.exit_time = fill_minute[:5]
        trade.exit_spot = float(spot_map.get(fill_minute, float("nan")))
        trade.fill_fallback = (trade.fill_fallback or (fill_minute != intended)
                               or (used_field != price_field))

    trade.legs["sell"].exit_price = sell_exit
    trade.legs["buy"].exit_price = buy_exit
    trade.exit_reason = reason

    contracts = ctx.lot_size * ctx.lots
    trade.pnl_pts = (sell_entry + buy_exit) - (sell_exit + buy_entry)
    trade.pnl_inr = trade.pnl_pts * contracts


def run_one_day(day_df: pd.DataFrame, day_bars: pd.DataFrame,
                ctx: BbPivotDayContext) -> Tuple[List[BbPivotTrade], int]:
    """Run the strategy over one day.

    `day_bars` is this day's 30-min signal bars (output of generate_signals,
    filtered to ctx.date). Returns (trades, n_signals_skipped).
    """
    trades: List[BbPivotTrade] = []
    skipped = 0
    if day_df.empty:
        return trades, skipped

    time_col = day_df["_time"] if "_time" in day_df.columns else \
        day_df["datetime"].str.slice(11, 19)
    square = _norm_time(ctx.square_off_time)
    minutes = sorted(m for m in time_col.unique() if m <= square)
    if not minutes:
        return trades, skipped

    # Minute -> spot (first row per minute).
    sm = day_df.assign(_t=time_col.values).drop_duplicates("_t")
    spot_map = dict(zip(sm["_t"], sm["spot"].astype(float)))

    # Signal direction keyed at each in-window 30-min bar's close minute.
    entry_sig_at: Dict[str, str] = {}
    for _, b in day_bars.iterrows():
        if not bool(b["in_window"]):
            continue
        cm = _norm_time(b["close_minute"])
        if bool(b["bull_put_sig"]):
            entry_sig_at[cm] = "BULL_PUT"
        elif bool(b["bear_call_sig"]):
            entry_sig_at[cm] = "BEAR_CALL"

    pos: Optional[_OpenPosition] = None
    trades_today = 0
    pending_exit: Optional[str] = None     # reason awaiting next-min fill
    tp_active = ctx.tp_ratio > 0
    sl_active = ctx.sl_ratio > 0

    for i, t in enumerate(minutes):
        is_square = t == square

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

        # --- (C) Value-based SL/TP detection on this 1-min option close -----
        if pos is not None and pending_exit is None and t > pos.entry_minute:
            sc = pos.sell_close.get(t)
            bc = pos.buy_close.get(t)
            if (sc is not None and bc is not None
                    and not isnan(sc) and not isnan(bc)):
                value = sc - bc
                credit = pos.trade.net_credit_pts
                tp_hit = tp_active and value <= ctx.tp_ratio * credit
                sl_hit = sl_active and value >= ctx.sl_ratio * credit
                if sl_hit or tp_hit:
                    reason = "SL" if sl_hit else "TP"
                    if i + 1 < len(minutes):
                        pending_exit = reason          # fill next minute's OPEN
                    else:
                        _close_position(pos, i, reason, minutes, square,
                                        spot_map, ctx, price_field="close")
                        pos = None

        # --- (D) Entry fills at THIS minute's CLOSE -- the 30-min signal
        #         bar's own close minute. ATM and the leg premiums are both
        #         read from this same bar (no next-minute delay).
        if (t in entry_sig_at and pos is None
                and trades_today < ctx.max_trades_per_day and not is_square):
            sig = entry_sig_at[t]
            new_pos = _find_entry_fill(day_df, time_col, minutes, i, sig,
                                       square, ctx)
            if new_pos is None:
                skipped += 1
                logger.warning(f"{ctx.date} {t}: {sig} entry skipped "
                               "(ATM or spread legs missing).")
            elif new_pos.trade.net_credit_pts <= 0:
                skipped += 1
                logger.warning(f"{ctx.date} {t}: {sig} entry skipped "
                               "(non-positive net credit).")
            else:
                new_pos.sell_open, new_pos.sell_close = _leg_maps(
                    day_df, time_col, new_pos.option_type, new_pos.sell_strike)
                new_pos.buy_open, new_pos.buy_close = _leg_maps(
                    day_df, time_col, new_pos.option_type, new_pos.buy_strike)
                pos = new_pos
                trades.append(new_pos.trade)
                trades_today += 1

    if pos is not None:
        # Data ended before the square-off bar: close on the last minute.
        _close_position(pos, len(minutes) - 1, "EOD", minutes, square,
                        spot_map, ctx, price_field="close")

    return trades, skipped


# --------------------------------------------------------------------------- #
#  Data loading                                                               #
# --------------------------------------------------------------------------- #

def load_filtered_options(
    options_path: str, start_date: str, end_date: str,
    expiry_type: str = "WEEK", expiry_roll: bool = True,
    session_start: str = SESSION_START,
    square_off_time: str = DEFAULT_SQUARE_OFF_TIME,
    warmup_days: int = DEFAULT_WARMUP_DAYS,
) -> pd.DataFrame:
    """Predicate-pushdown load of NIFTY options minute bars.

    Loads [start_date - 2*warmup_days calendar days, end_date] so Bollinger/RSI
    are seeded before the backtest window, restricted to minute bars in
    [session_start, square_off_time]. When `expiry_roll` is on (weekly), both
    expiry_code 1 and 2 are loaded so the per-day driver can roll on expiry day.
    """
    import pyarrow.parquet as pq

    codes = [1, 2] if (expiry_type == "WEEK" and expiry_roll) else [1]
    load_start = (_date.fromisoformat(start_date)
                  - timedelta(days=2 * warmup_days)).isoformat()
    filters = [
        ("underlying", "=", "NIFTY"),
        ("expiry_type", "=", expiry_type),
        ("expiry_code", "in", codes),
    ]
    logger.info("Loading parquet with predicate pushdown...")
    table = pq.read_table(options_path, columns=REQUIRED_COLS, filters=filters)
    df = table.to_pandas()
    if df.empty:
        return df

    df = df[(df["datetime"].str.slice(0, 10) >= load_start)
            & (df["datetime"].str.slice(0, 10) <= end_date)]

    lower = _norm_time(session_start)
    upper = _norm_time(square_off_time)
    time_col = df["datetime"].str.slice(11, 19)
    keep = (time_col >= lower) & (time_col <= upper)
    df = df[keep].copy()
    df["_time"] = time_col[keep].values
    df["_date"] = df["datetime"].str.slice(0, 10).values
    logger.info(f"Loaded {len(df):,} rows for {df['_date'].nunique()} trading days "
                f"(codes={codes}, incl. warm-up from {load_start}).")
    return df


def load_spot_ohlc(spot_path: str, start_date: str, end_date: str,
                   session_start: str = SESSION_START,
                   session_end: str = SPOT_SESSION_END,
                   warmup_days: int = DEFAULT_WARMUP_DAYS) -> pd.DataFrame:
    """Load the underlying's TRUE 1-min OHLC -- the signal-series source.

    The options parquet only carries a per-minute `spot` snapshot, whose
    aggregated high/low are wrong for ADX and pivots. This loads the dedicated
    spot feed instead. Loads [start_date - 2*warmup_days, end_date] within
    [session_start, session_end]; returns _date, _time, open, high, low, close.
    """
    cols = ["_date", "_time", "open", "high", "low", "close"]
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
    df["_time"] = tcol[keep].values
    df["_date"] = df["datetime"].str.slice(0, 10).values
    logger.info(f"Loaded {len(df):,} spot 1-min bars for the signal series "
                f"(incl. warm-up from {load_start}).")
    return df[cols]


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
        "timeframe_min": int(signal.get("timeframe_min", DEFAULT_TIMEFRAME_MIN)),
        "bb_period": int(signal.get("bb_period", DEFAULT_BB_PERIOD)),
        "bb_std": float(signal.get("bb_std", DEFAULT_BB_STD)),
        "rsi_length": int(signal.get("rsi_length", DEFAULT_RSI_LENGTH)),
        "rsi_upper": float(signal.get("rsi_upper", DEFAULT_RSI_UPPER)),
        "rsi_lower": float(signal.get("rsi_lower", DEFAULT_RSI_LOWER)),
        "adx_period": int(signal.get("adx_period", DEFAULT_ADX_PERIOD)),
        "adx_max": float(signal.get("adx_max", DEFAULT_ADX_MAX)),
        "band_tol_pct": float(signal.get("band_tol_pct", DEFAULT_BAND_TOL_PCT)),
        "pivot_tol_pct": float(signal.get("pivot_tol_pct", DEFAULT_PIVOT_TOL_PCT)),
        "pivot_tol_pts": float(signal.get("pivot_tol_pts", DEFAULT_PIVOT_TOL_PTS)),
        "warmup_days": int(signal.get("warmup_days", DEFAULT_WARMUP_DAYS)),
        "window_start": _norm_time(entry.get("window_start", DEFAULT_WINDOW_START)),
        "window_end": _norm_time(entry.get("window_end", DEFAULT_WINDOW_END)),
        "tp_ratio": float(exit_cfg.get("tp_ratio", DEFAULT_TP_RATIO)),
        "sl_ratio": float(exit_cfg.get("sl_ratio", DEFAULT_SL_RATIO)),
        "square_off_time": _norm_time(exit_cfg.get("square_off_time",
                                                   DEFAULT_SQUARE_OFF_TIME)),
        "lots": int(structure.get("lots", 1)),
        "sell_offset_abs": int(structure.get("sell_offset_abs", DEFAULT_SELL_OFFSET)),
        "buy_offset_abs": int(structure.get("buy_offset_abs", DEFAULT_BUY_OFFSET)),
        "max_trades_per_day": int(structure.get("max_trades_per_day",
                                                DEFAULT_MAX_TRADES_PER_DAY)),
        "expiry_type": str(expiry.get("expiry_type", "WEEK")).upper(),
        "expiry_roll": bool(expiry.get("expiry_roll", True)),
        "reference_capital": float(config["sizing"]["reference_capital"]),
    }


def _expiry_code_for(date_str: str, p: dict) -> int:
    """Pick the expiry_code for a trading day: roll to 2 on a weekly expiry day."""
    if not (p["expiry_type"] == "WEEK" and p["expiry_roll"]):
        return 1
    d = _date.fromisoformat(date_str)
    nearest = get_nearest_weekly_expiry(d)
    return 2 if nearest == d else 1


def run_backtest(df: pd.DataFrame, config: dict,
                 spot_ohlc: Optional[pd.DataFrame] = None) -> dict:
    """Run the strategy over [backtest_start, backtest_end].

    `df` (options) may include extra warm-up days BEFORE backtest_start (and
    both expiry codes): they seed the indicators / feed the roll but are never
    traded outside the window. `spot_ohlc` is the underlying's true 1-min OHLC
    used to build the 30-min signal bars; if omitted, the options `spot`
    snapshot is used as a fallback.
    """
    p = parse_config(config)
    bt_start = config["backtest_start"]
    bt_end = config["backtest_end"]
    capital = p["reference_capital"]

    trades: List[BbPivotTrade] = []
    signals_skipped = 0
    days_processed = 0
    running_equity = capital

    if df.empty:
        return {"trades": trades, "config": config,
                "signals_skipped": 0, "days_processed": 0}

    # Signal bars come from the underlying's TRUE 1-min OHLC (spot feed). The
    # options `spot` column is only a per-minute snapshot, whose aggregated
    # high/low are wrong for ADX/pivots, so it is used only as a fallback.
    if spot_ohlc is not None and not spot_ohlc.empty:
        spot_df = spot_ohlc.sort_values(["_date", "_time"])
    else:
        logger.warning("No spot OHLC feed -- falling back to the options `spot` "
                       "snapshot for signal bars (ADX/pivots will be approximate).")
        spot_df = (df.drop_duplicates(["_date", "_time"])[["_date", "_time", "spot"]]
                   .sort_values(["_date", "_time"]))
    bars = build_signal_bars(spot_df, timeframe_min=p["timeframe_min"])
    bars = generate_signals(
        bars, bb_period=p["bb_period"], bb_std=p["bb_std"],
        rsi_length=p["rsi_length"], rsi_upper=p["rsi_upper"],
        rsi_lower=p["rsi_lower"], adx_period=p["adx_period"],
        adx_max=p["adx_max"], band_tol_pct=p["band_tol_pct"],
        pivot_tol_pct=p["pivot_tol_pct"], pivot_tol_pts=p["pivot_tol_pts"],
        timeframe_min=p["timeframe_min"], window_start=p["window_start"],
        window_end=p["window_end"])

    bt_df = df[(df["_date"] >= bt_start) & (df["_date"] <= bt_end)]
    for date_str, day_df_all in bt_df.groupby("_date", sort=True):
        days_processed += 1
        code = _expiry_code_for(str(date_str), p)
        day_df = day_df_all[day_df_all["expiry_code"] == code]
        if day_df.empty:
            logger.warning(f"{date_str}: no rows for expiry_code {code}; skipping day.")
            continue
        day_bars = bars[bars["date"] == str(date_str)]
        ctx = BbPivotDayContext(
            date=str(date_str),
            expiry_code=code,
            lots=p["lots"],
            sell_offset_abs=p["sell_offset_abs"],
            buy_offset_abs=p["buy_offset_abs"],
            tp_ratio=p["tp_ratio"],
            sl_ratio=p["sl_ratio"],
            square_off_time=p["square_off_time"],
            max_trades_per_day=p["max_trades_per_day"],
        )
        day_trades, day_skipped = run_one_day(day_df, day_bars, ctx)
        signals_skipped += day_skipped
        for t in day_trades:
            running_equity += t.pnl_inr
            t.return_pct = t.pnl_inr / capital if capital else 0.0
            t.running_equity_inr = running_equity
        trades.extend(day_trades)

    return {"trades": trades, "config": config,
            "signals_skipped": signals_skipped,
            "days_processed": days_processed}


# --------------------------------------------------------------------------- #
#  Reporting                                                                  #
# --------------------------------------------------------------------------- #

def build_equity_curve(trades: List[BbPivotTrade],
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


def summarize_metrics(trades: List[BbPivotTrade],
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
        "bear_call_trades": sum(1 for t in trades if t.signal == "BEAR_CALL"),
        "bull_put_trades": sum(1 for t in trades if t.signal == "BULL_PUT"),
        "exit_reason_counts": _count_by(trades, lambda t: t.exit_reason),
        "rolled_trades": sum(1 for t in trades if t.expiry_code == 2),
        "fill_fallback_count": sum(1 for t in trades if t.fill_fallback),
        "trading_days": len({t.date for t in trades}),
    }


def trades_to_dataframe(trades: List[BbPivotTrade]) -> pd.DataFrame:
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


def write_trades_csv(trades: List[BbPivotTrade], path) -> None:
    trades_to_dataframe(trades).to_csv(path, index=False)


def write_equity_csv(trades: List[BbPivotTrade], capital: float, path) -> None:
    build_equity_curve(trades, capital).to_csv(path, index=False)


def print_summary(s: dict, days_processed: int, signals_skipped: int) -> None:
    lines = [
        f"Days processed: {days_processed}    Days with trades: {s['trading_days']}",
        f"Trades: {s['total_trades']}  (BEAR_CALL={s['bear_call_trades']}, "
        f"BULL_PUT={s['bull_put_trades']}, rolled={s['rolled_trades']})",
        f"Signals skipped (missing legs / non-positive credit): {signals_skipped}",
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
    df = load_filtered_options(
        options_path,
        start_date=config["backtest_start"],
        end_date=config["backtest_end"],
        expiry_type=p["expiry_type"],
        expiry_roll=p["expiry_roll"],
        square_off_time=p["square_off_time"],
        warmup_days=p["warmup_days"],
    )
    spot_path = spot_path or SPOT_DATA_PATH.get("NIFTY")
    spot_ohlc = None
    if spot_path:
        spot_ohlc = load_spot_ohlc(
            spot_path, config["backtest_start"], config["backtest_end"],
            warmup_days=p["warmup_days"])
    result = run_backtest(df, config, spot_ohlc=spot_ohlc)
    trades = result["trades"]
    capital = p["reference_capital"]
    summary = summarize_metrics(trades, capital)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    start_str = config["backtest_start"]
    end_str = config["backtest_end"]
    trades_path = out / f"bb_pivot_spread_trades_{start_str}_{end_str}.csv"
    equity_path = out / f"bb_pivot_spread_equity_{start_str}_{end_str}.csv"
    write_trades_csv(trades, trades_path)
    write_equity_csv(trades, capital, equity_path)
    print_summary(summary, result["days_processed"], result["signals_skipped"])
    return {
        "trades": trades, "summary": summary,
        "days_processed": result["days_processed"],
        "signals_skipped": result["signals_skipped"],
        "trades_csv": str(trades_path),
        "equity_csv": str(equity_path),
    }
