"""
Regime RSI Spread Backtest Engine.

Port of the TradingView Pine v6 "Regime Bias WMA44 + RSI/RSIMA" strategy to
defined-risk NIFTY credit spreads. Everything is evaluated on a single
15-minute spot-bar series (the user's choice -- both RSI and WMA on 15m).

  SIGNAL series = 15-min NIFTY spot bars (anchored to the 09:15 session open).
  Indicators are CONTINUOUS across days (TradingView behaviour -- no daily
  reset; warm-up days are loaded before backtest_start so RSI/WMA are seeded
  by the time trading begins).

    rsi    = RSI(rsi_length)            on 15m closes (Wilder's smoothing)
    rsiMA  = SMA(rsi_ma_length) of rsi
    wma    = WMA(wma_length)            on 15m closes  (regime line)
    regimeBull = close > wma

  LONG  signal: crossover(rsi, rsiMA)  -- RSI crosses ABOVE its MA
                -> bull-put credit spread: SELL ATM-2 PE, BUY ATM-6 PE.
  SHORT signal: crossunder(rsi, rsiMA) -- RSI crosses BELOW its MA
                -> bear-call credit spread: SELL ATM+2 CE, BUY ATM+6 CE.

  Gates (all must hold on the 15m signal bar):
    * Directional filter (use_filter): long only when regimeBull, short only
      when not regimeBull. Off -> direction is decided purely by the cross.
    * Distance gate: |close - wma| <= max_dist points.
    * Trade window: the 15m bar STARTS inside [window_start, window_end).
    * One trade per regime per day: a `tradeTaken` flag resets each new day
      AND on every intraday regime flip (re-arms one more entry). Entry also
      requires being flat -- there is NO reversal; an opposite signal while a
      position is open is ignored.

  Fills (the user's execution rules):
    * ENTRY fills at the NEXT 1-min bar's OPEN premium after the 15m signal
      bar closes.
    * SL/TP are scanned on EVERY 1-min spot close after the entry minute:
        LONG  SL spot <= entry_spot - sl_points ; TP spot >= entry_spot + tp_points
        SHORT SL spot >= entry_spot + sl_points ; TP spot <= entry_spot - tp_points
    * When SL/TP triggers, the EXIT fills at the NEXT 1-min bar's OPEN premium.
    * TIME square-off at square_off_time fills at that minute's OPEN.

  Fills walk forward to the first minute (capped at square-off) where both
  legs have rows; if none exists, the last known closes are used and the trade
  is flagged fill_fallback. A placed trade is never dropped.
"""

import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from indicators import get_indicator

logger = logging.getLogger(__name__)

LOT_SIZE_NIFTY = 65  # Mirrors config.LOT_SIZE['NIFTY']; pinned for unit tests.

SESSION_START = "09:15:00"
DEFAULT_TIMEFRAME_MIN = 15
DEFAULT_RSI_LENGTH = 7
DEFAULT_RSI_MA_LENGTH = 7
DEFAULT_WMA_LENGTH = 44
DEFAULT_MAX_DIST = 36.0
DEFAULT_USE_FILTER = True
DEFAULT_WINDOW_START = "09:30:00"
DEFAULT_WINDOW_END = "15:00:00"
DEFAULT_SQUARE_OFF_TIME = "15:15:00"
DEFAULT_SL_POINTS = 36.0
DEFAULT_TP_POINTS = 36.0
DEFAULT_WARMUP_DAYS = 10

REQUIRED_COLS = [
    "datetime", "option_type", "expiry_type", "expiry_code",
    "strike_offset", "moneyness", "strike", "spot", "open", "close", "oi",
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
class RegimeRsiTrade:
    date: str
    direction: str              # "LONG" | "SHORT"

    entry_time: str             # HH:MM -- entry fill minute (next-min open)
    entry_spot: float           # SL/TP anchor (spot at the entry fill minute)
    atm_strike: float

    exit_time: str              # HH:MM
    exit_reason: str            # "TP" | "SL" | "TIME"
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
class RegimeRsiDayContext:
    date: str
    lots: int = 1
    sell_offset_abs: int = 2
    buy_offset_abs: int = 6
    sl_points: float = DEFAULT_SL_POINTS    # 0 disables
    tp_points: float = DEFAULT_TP_POINTS    # 0 disables
    window_start: str = DEFAULT_WINDOW_START
    window_end: str = DEFAULT_WINDOW_END
    square_off_time: str = DEFAULT_SQUARE_OFF_TIME
    use_filter: bool = DEFAULT_USE_FILTER
    lot_size: int = LOT_SIZE_NIFTY


# --------------------------------------------------------------------------- #
#  Signal series                                                              #
# --------------------------------------------------------------------------- #

def build_signal_bars(spot_df: pd.DataFrame, timeframe_min: int = DEFAULT_TIMEFRAME_MIN,
                      anchor: str = SESSION_START) -> pd.DataFrame:
    """Aggregate a per-minute spot series into N-min bars anchored at `anchor`.

    `spot_df` needs columns _date, _time, spot (one row per minute).
    Returns a frame sorted by (date, bar) with columns:
      date, bar_start (HH:MM:SS), close_minute (HH:MM:SS of the last 1-min
      bar present in the bucket), close (spot at that minute).
    """
    if spot_df.empty:
        return pd.DataFrame(columns=["date", "bar_start", "close_minute", "close"])
    anchor_min = _minute_of_day(_norm_time(anchor))
    df = spot_df[["_date", "_time", "spot"]].copy()
    df["_mod"] = df["_time"].map(_minute_of_day)
    df = df[df["_mod"] >= anchor_min]
    df["_bucket"] = (df["_mod"] - anchor_min) // timeframe_min
    df = df.sort_values(["_date", "_mod"])
    last = df.groupby(["_date", "_bucket"], sort=True).tail(1)
    return pd.DataFrame({
        "date": last["_date"].values,
        "bar_start": [
            _time_str(anchor_min + b * timeframe_min) for b in last["_bucket"]
        ],
        "close_minute": last["_time"].values,
        "close": last["spot"].astype(float).values,
    }).reset_index(drop=True)


def generate_signals(bars: pd.DataFrame, rsi_length: int = DEFAULT_RSI_LENGTH,
                     rsi_ma_length: int = DEFAULT_RSI_MA_LENGTH,
                     wma_length: int = DEFAULT_WMA_LENGTH,
                     max_dist: float = DEFAULT_MAX_DIST) -> pd.DataFrame:
    """Add indicator + signal columns to a signal-bar frame.

    Indicators are computed over the WHOLE frame (continuous across days, like
    TradingView). Signal on bar i uses bars i-1 and i:
      long_sig  : rsi[i-1] <= rsiMA[i-1] and rsi[i] > rsiMA[i]   (crossover)
      short_sig : rsi[i-1] >= rsiMA[i-1] and rsi[i] < rsiMA[i]   (crossunder)
    regime_bull = close > wma ; regime_flip = regime_bull changed vs prev bar.
    """
    bars = bars.copy()
    close = bars["close"]

    rsi = get_indicator("RSI", name="_rsi", period=rsi_length).calculate(close)
    rsi_ma = rsi.rolling(rsi_ma_length, min_periods=rsi_ma_length).mean()
    wma = get_indicator("WMA", name="_wma", period=wma_length).calculate(close)

    bars["rsi"] = rsi
    bars["rsi_ma"] = rsi_ma
    bars["wma"] = wma
    bars["regime_bull"] = (close > wma)
    bars["long_sig"] = (
        (rsi.shift(1) <= rsi_ma.shift(1)) & (rsi > rsi_ma)
    ).fillna(False)
    bars["short_sig"] = (
        (rsi.shift(1) >= rsi_ma.shift(1)) & (rsi < rsi_ma)
    ).fillna(False)
    bars["within_dist"] = ((close - wma).abs() <= max_dist).fillna(False)

    flip = bars["regime_bull"].ne(bars["regime_bull"].shift(1))
    if len(flip):
        flip.iloc[0] = False
    bars["regime_flip"] = flip
    return bars


# --------------------------------------------------------------------------- #
#  Per-day driver                                                             #
# --------------------------------------------------------------------------- #

def lookup_by_strike(slice_df: pd.DataFrame, option_type: str,
                     strike: float) -> Optional[pd.Series]:
    if slice_df.empty:
        return None
    rows = slice_df[(slice_df["option_type"] == option_type)
                    & (slice_df["strike"] == strike)]
    if rows.empty:
        return None
    return rows.iloc[0]


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


class _OpenPosition:
    """Mutable in-flight trade state while scanning a day."""

    def __init__(self, trade: RegimeRsiTrade, entry_minute: str,
                 option_type: str, sell_strike: float, buy_strike: float):
        self.trade = trade
        self.entry_minute = entry_minute      # HH:MM:SS (fill minute)
        self.option_type = option_type
        self.sell_strike = sell_strike
        self.buy_strike = buy_strike


def _open_position(slice_t: pd.DataFrame, t: str, direction: str,
                   ctx: RegimeRsiDayContext) -> Optional[_OpenPosition]:
    """Resolve strikes from a minute slice and fill at its OPEN prices."""
    atm = atm_strike_from(slice_t)
    if atm is None:
        return None
    atm_strike, spot = atm

    if direction == "LONG":
        opt, sell_off, buy_off = "PE", -ctx.sell_offset_abs, -ctx.buy_offset_abs
    else:
        opt, sell_off, buy_off = "CE", ctx.sell_offset_abs, ctx.buy_offset_abs

    sell_row = lookup_by_offset(slice_t, opt, sell_off)
    buy_row = lookup_by_offset(slice_t, opt, buy_off)
    if sell_row is None or buy_row is None:
        return None

    sell_entry = float(sell_row["open"])   # next-min OPEN premium
    buy_entry = float(buy_row["open"])
    net_credit_pts = sell_entry - buy_entry
    contracts = ctx.lot_size * ctx.lots

    trade = RegimeRsiTrade(
        date=ctx.date, direction=direction,
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
                     minutes: List[str], start_idx: int, direction: str,
                     square: str, ctx: RegimeRsiDayContext
                     ) -> Optional[_OpenPosition]:
    """Fill the entry at the first minute >= start_idx (strictly before the
    square-off) where ATM + both legs exist. OPEN prices are used. Flags
    fill_fallback if it had to walk past the immediate next minute."""
    intended = minutes[start_idx] if start_idx < len(minutes) else None
    for j in range(start_idx, len(minutes)):
        m = minutes[j]
        if m >= square:
            break
        pos = _open_position(day_df[time_col == m], m, direction, ctx)
        if pos is not None:
            pos.trade.fill_fallback = (m != intended)
            return pos
    return None


def _close_position(pos: _OpenPosition, start_idx: int, reason: str,
                    day_df: pd.DataFrame, time_col: pd.Series,
                    minutes: List[str], square: str,
                    ctx: RegimeRsiDayContext) -> None:
    """Fill the exit at the first minute >= start_idx (capped at square-off)
    where both legs have rows, using OPEN prices. If none, fall back to the
    last known CLOSE prices at or before start_idx. Never drops the trade."""
    trade = pos.trade
    n = len(minutes)
    fill_minute = None
    sell_row = buy_row = None
    used_open = True

    for j in range(start_idx, n):
        m = minutes[j]
        if m > square:
            break
        slice_m = day_df[time_col == m]
        sr = lookup_by_strike(slice_m, pos.option_type, pos.sell_strike)
        br = lookup_by_strike(slice_m, pos.option_type, pos.buy_strike)
        if sr is not None and br is not None:
            fill_minute, sell_row, buy_row = m, sr, br
            break

    if fill_minute is None:
        # Backward fallback: last minute (< start_idx) with both legs -> CLOSE.
        for j in range(min(start_idx, n) - 1, -1, -1):
            m = minutes[j]
            slice_m = day_df[time_col == m]
            sr = lookup_by_strike(slice_m, pos.option_type, pos.sell_strike)
            br = lookup_by_strike(slice_m, pos.option_type, pos.buy_strike)
            if sr is not None and br is not None:
                fill_minute, sell_row, buy_row = m, sr, br
                used_open = False
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
        price_field = "open" if used_open else "close"
        sell_exit = float(sell_row[price_field])
        buy_exit = float(buy_row[price_field])
        intended = minutes[start_idx] if start_idx < n else None
        trade.exit_time = fill_minute[:5]
        trade.exit_spot = float(sell_row["spot"])
        trade.fill_fallback = trade.fill_fallback or (fill_minute != intended) \
            or (not used_open)

    trade.legs["sell"].exit_price = sell_exit
    trade.legs["buy"].exit_price = buy_exit
    trade.exit_reason = reason

    contracts = ctx.lot_size * ctx.lots
    trade.pnl_pts = (sell_entry + buy_exit) - (sell_exit + buy_entry)
    trade.pnl_inr = trade.pnl_pts * contracts


def run_one_day(day_df: pd.DataFrame, day_bars: pd.DataFrame,
                ctx: RegimeRsiDayContext) -> Tuple[List[RegimeRsiTrade], int]:
    """Run the strategy over one day.

    `day_bars` is this day's 15-min signal bars (output of generate_signals,
    filtered to ctx.date). Returns (trades, n_signals_skipped).
    """
    trades: List[RegimeRsiTrade] = []
    skipped = 0
    if day_df.empty:
        return trades, skipped

    time_col = day_df["_time"] if "_time" in day_df.columns else \
        day_df["datetime"].str.slice(11, 19)
    square = _norm_time(ctx.square_off_time)
    minutes = sorted(m for m in time_col.unique() if m <= square)
    if not minutes:
        return trades, skipped

    ws, we = _norm_time(ctx.window_start), _norm_time(ctx.window_end)

    # Per-minute lookups built from the day's 15m bars.
    regime_flip_at: Dict[str, bool] = {}
    entry_dir_at: Dict[str, str] = {}
    for _, b in day_bars.iterrows():
        cm = _norm_time(b["close_minute"])
        regime_flip_at[cm] = bool(b["regime_flip"])
        bar_start = _norm_time(b["bar_start"])
        if not (ws <= bar_start < we) or not bool(b["within_dist"]):
            continue
        regime_bull = bool(b["regime_bull"])
        long_ok = regime_bull if ctx.use_filter else True
        short_ok = (not regime_bull) if ctx.use_filter else True
        if bool(b["long_sig"]) and long_ok:
            entry_dir_at[cm] = "LONG"
        elif bool(b["short_sig"]) and short_ok:
            entry_dir_at[cm] = "SHORT"

    pos: Optional[_OpenPosition] = None
    trade_taken = False              # 1-trade-per-regime-per-day gate
    pending_entry: Optional[str] = None   # direction awaiting next-min fill
    pending_exit: Optional[str] = None    # reason awaiting next-min fill
    sl_active = ctx.sl_points > 0
    tp_active = ctx.tp_points > 0

    for i, t in enumerate(minutes):
        slice_t = day_df[time_col == t]
        if slice_t.empty:
            continue
        spot_t = float(slice_t.iloc[0]["spot"])
        is_square = t == square

        # --- (A) Pending EXIT fills at this minute's OPEN -------------------
        if pending_exit is not None and pos is not None:
            _close_position(pos, i, pending_exit, day_df, time_col,
                            minutes, square, ctx)
            pos = None
            pending_exit = None

        # --- (B) Pending ENTRY fills at this minute's OPEN ------------------
        if pending_entry is not None and pos is None:
            new_pos = _find_entry_fill(day_df, time_col, minutes, i,
                                       pending_entry, square, ctx)
            if new_pos is None:
                skipped += 1
                logger.warning(f"{ctx.date} {t}: {pending_entry} entry skipped "
                               "(ATM or spread legs missing).")
            else:
                pos = new_pos
                trades.append(new_pos.trade)
                trade_taken = True
            pending_entry = None

        # --- (C) Regime flip at t re-arms the daily allowance ---------------
        if regime_flip_at.get(t, False):
            trade_taken = False

        # --- (D) Square-off: flatten at the deadline's OPEN -----------------
        if is_square:
            if pos is not None:
                _close_position(pos, i, "TIME", day_df, time_col,
                                minutes, square, ctx)
                pos = None
            break

        # --- (E) SL/TP detection on this 1-min spot close -------------------
        if pos is not None and pending_exit is None and t > pos.entry_minute:
            anchor = pos.trade.entry_spot
            if pos.trade.direction == "LONG":
                sl_hit = sl_active and spot_t <= anchor - ctx.sl_points
                tp_hit = tp_active and spot_t >= anchor + ctx.tp_points
            else:
                sl_hit = sl_active and spot_t >= anchor + ctx.sl_points
                tp_hit = tp_active and spot_t <= anchor - ctx.tp_points
            if sl_hit or tp_hit:
                reason = "SL" if sl_hit else "TP"
                if i + 1 < len(minutes):
                    pending_exit = reason          # fill next minute's OPEN
                else:
                    _close_position(pos, i, reason, day_df, time_col,
                                    minutes, square, ctx)
                    pos = None

        # --- (F) Entry signal at t schedules a next-min-open fill -----------
        if (t in entry_dir_at and pos is None and pending_entry is None
                and not trade_taken and not is_square):
            if i + 1 < len(minutes) and minutes[i + 1] < square:
                pending_entry = entry_dir_at[t]
            # else: no room to enter before square-off -> drop silently.

    if pos is not None:
        # Data ended before the square-off bar: close on the last minute.
        _close_position(pos, len(minutes) - 1, "TIME", day_df, time_col,
                        minutes, square, ctx)

    return trades, skipped


# --------------------------------------------------------------------------- #
#  Data loading                                                               #
# --------------------------------------------------------------------------- #

def load_filtered_options(
    options_path: str, start_date: str, end_date: str,
    expiry_type: str = "WEEK", expiry_code: int = 1,
    session_start: str = SESSION_START,
    square_off_time: str = DEFAULT_SQUARE_OFF_TIME,
    warmup_days: int = DEFAULT_WARMUP_DAYS,
) -> pd.DataFrame:
    """Predicate-pushdown load of NIFTY options minute bars.

    Loads [start_date - 2*warmup_days calendar days, end_date] so RSI/WMA are
    seeded before the backtest window, restricted to minute bars in
    [session_start, square_off_time].
    """
    import pyarrow.parquet as pq

    load_start = (_date.fromisoformat(start_date)
                  - timedelta(days=2 * warmup_days)).isoformat()
    filters = [
        ("underlying", "=", "NIFTY"),
        ("expiry_type", "=", expiry_type),
        ("expiry_code", "=", int(expiry_code)),
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
                f"(incl. warm-up from {load_start}).")
    return df


# --------------------------------------------------------------------------- #
#  Backtest orchestrator                                                      #
# --------------------------------------------------------------------------- #

def parse_config(config: dict) -> dict:
    signal = config.get("signal", {}) or {}
    entry = config.get("entry", {}) or {}
    exit_cfg = config.get("exit", {}) or {}
    structure = config.get("structure", {}) or {}
    return {
        "timeframe_min": int(signal.get("timeframe_min", DEFAULT_TIMEFRAME_MIN)),
        "rsi_length": int(signal.get("rsi_length", DEFAULT_RSI_LENGTH)),
        "rsi_ma_length": int(signal.get("rsi_ma_length", DEFAULT_RSI_MA_LENGTH)),
        "wma_length": int(signal.get("wma_length", DEFAULT_WMA_LENGTH)),
        "max_dist": float(signal.get("max_dist", DEFAULT_MAX_DIST)),
        "use_filter": bool(signal.get("use_filter", DEFAULT_USE_FILTER)),
        "warmup_days": int(signal.get("warmup_days", DEFAULT_WARMUP_DAYS)),
        "window_start": _norm_time(entry.get("window_start", DEFAULT_WINDOW_START)),
        "window_end": _norm_time(entry.get("window_end", DEFAULT_WINDOW_END)),
        "sl_points": float(exit_cfg.get("sl_points", DEFAULT_SL_POINTS)),
        "tp_points": float(exit_cfg.get("tp_points", DEFAULT_TP_POINTS)),
        "square_off_time": _norm_time(exit_cfg.get("square_off_time",
                                                   DEFAULT_SQUARE_OFF_TIME)),
        "lots": int(structure.get("lots", 1)),
        "sell_offset_abs": int(structure.get("sell_offset_abs", 2)),
        "buy_offset_abs": int(structure.get("buy_offset_abs", 6)),
        "reference_capital": float(config["sizing"]["reference_capital"]),
    }


def run_backtest(df: pd.DataFrame, config: dict) -> dict:
    """Run the strategy over [backtest_start, backtest_end].

    `df` may include extra warm-up days BEFORE backtest_start: they seed the
    indicators but are never traded.
    """
    p = parse_config(config)
    bt_start = config["backtest_start"]
    bt_end = config["backtest_end"]
    capital = p["reference_capital"]

    trades: List[RegimeRsiTrade] = []
    signals_skipped = 0
    days_processed = 0
    running_equity = capital

    if df.empty:
        return {"trades": trades, "config": config,
                "signals_skipped": 0, "days_processed": 0}

    spot_df = (df.drop_duplicates(["_date", "_time"])[["_date", "_time", "spot"]]
               .sort_values(["_date", "_time"]))
    bars = build_signal_bars(spot_df, timeframe_min=p["timeframe_min"])
    bars = generate_signals(bars, rsi_length=p["rsi_length"],
                            rsi_ma_length=p["rsi_ma_length"],
                            wma_length=p["wma_length"], max_dist=p["max_dist"])

    bt_df = df[(df["_date"] >= bt_start) & (df["_date"] <= bt_end)]
    for date_str, day_df in bt_df.groupby("_date", sort=True):
        days_processed += 1
        day_bars = bars[bars["date"] == str(date_str)]
        ctx = RegimeRsiDayContext(
            date=str(date_str),
            lots=p["lots"],
            sell_offset_abs=p["sell_offset_abs"],
            buy_offset_abs=p["buy_offset_abs"],
            sl_points=p["sl_points"],
            tp_points=p["tp_points"],
            window_start=p["window_start"],
            window_end=p["window_end"],
            square_off_time=p["square_off_time"],
            use_filter=p["use_filter"],
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

def build_equity_curve(trades: List[RegimeRsiTrade],
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


def summarize_metrics(trades: List[RegimeRsiTrade],
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
        "long_trades": sum(1 for t in trades if t.direction == "LONG"),
        "short_trades": sum(1 for t in trades if t.direction == "SHORT"),
        "exit_reason_counts": _count_by(trades, lambda t: t.exit_reason),
        "fill_fallback_count": sum(1 for t in trades if t.fill_fallback),
        "trading_days": len({t.date for t in trades}),
    }


def trades_to_dataframe(trades: List[RegimeRsiTrade]) -> pd.DataFrame:
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


def write_trades_csv(trades: List[RegimeRsiTrade], path) -> None:
    trades_to_dataframe(trades).to_csv(path, index=False)


def write_equity_csv(trades: List[RegimeRsiTrade], capital: float, path) -> None:
    build_equity_curve(trades, capital).to_csv(path, index=False)


def print_summary(s: dict, days_processed: int, signals_skipped: int) -> None:
    lines = [
        f"Days processed: {days_processed}    Days with trades: {s['trading_days']}",
        f"Trades: {s['total_trades']}  (LONG={s['long_trades']}, SHORT={s['short_trades']})",
        f"Signals skipped (missing legs): {signals_skipped}",
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


def run(config: dict, options_path: str, output_dir: str) -> dict:
    """Top-level entrypoint. Loads parquet, runs backtest, writes CSVs."""
    p = parse_config(config)
    expiry_cfg = config.get("expiry", {}) or {}
    df = load_filtered_options(
        options_path,
        start_date=config["backtest_start"],
        end_date=config["backtest_end"],
        expiry_type=str(expiry_cfg.get("expiry_type", "WEEK")).upper(),
        expiry_code=int(expiry_cfg.get("expiry_code", 1)),
        square_off_time=p["square_off_time"],
        warmup_days=p["warmup_days"],
    )
    result = run_backtest(df, config)
    trades = result["trades"]
    capital = p["reference_capital"]
    summary = summarize_metrics(trades, capital)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    start_str = config["backtest_start"]
    end_str = config["backtest_end"]
    trades_path = out / f"regime_rsi_spread_trades_{start_str}_{end_str}.csv"
    equity_path = out / f"regime_rsi_spread_equity_{start_str}_{end_str}.csv"
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
