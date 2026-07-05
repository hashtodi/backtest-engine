"""
OI Wall Backtest Engine - NIFTY OI-wall-driven vertical credit spread.

Strategy:
  Daily entry. At `wall_pick_time` (default 10:00) IST, scan the 20 OTM
  contracts (CE strike_offset +1..+10 and PE strike_offset -1..-10) of the
  nearest weekly or monthly expiry; the one with the single highest OI is
  the WALL.

  Signal vs fill bars (no look-ahead):
    SIGNAL bar  = entry_time (default 10:15). Re-snapshot WALL price/OI and
                  evaluate two conditions:
      C1: price(entry_time) <= price(wall_pick_time)
      C2: oi(entry_time)    >= oi(wall_pick_time)
                  ATM and spread leg STRIKES are also resolved here (the
                  trader makes the strike-selection decision based on
                  signal-bar data only).
    FILL bar    = entry_time + 1 minute (T+1). The spread legs (resolved at
                  the signal bar by absolute strike) are FILLED at the
                  T+1 bar's OPEN. No look-ahead -- the strike choice never
                  uses fill-bar information.

  Spread structure (anchored to FILL-bar ATM):
    CE wall  -> SELL ATM+2 CE, BUY ATM+4 CE   (bear-call credit spread)
    PE wall  -> SELL ATM-2 PE, BUY ATM-4 PE   (bull-put  credit spread)
  Both structures collect net premium at entry (sell strike is closer to
  ATM and therefore richer than the buy strike).

  Between (entry_fill_time, force_exit_time) scan each minute bar's CLOSE
  for three exit signals:
    BREACH : CE wall and spot_close >= wall_strike   (inclusive, no buffer)
             PE wall and spot_close <= wall_strike
    SL     : live spread P&L <= -sl_inr * lots       (per-lot SL)
    TP     : live spread P&L >= tp_inr * lots        (per-lot TP)
  If multiple fire on the same minute close, priority is BREACH > SL > TP.
  Signal-based exits (TP/SL/BREACH) detect at T's CLOSE and fill at the
  NEXT minute's OPEN (T+1 open). Uniform regardless of the breach toggle.
  No look-ahead.
  If no signal hits, exit both legs at force_exit_time (default 15:00)
  CLOSE with reason "TIME" (the TIME exit is a planned deadline, not a
  signal -- no T+1 shift). Set TP/SL to 0 to disable. wall_breach_enabled
  toggles only the BREACH check; the T+1-open fill rule for TP/SL applies
  in both states. P&L scales with lots.

  Profit-lock ("trailing") SL step:
    When trail_arm_inr > 0 and running spread P&L first reaches
    trail_arm_inr * lots (per-lot arm threshold), the stop is ARMED: from
    that bar on the SL is re-pinned UP to trail_lock_inr * lots (a locked
    profit level), replacing the -sl_inr hard stop for the rest of the day.
    This is a one-time step (sticky), NOT a continuous ratchet. The fixed
    TP and the pre-arm hard SL are unchanged; the locked-SL exit is reported
    with reason "TSL" and uses the same detect-at-T / fill-at-T+1-open rule.
    trail_arm_inr = 0 disables the step (default), leaving legacy behaviour.
"""

import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date as _date, datetime as _dt, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

LOT_SIZE_NIFTY = 65  # Mirrors config.LOT_SIZE['NIFTY']; pinned for unit tests.

DEFAULT_WALL_PICK_TIME = "10:00:00"
DEFAULT_ENTRY_TIME = "10:30:00"
DEFAULT_FORCE_EXIT_TIME = "15:00:00"


def _norm_time(s) -> str:
    """Normalize 'H:MM', 'HH:MM', or 'HH:MM:SS' to 'HH:MM:SS'."""
    parts = str(s).strip().split(":")
    if len(parts) == 2:
        parts.append("0")
    h, m, sec = (int(p) for p in parts)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _plus_one_minute(t: str) -> str:
    """'10:15:00' -> '10:16:00'."""
    return (_dt.strptime(_norm_time(t), "%H:%M:%S") + timedelta(minutes=1)).strftime("%H:%M:%S")

REQUIRED_COLS = [
    "datetime", "option_type", "expiry_type", "expiry_code",
    "strike_offset", "moneyness", "strike", "spot", "open", "close", "oi",
    "underlying",
]


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
class OiWallTrade:
    date: str

    # Wall signal snapshot
    wall_option_type: str       # "CE" / "PE" / ""
    wall_strike: float
    wall_offset: int
    wall_price_pick: float
    wall_oi_pick: float
    wall_price_entry: float
    wall_oi_entry: float
    cond_price_le: bool         # C1: price(1030) <= price(10am)
    cond_oi_ge: bool            # C2: oi(1030)    >= oi(10am)
    conditions_passed: int

    # Spread context
    atm_strike: float
    spot_at_entry: float
    spot_at_exit: float         # spot at the bar where the exit actually filled

    # Execution
    signal_time: str            # bar where entry conditions were evaluated
    entry_time: str             # bar where spread was filled (signal + 1 min)
    exit_signal_time: str       # bar where exit signal fired (== exit_time for TIME)
    exit_time: str              # bar where spread was closed (signal + 1 for TP/SL/BREACH)
    exit_reason: str            # "TIME" | "TP" | "SL" | "BREACH" | "" (when skipped)
    net_credit_pts: float       # sell_entry - buy_entry  (per 1 contract)
    net_credit_inr: float       # net_credit_pts * lot_size
    pnl_pts: float
    pnl_inr: float
    return_pct: float
    running_equity_inr: float

    skip_reason: Optional[str]
    legs: Dict[str, LegFill] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
#  Core helpers                                                               #
# --------------------------------------------------------------------------- #

def pick_wall(slice_10am: pd.DataFrame) -> Optional[Tuple[str, int, float, float, float]]:
    """Find the WALL across 20 OTM strikes at 10:00.

    Eligible: CE rows with strike_offset in [1..10], PE rows with [-10..-1].
    Winner: highest OI. Tiebreak: prefer CE; then smaller |offset|; then lower strike.
    Returns (option_type, offset, strike, price, oi) or None if no candidate.
    """
    if slice_10am.empty:
        return None

    ot = slice_10am["option_type"].to_numpy()
    off = slice_10am["strike_offset"].to_numpy()
    eligible = (
        ((ot == "CE") & (off >= 1) & (off <= 10))
        | ((ot == "PE") & (off >= -10) & (off <= -1))
    )
    cands = slice_10am[eligible]
    if cands.empty:
        return None

    cands = cands.assign(
        _abs_off=cands["strike_offset"].abs(),
        # ce_priority: True (1) for CE so it wins on cross-type tie.
        _ce_priority=(cands["option_type"] == "CE").astype(int),
    ).sort_values(
        by=["oi", "_ce_priority", "_abs_off", "strike"],
        ascending=[False, False, True, True],
    )
    row = cands.iloc[0]
    return (
        str(row["option_type"]),
        int(row["strike_offset"]),
        float(row["strike"]),
        float(row["close"]),
        float(row["oi"]),
    )


def lookup_by_strike(
    slice_df: pd.DataFrame, option_type: str, strike: float
) -> Optional[pd.Series]:
    """Find a single row matching (option_type, strike) in the slice."""
    if slice_df.empty:
        return None
    rows = slice_df[
        (slice_df["option_type"] == option_type)
        & (slice_df["strike"] == strike)
    ]
    if rows.empty:
        return None
    return rows.iloc[0]


def lookup_by_offset(
    slice_df: pd.DataFrame, option_type: str, offset: int
) -> Optional[pd.Series]:
    """Find a single row matching (option_type, strike_offset) in the slice."""
    if slice_df.empty:
        return None
    rows = slice_df[
        (slice_df["option_type"] == option_type)
        & (slice_df["strike_offset"] == offset)
    ]
    if rows.empty:
        return None
    return rows.iloc[0]


def atm_strike_from(slice_df: pd.DataFrame) -> Optional[Tuple[float, float]]:
    """Return (atm_strike, spot) from a slice using moneyness=='ATM'.

    If multiple rows tag ATM, pick the one with smallest |strike - spot|.
    """
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


def check_conditions(
    p_10: float, oi_10: float,
    p_1030: float, oi_1030: float,
) -> Tuple[bool, bool, int]:
    """Evaluate the two entry conditions. Returns (c1, c2, count_passed)."""
    c1 = p_1030 <= p_10        # price contracted or flat
    c2 = oi_1030 >= oi_10      # OI defended or grew
    return c1, c2, int(c1) + int(c2)


# --------------------------------------------------------------------------- #
#  Per-day driver                                                             #
# --------------------------------------------------------------------------- #

@dataclass
class DayContext:
    date: _date
    min_conditions_to_enter: int          # default 1 (of 2)
    wall_pick_time: str = DEFAULT_WALL_PICK_TIME  # HH:MM:SS
    entry_time: str = DEFAULT_ENTRY_TIME          # HH:MM:SS
    force_exit_time: str = DEFAULT_FORCE_EXIT_TIME  # HH:MM:SS
    lots: int = 1                         # 1 lot = lot_size contracts
    sell_offset_abs: int = 2              # short leg's |strike_offset| from ATM
    buy_offset_abs: int = 4               # long leg's |strike_offset| from ATM (must be > sell)
    tp_inr: float = 1000.0                # PER LOT; 0 disables. Threshold = tp_inr * lots.
    sl_inr: float = 2000.0                # PER LOT; 0 disables. Threshold = sl_inr * lots.
    trail_arm_inr: float = 0.0            # PER LOT; 0 disables. Arm the profit-lock when live P&L >= this * lots.
    trail_lock_inr: float = 200.0         # PER LOT; once armed, SL is re-pinned to this profit level (* lots).
    wall_breach_enabled: bool = True      # exit when spot crosses the wall strike
    lot_size: int = LOT_SIZE_NIFTY        # contracts per lot


def _empty_trade(ctx: DayContext, skip_reason: str) -> OiWallTrade:
    nan = float("nan")
    return OiWallTrade(
        date=ctx.date.isoformat(),
        wall_option_type="", wall_strike=nan, wall_offset=0,
        wall_price_pick=nan, wall_oi_pick=nan,
        wall_price_entry=nan, wall_oi_entry=nan,
        cond_price_le=False, cond_oi_ge=False,
        conditions_passed=0,
        atm_strike=nan, spot_at_entry=nan, spot_at_exit=nan,
        signal_time="", entry_time="", exit_signal_time="", exit_time="", exit_reason="",
        net_credit_pts=0.0, net_credit_inr=0.0,
        pnl_pts=0.0, pnl_inr=0.0,
        return_pct=0.0, running_equity_inr=0.0,
        skip_reason=skip_reason, legs={},
    )


def run_one_day(day_df: pd.DataFrame, ctx: DayContext) -> OiWallTrade:
    """Run the WALL strategy for a single trading day.

    `day_df` should be the per-day slice of the parquet, restricted by
    `load_filtered_options` to minute bars in [wall_pick_time,
    force_exit_time] for the chosen expiry.
    """
    if day_df.empty:
        return _empty_trade(ctx, "no_data_on_entry_day")

    time_col = day_df["_time"] if "_time" in day_df.columns else \
        day_df["datetime"].str.slice(11, 19)

    entry_fill_time = _plus_one_minute(ctx.entry_time)

    slice_pick   = day_df[time_col == ctx.wall_pick_time]
    slice_signal = day_df[time_col == ctx.entry_time]      # T: conditions checked here
    slice_fill   = day_df[time_col == entry_fill_time]     # T+1: spread filled here
    slice_forced = day_df[time_col == ctx.force_exit_time]

    if slice_pick.empty:
        return _empty_trade(ctx, "no_wall_pick_bar")
    if slice_signal.empty:
        return _empty_trade(ctx, "no_entry_bar")

    # --- WALL pick at wall_pick_time ---
    wall = pick_wall(slice_pick)
    if wall is None:
        return _empty_trade(ctx, "no_wall_candidates")
    w_opt, w_off, w_strike, p_pick, oi_pick = wall

    # Pre-populate trade with the wall-pick snapshot so it survives any later skip.
    trade = _empty_trade(ctx, skip_reason=None)
    trade.wall_option_type = w_opt
    trade.wall_strike = w_strike
    trade.wall_offset = w_off
    trade.wall_price_pick = p_pick
    trade.wall_oi_pick = oi_pick

    # --- WALL re-snapshot at SIGNAL bar (entry_time) ---
    w_row_signal = lookup_by_strike(slice_signal, w_opt, w_strike)
    if w_row_signal is None:
        trade.skip_reason = "wall_missing_at_entry"
        return trade
    p_signal = float(w_row_signal["close"])
    oi_signal = float(w_row_signal["oi"])
    trade.wall_price_entry = p_signal
    trade.wall_oi_entry = oi_signal

    # --- Conditions (evaluated on SIGNAL bar) ---
    c1, c2, n_pass = check_conditions(p_pick, oi_pick, p_signal, oi_signal)
    trade.cond_price_le = bool(c1)
    trade.cond_oi_ge = bool(c2)
    trade.conditions_passed = n_pass

    if n_pass < ctx.min_conditions_to_enter:
        trade.skip_reason = f"conditions_not_met:{n_pass}/2"
        return trade

    # --- Strike selection at the SIGNAL bar (no look-ahead) ----------------
    # Decision made at signal close: ATM and spread leg strikes are RESOLVED
    # from the signal bar's data. Fill prices come from the T+1 (fill) bar's
    # OPEN later, by absolute strike.
    atm = atm_strike_from(slice_signal)
    if atm is None:
        trade.skip_reason = "atm_missing_at_signal"
        return trade
    atm_strike, spot = atm
    trade.atm_strike = atm_strike
    trade.spot_at_entry = spot

    sell_abs = int(ctx.sell_offset_abs)
    buy_abs = int(ctx.buy_offset_abs)
    if w_opt == "CE":
        sell_off, buy_off = sell_abs, buy_abs
    else:
        sell_off, buy_off = -sell_abs, -buy_abs

    sell_row_signal = lookup_by_offset(slice_signal, w_opt, sell_off)
    buy_row_signal = lookup_by_offset(slice_signal, w_opt, buy_off)
    if sell_row_signal is None or buy_row_signal is None:
        trade.skip_reason = "spread_leg_missing_at_signal"
        return trade
    sell_strike = float(sell_row_signal["strike"])
    buy_strike = float(buy_row_signal["strike"])

    # --- Fill at T+1: read OPEN prices for the strikes resolved above ------
    if slice_fill.empty:
        trade.skip_reason = "no_entry_fill_bar"
        return trade
    sell_row_fill = lookup_by_strike(slice_fill, w_opt, sell_strike)
    buy_row_fill = lookup_by_strike(slice_fill, w_opt, buy_strike)
    if sell_row_fill is None or buy_row_fill is None:
        trade.skip_reason = "spread_leg_missing_at_fill"
        return trade

    # Entry fills at T+1 OPEN (next minute's first tick) -- symmetric with the
    # exit fill rule. Detection at T's close, fill at T+1's open. No look-ahead.
    sell_entry = float(sell_row_fill["open"])
    buy_entry = float(buy_row_fill["open"])

    # --- Intraday exit scan (close-only) over minutes in (entry_fill_time, force_exit_time) ---
    # Detect at T's close; on hit, fill at T+1 OPEN. Priority: BREACH > SL > TP.
    sell_exit: Optional[float] = None
    buy_exit: Optional[float] = None
    spot_exit: float = float("nan")
    exit_signal_time_str = ctx.force_exit_time[:5]
    exit_time_str = ctx.force_exit_time[:5]
    exit_reason = "TIME"

    tp_threshold_inr = ctx.tp_inr * ctx.lots
    sl_threshold_inr = ctx.sl_inr * ctx.lots
    tp_active = tp_threshold_inr > 0
    sl_active = sl_threshold_inr > 0
    breach_active = bool(ctx.wall_breach_enabled)
    # Profit-lock ("trailing") SL step: once live P&L first reaches the arm
    # threshold, the SL is re-pinned UP to the locked profit level for the
    # rest of the day (sticky, one-time step -- not a ratchet).
    trail_active = ctx.trail_arm_inr > 0
    arm_threshold_inr = ctx.trail_arm_inr * ctx.lots
    lock_level_inr = ctx.trail_lock_inr * ctx.lots
    trail_armed = False
    if tp_active or sl_active or breach_active or trail_active:
        time_col_day = day_df["_time"] if "_time" in day_df.columns else \
            day_df["datetime"].str.slice(11, 19)
        intraday_times = sorted(
            t for t in day_df.loc[
                (time_col_day > entry_fill_time) & (time_col_day < ctx.force_exit_time),
                "_time",
            ].unique()
        )
        for t in intraday_times:
            slice_t = day_df[time_col_day == t]
            sr = lookup_by_strike(slice_t, w_opt, sell_strike)
            br = lookup_by_strike(slice_t, w_opt, buy_strike)
            if sr is None or br is None:
                continue
            s_t = float(sr["close"])
            b_t = float(br["close"])
            spot_t = float(sr["spot"])
            live_pts = (sell_entry + b_t) - (s_t + buy_entry)
            live_inr = live_pts * ctx.lot_size * ctx.lots
            tp_hit = tp_active and live_inr >= tp_threshold_inr
            # Arm the profit-lock the first time running P&L clears the arm
            # threshold (sticky for the rest of the day).
            if trail_active and not trail_armed and live_inr >= arm_threshold_inr:
                trail_armed = True
            # Once armed, the stop is the locked profit level; otherwise it is
            # the pre-arm hard SL. The armed exit is reported as "TSL".
            if trail_armed:
                sl_hit = live_inr <= lock_level_inr
                sl_reason = "TSL"
            elif sl_active:
                sl_hit = live_inr <= -sl_threshold_inr
                sl_reason = "SL"
            else:
                sl_hit = False
                sl_reason = "SL"
            if breach_active:
                breach_hit = (spot_t >= w_strike) if w_opt == "CE" else (spot_t <= w_strike)
            else:
                breach_hit = False
            if not (breach_hit or sl_hit or tp_hit):
                continue

            # Priority: BREACH > SL/TSL > TP (within the same close).
            if breach_hit:
                reason = "BREACH"
            elif sl_hit:
                reason = sl_reason
            else:
                reason = "TP"

            # Uniform fill rule: detect at T's close, fill at T+1 OPEN.
            # No look-ahead, applies whether breach is enabled or not.
            # If T+1 leg row is missing, skip this hit and keep scanning.
            fill_t = _plus_one_minute(t)
            slice_exit = day_df[time_col_day == fill_t]
            sr_exit = lookup_by_strike(slice_exit, w_opt, sell_strike)
            br_exit = lookup_by_strike(slice_exit, w_opt, buy_strike)
            if sr_exit is None or br_exit is None:
                continue
            sell_exit = float(sr_exit["open"])
            buy_exit = float(br_exit["open"])
            # spot at T+1 OPEN approximated by spot at T's close (the parquet
            # samples `spot` once per minute -- close-of-minute -- so the value
            # at the instant T+1 opens equals T's reported spot).
            spot_exit = spot_t
            exit_signal_time_str = t[:5]
            exit_time_str = fill_t[:5]
            exit_reason = reason
            break

    # --- Fallback: TIME exit at force_exit_time by absolute strike (no T+1 shift) ---
    if sell_exit is None:
        if slice_forced.empty:
            trade.skip_reason = "no_force_exit_bar"
            return trade
        sell_exit_row = lookup_by_strike(slice_forced, w_opt, sell_strike)
        buy_exit_row = lookup_by_strike(slice_forced, w_opt, buy_strike)
        if sell_exit_row is None or buy_exit_row is None:
            trade.skip_reason = "exit_leg_missing_at_force_exit"
            return trade
        sell_exit = float(sell_exit_row["close"])
        buy_exit = float(buy_exit_row["close"])
        spot_exit = float(sell_exit_row["spot"])
        exit_signal_time_str = ctx.force_exit_time[:5]
        exit_time_str = ctx.force_exit_time[:5]

    # --- P&L (scaled by lots) ---
    # Per 1 contract:
    #   short side P&L = sell_entry - sell_exit  (we sold high, buy back)
    #   long  side P&L = buy_exit  - buy_entry   (we bought low, sell at exit)
    # Total per contract = (sell_entry + buy_exit) - (sell_exit + buy_entry)
    # INR = points * lot_size * lots
    contracts = ctx.lot_size * ctx.lots
    net_credit_pts = sell_entry - buy_entry          # entry credit (per contract)
    pnl_pts = (sell_entry + buy_exit) - (sell_exit + buy_entry)
    pnl_inr = pnl_pts * contracts

    trade.signal_time = ctx.entry_time[:5]
    trade.entry_time = entry_fill_time[:5]
    trade.spot_at_exit = spot_exit
    trade.exit_signal_time = exit_signal_time_str
    trade.exit_time = exit_time_str
    trade.exit_reason = exit_reason
    trade.net_credit_pts = net_credit_pts
    trade.net_credit_inr = net_credit_pts * contracts
    trade.pnl_pts = pnl_pts
    trade.pnl_inr = pnl_inr

    sell_key = f"{w_opt.lower()}_short"
    buy_key = f"{w_opt.lower()}_long"
    trade.legs = {
        sell_key: LegFill(
            option_type=w_opt, side="SELL", lots=ctx.lots,
            strike_offset=sell_off, strike=sell_strike,
            entry_price=sell_entry, exit_price=sell_exit,
        ),
        buy_key: LegFill(
            option_type=w_opt, side="BUY", lots=ctx.lots,
            strike_offset=buy_off, strike=buy_strike,
            entry_price=buy_entry, exit_price=buy_exit,
        ),
    }
    return trade


# --------------------------------------------------------------------------- #
#  Data loading                                                               #
# --------------------------------------------------------------------------- #

def load_filtered_options(
    options_path: str, start_date: str, end_date: str,
    expiry_type: str = "MONTH", expiry_code: int = 1,
    wall_pick_time: str = DEFAULT_WALL_PICK_TIME,
    force_exit_time: str = DEFAULT_FORCE_EXIT_TIME,
) -> pd.DataFrame:
    """Predicate-pushdown load: NIFTY <expiry_type> code=<expiry_code>, only
    minute bars in [wall_pick_time, force_exit_time] so the intraday TP/SL
    scanner has data.

    Returns a DataFrame with an extra `_time` column (HH:MM:SS) for fast
    per-timestamp filtering.
    """
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

    # Date range filter on the raw ISO string (datetime is stored as a string
    # like "2025-01-01T10:00:00+05:30") -- avoids parsing 5M+ timestamps.
    df = df[(df["datetime"].str.slice(0, 10) >= start_date)
            & (df["datetime"].str.slice(0, 10) <= end_date)]

    # Keep every minute in [wall_pick_time, force_exit_time] (lexicographic compare on HH:MM:SS).
    lower = _norm_time(wall_pick_time)
    upper = _norm_time(force_exit_time)
    time_col = df["datetime"].str.slice(11, 19)
    keep = (time_col >= lower) & (time_col <= upper)
    df = df[keep].copy()
    df["_time"] = time_col[keep].values
    df["_date"] = df["datetime"].str.slice(0, 10).values
    logger.info(f"Loaded {len(df):,} rows for {df['_date'].nunique()} trading days.")
    return df


# --------------------------------------------------------------------------- #
#  Backtest orchestrator                                                      #
# --------------------------------------------------------------------------- #

def parse_config(config: dict) -> dict:
    entry = config.get("entry", {})
    exit_cfg = config.get("exit", {}) or {}
    structure = config.get("structure", {}) or {}
    return {
        "wall_pick_time": _norm_time(entry.get("wall_pick_time", DEFAULT_WALL_PICK_TIME)),
        "entry_time": _norm_time(entry.get("entry_time", DEFAULT_ENTRY_TIME)),
        "min_conditions_to_enter": int(entry.get("min_conditions_to_enter", 1)),
        "force_exit_time": _norm_time(exit_cfg.get("force_exit_time", DEFAULT_FORCE_EXIT_TIME)),
        "tp_inr": float(exit_cfg.get("tp_inr", 1000.0)),
        "sl_inr": float(exit_cfg.get("sl_inr", 2000.0)),
        "trail_arm_inr": float(exit_cfg.get("trail_arm_inr", 0.0)),
        "trail_lock_inr": float(exit_cfg.get("trail_lock_inr", 200.0)),
        "wall_breach_enabled": bool(exit_cfg.get("wall_breach_enabled", True)),
        "lots": int(structure.get("lots", 1)),
        "sell_offset_abs": int(structure.get("sell_offset_abs", 2)),
        "buy_offset_abs": int(structure.get("buy_offset_abs", 4)),
        "reference_capital": float(config["sizing"]["reference_capital"]),
    }


def run_backtest(df: pd.DataFrame, config: dict) -> dict:
    """Run the strategy over every trading day in [backtest_start, backtest_end]."""
    p = parse_config(config)
    bt_start = config["backtest_start"]
    bt_end = config["backtest_end"]
    capital = p["reference_capital"]

    trades: List[OiWallTrade] = []
    running_equity = capital

    if df.empty:
        return {"trades": trades, "config": config}

    # df is already date-range filtered in load_filtered_options.
    # But keep a guard here so callers passing in raw frames still work.
    df = df[(df["_date"] >= bt_start) & (df["_date"] <= bt_end)]

    for date_str, day_df in df.groupby("_date", sort=True):
        try:
            d = _date.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue
        ctx = DayContext(
            date=d,
            min_conditions_to_enter=p["min_conditions_to_enter"],
            wall_pick_time=p["wall_pick_time"],
            entry_time=p["entry_time"],
            force_exit_time=p["force_exit_time"],
            lots=p["lots"],
            sell_offset_abs=p["sell_offset_abs"],
            buy_offset_abs=p["buy_offset_abs"],
            tp_inr=p["tp_inr"],
            sl_inr=p["sl_inr"],
            trail_arm_inr=p["trail_arm_inr"],
            trail_lock_inr=p["trail_lock_inr"],
            wall_breach_enabled=p["wall_breach_enabled"],
        )
        trade = run_one_day(day_df, ctx)
        running_equity += trade.pnl_inr
        trade.return_pct = trade.pnl_inr / capital if capital else 0.0
        trade.running_equity_inr = running_equity
        trades.append(trade)

    return {"trades": trades, "config": config}


# --------------------------------------------------------------------------- #
#  Reporting                                                                  #
# --------------------------------------------------------------------------- #

def _is_nan(x) -> bool:
    try:
        return x != x
    except Exception:
        return False


def build_equity_curve(
    trades: List[OiWallTrade], starting_capital: float,
) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=["date", "equity_inr", "drawdown_inr",
                                     "drawdown_pct", "in_trade"])
    rows = []
    peak = starting_capital
    for t in trades:
        equity = t.running_equity_inr if not _is_nan(t.running_equity_inr) else starting_capital
        peak = max(peak, equity)
        dd_inr = peak - equity
        dd_pct = dd_inr / peak if peak else 0.0
        rows.append({
            "date": t.date,
            "equity_inr": equity,
            "drawdown_inr": dd_inr,
            "drawdown_pct": dd_pct,
            "in_trade": t.skip_reason is None,
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


def summarize_metrics(trades: List[OiWallTrade], starting_capital: float) -> dict:
    placed = [t for t in trades if t.skip_reason is None]
    pnls = [t.pnl_inr for t in placed]
    wins = [t for t in placed if t.pnl_inr > 0]
    losses = [t for t in placed if t.pnl_inr < 0]

    equity_curve = build_equity_curve(trades, starting_capital)
    if not equity_curve.empty:
        max_dd_inr = float(equity_curve["drawdown_inr"].max())
        max_dd_pct = float(equity_curve["drawdown_pct"].max())
    else:
        max_dd_inr = 0.0
        max_dd_pct = 0.0

    # Breakdown of placed trades by how they exited (TP / SL / TSL / TIME / BREACH).
    exit_reason_counts = _count_by(placed, lambda t: t.exit_reason)
    exit_reason_pnl: Dict[str, float] = {}
    for t in placed:
        exit_reason_pnl[t.exit_reason] = exit_reason_pnl.get(t.exit_reason, 0.0) + t.pnl_inr

    return {
        "total_days_processed": len(trades),
        "trades_placed": len(placed),
        "trades_skipped": len(trades) - len(placed),
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
        "ce_trades": sum(1 for t in placed if t.wall_option_type == "CE"),
        "pe_trades": sum(1 for t in placed if t.wall_option_type == "PE"),
        "exit_reason_counts": exit_reason_counts,
        "exit_reason_pnl": exit_reason_pnl,
        "skip_reason_counts": _count_by(
            [t for t in trades if t.skip_reason], lambda t: t.skip_reason
        ),
    }


def trades_to_dataframe(trades: List[OiWallTrade]) -> pd.DataFrame:
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


def write_trades_csv(trades: List[OiWallTrade], path) -> None:
    trades_to_dataframe(trades).to_csv(path, index=False)


def write_equity_csv(trades: List[OiWallTrade], capital: float, path) -> None:
    build_equity_curve(trades, capital).to_csv(path, index=False)


def print_summary(s: dict) -> None:
    lines = [
        f"Total days processed: {s['total_days_processed']}",
        f"Trades placed: {s['trades_placed']}  (CE={s['ce_trades']}, PE={s['pe_trades']})",
        f"Trades skipped: {s['trades_skipped']}",
        f"Wins / Losses: {s['wins']} / {s['losses']}  ({s['win_rate']*100:.2f}% win-rate)",
        f"Mean P&L: Rs {s['mean_pnl_inr']:.2f}    Median: Rs {s['median_pnl_inr']:.2f}",
        f"Total P&L: Rs {s['total_pnl_inr']:.2f}",
        f"Total return on reference capital: {s['total_return_pct']*100:.2f}%",
        f"Max drawdown: Rs {s['max_drawdown_inr']:.2f}  ({s['max_drawdown_pct']*100:.2f}%)",
        f"Max consecutive losing days: {s['max_consecutive_losses']}",
        f"Best day: Rs {s['best_trade_inr']:.2f}    Worst day: Rs {s['worst_trade_inr']:.2f}",
        "Exit breakdown (count / total P&L):  " + "  ".join(
            f"{('EOD' if r == 'TIME' else r)}={s['exit_reason_counts'].get(r, 0)}"
            f"/Rs {s['exit_reason_pnl'].get(r, 0.0):.0f}"
            for r in ("TP", "SL", "TSL", "TIME", "BREACH")
            if s['exit_reason_counts'].get(r, 0)
        ),
        f"Skip reasons: {s['skip_reason_counts']}",
    ]
    for line in lines:
        print(line)


def run(config: dict, options_path: str, output_dir: str) -> dict:
    """Top-level entrypoint. Loads parquet, runs backtest, writes CSVs, prints summary."""
    expiry_cfg = config.get("expiry", {}) or {}
    entry_cfg = config.get("entry", {}) or {}
    exit_cfg = config.get("exit", {}) or {}
    df = load_filtered_options(
        options_path,
        start_date=config["backtest_start"],
        end_date=config["backtest_end"],
        expiry_type=str(expiry_cfg.get("expiry_type", "MONTH")).upper(),
        expiry_code=int(expiry_cfg.get("expiry_code", 1)),
        wall_pick_time=_norm_time(entry_cfg.get("wall_pick_time", DEFAULT_WALL_PICK_TIME)),
        force_exit_time=_norm_time(exit_cfg.get("force_exit_time", DEFAULT_FORCE_EXIT_TIME)),
    )
    result = run_backtest(df, config)
    trades = result["trades"]
    capital = float(config["sizing"]["reference_capital"])
    summary = summarize_metrics(trades, capital)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    start_str = config["backtest_start"]
    end_str = config["backtest_end"]
    trades_path = out / f"oi_wall_trades_{start_str}_{end_str}.csv"
    equity_path = out / f"oi_wall_equity_{start_str}_{end_str}.csv"
    write_trades_csv(trades, trades_path)
    write_equity_csv(trades, capital, equity_path)
    print_summary(summary)
    return {
        "trades": trades, "summary": summary,
        "trades_csv": str(trades_path),
        "equity_csv": str(equity_path),
    }
