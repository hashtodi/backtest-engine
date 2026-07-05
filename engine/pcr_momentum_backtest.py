"""
PCR Momentum Backtest Engine - NIFTY PCR-shift-driven vertical credit spread.

Strategy:
  Daily entry. At pcr_first_time (default 09:20) and pcr_second_time
  (default 10:00) IST, compute the Put/Call Ratio (PCR) over the 10 OTM
  strikes each side of ATM:
      PCR = sum(PE OI, offsets -10..-1) / sum(CE OI, offsets +1..+10)

  Direction rule (BOTH must hold; strict inequalities):
      pcr_first > pcr_threshold  AND  (pcr_second - pcr_first) >= min_pcr_delta
          -> PE side: SELL ATM-N PE, BUY ATM-M PE  (bull-put credit spread)
      pcr_first < pcr_threshold  AND  (pcr_first - pcr_second) >= min_pcr_delta
          -> CE side: SELL ATM+N CE, BUY ATM+M CE  (bear-call credit spread)
      else -> skip day.

  Signal vs fill bars (no look-ahead):
    SIGNAL bar  = pcr_second_time (default 10:00). PCR is re-computed and
                  the direction decided here. ATM and the two leg STRIKES
                  are also resolved on this bar.
    FILL bar    = pcr_second_time + 1 minute (T+1). The spread legs
                  (resolved by absolute strike at the signal bar) are
                  FILLED at the T+1 bar's OPEN. No look-ahead -- strike
                  choice never uses fill-bar information.

  Spread structure (anchored to SIGNAL-bar ATM):
    PE side -> SELL ATM-2 PE, BUY ATM-6 PE   (bull-put  credit spread)
    CE side -> SELL ATM+2 CE, BUY ATM+6 CE   (bear-call credit spread)
  Both structures collect net premium at entry (sell strike is closer to
  ATM and therefore richer than the buy strike).

  Between (entry_fill_time, force_exit_time) scan each minute bar's CLOSE
  for two exit signals:
    SL : live spread P&L <= -sl_inr * lots   (per-lot SL)
    TP : live spread P&L >=  tp_inr * lots   (per-lot TP)
  Priority SL > TP within the same close. Signal-based exits detect at
  T's CLOSE and fill at the NEXT minute's OPEN (T+1 open) - symmetric
  with the entry. No look-ahead.

  If no signal hits, exit both legs at force_exit_time (default 15:00)
  CLOSE with reason "TIME" (planned deadline, no T+1 shift). Set TP/SL
  to 0 to disable. P&L scales with lots.
"""

import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

DEFAULT_PCR_FIRST_TIME = "09:20:00"
DEFAULT_PCR_SECOND_TIME = "10:00:00"
DEFAULT_FORCE_EXIT_TIME = "15:00:00"
DEFAULT_PCR_THRESHOLD = 1.0
DEFAULT_MIN_PCR_DELTA = 0.01
DEFAULT_PCR_STRIKES_EACH_SIDE = 10

REQUIRED_COLS = [
    "datetime", "option_type", "expiry_type", "expiry_code",
    "strike_offset", "moneyness", "strike", "spot", "open", "close", "oi",
    "underlying",
]


# --------------------------------------------------------------------------- #
#  Dataclasses                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class PcrMomentumTrade:
    date: str

    # PCR signal snapshot
    pcr_first: float            # PCR at first snapshot (e.g. 09:20)
    pcr_second: float           # PCR at second snapshot (e.g. 10:00)
    pcr_delta: float            # pcr_second - pcr_first
    ce_oi_sum_first: float
    pe_oi_sum_first: float
    ce_oi_sum_second: float
    pe_oi_sum_second: float
    side: str                   # "CE" | "PE" | ""  (chosen wall side)
    cond_side: bool             # PCR-first > or < threshold
    cond_momentum: bool         # |delta| >= min_pcr_delta in correct direction

    # Spread context
    atm_strike: float
    spot_at_entry: float
    spot_at_exit: float

    # Execution
    signal_time: str            # bar where the side is decided (pcr_second_time)
    entry_time: str             # bar where spread was filled (signal + 1 min)
    exit_signal_time: str
    exit_time: str
    exit_reason: str            # "TIME" | "TP" | "SL" | "" (when skipped)
    net_credit_pts: float
    net_credit_inr: float
    pnl_pts: float
    pnl_inr: float
    return_pct: float
    running_equity_inr: float

    skip_reason: Optional[str]
    legs: Dict[str, LegFill] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
#  PCR helpers                                                                #
# --------------------------------------------------------------------------- #

def compute_pcr(
    slice_df: pd.DataFrame,
    n_each_side: int = DEFAULT_PCR_STRIKES_EACH_SIDE,
) -> Optional[Tuple[float, float, float]]:
    """Sum OI across the 10 OTM strikes each side and return
    (pcr, ce_oi_sum, pe_oi_sum). PCR = pe_oi_sum / ce_oi_sum.

    Eligible CE rows: strike_offset in [1..n_each_side].
    Eligible PE rows: strike_offset in [-n_each_side..-1].
    Returns None if no eligible CE rows (would divide by zero) or no eligible
    PE rows (numerator missing).
    """
    if slice_df.empty:
        return None
    ot = slice_df["option_type"].to_numpy()
    off = slice_df["strike_offset"].to_numpy()
    ce_mask = (ot == "CE") & (off >= 1) & (off <= n_each_side)
    pe_mask = (ot == "PE") & (off >= -n_each_side) & (off <= -1)
    ce_rows = slice_df[ce_mask]
    pe_rows = slice_df[pe_mask]
    if ce_rows.empty or pe_rows.empty:
        return None
    ce_oi = float(ce_rows["oi"].sum())
    pe_oi = float(pe_rows["oi"].sum())
    if ce_oi <= 0:
        return None
    return pe_oi / ce_oi, ce_oi, pe_oi


def pcr_signal(
    pcr_first: float,
    pcr_second: float,
    pcr_threshold: float = DEFAULT_PCR_THRESHOLD,
    min_pcr_delta: float = DEFAULT_MIN_PCR_DELTA,
) -> Tuple[Optional[str], bool, bool]:
    """Decide the trade side.

    Returns (side, cond_side, cond_momentum):
      side is "PE", "CE", or None (skip).
      cond_side -- is pcr_first on a strict side of the threshold?
      cond_momentum -- did PCR move >= min_pcr_delta in the qualifying direction?
    """
    delta = pcr_second - pcr_first
    if pcr_first > pcr_threshold:
        cond_side = True
        cond_momentum = delta >= min_pcr_delta
        side = "PE" if cond_momentum else None
    elif pcr_first < pcr_threshold:
        cond_side = True
        cond_momentum = (-delta) >= min_pcr_delta
        side = "CE" if cond_momentum else None
    else:
        cond_side = False
        cond_momentum = False
        side = None
    return side, cond_side, cond_momentum


# --------------------------------------------------------------------------- #
#  Per-day driver                                                             #
# --------------------------------------------------------------------------- #

@dataclass
class DayContext:
    date: _date
    pcr_first_time: str = DEFAULT_PCR_FIRST_TIME
    pcr_second_time: str = DEFAULT_PCR_SECOND_TIME
    force_exit_time: str = DEFAULT_FORCE_EXIT_TIME
    pcr_threshold: float = DEFAULT_PCR_THRESHOLD
    min_pcr_delta: float = DEFAULT_MIN_PCR_DELTA
    pcr_strikes_each_side: int = DEFAULT_PCR_STRIKES_EACH_SIDE
    lots: int = 1
    sell_offset_abs: int = 2
    buy_offset_abs: int = 6
    tp_inr: float = 1200.0
    sl_inr: float = 2000.0
    lot_size: int = LOT_SIZE_NIFTY


def _empty_trade(ctx: DayContext, skip_reason: str) -> PcrMomentumTrade:
    nan = float("nan")
    return PcrMomentumTrade(
        date=ctx.date.isoformat(),
        pcr_first=nan, pcr_second=nan, pcr_delta=nan,
        ce_oi_sum_first=nan, pe_oi_sum_first=nan,
        ce_oi_sum_second=nan, pe_oi_sum_second=nan,
        side="", cond_side=False, cond_momentum=False,
        atm_strike=nan, spot_at_entry=nan, spot_at_exit=nan,
        signal_time="", entry_time="", exit_signal_time="", exit_time="",
        exit_reason="",
        net_credit_pts=0.0, net_credit_inr=0.0,
        pnl_pts=0.0, pnl_inr=0.0,
        return_pct=0.0, running_equity_inr=0.0,
        skip_reason=skip_reason, legs={},
    )


def run_one_day(day_df: pd.DataFrame, ctx: DayContext) -> PcrMomentumTrade:
    """Run the PCR Momentum strategy for a single trading day."""
    if day_df.empty:
        return _empty_trade(ctx, "no_data_on_entry_day")

    time_col = day_df["_time"] if "_time" in day_df.columns else \
        day_df["datetime"].str.slice(11, 19)

    entry_fill_time = _plus_one_minute(ctx.pcr_second_time)

    slice_first = day_df[time_col == ctx.pcr_first_time]
    slice_second = day_df[time_col == ctx.pcr_second_time]
    slice_fill = day_df[time_col == entry_fill_time]
    slice_forced = day_df[time_col == ctx.force_exit_time]

    if slice_first.empty:
        return _empty_trade(ctx, "no_pcr_first_bar")
    if slice_second.empty:
        return _empty_trade(ctx, "no_pcr_second_bar")

    n_strikes = int(ctx.pcr_strikes_each_side)
    pcr_first_res = compute_pcr(slice_first, n_each_side=n_strikes)
    pcr_second_res = compute_pcr(slice_second, n_each_side=n_strikes)
    if pcr_first_res is None:
        return _empty_trade(ctx, "pcr_first_uncomputable")
    if pcr_second_res is None:
        return _empty_trade(ctx, "pcr_second_uncomputable")
    pcr_first, ce_oi_1, pe_oi_1 = pcr_first_res
    pcr_second, ce_oi_2, pe_oi_2 = pcr_second_res

    side, cond_side, cond_momentum = pcr_signal(
        pcr_first, pcr_second,
        pcr_threshold=ctx.pcr_threshold,
        min_pcr_delta=ctx.min_pcr_delta,
    )

    trade = _empty_trade(ctx, skip_reason=None)
    trade.pcr_first = pcr_first
    trade.pcr_second = pcr_second
    trade.pcr_delta = pcr_second - pcr_first
    trade.ce_oi_sum_first = ce_oi_1
    trade.pe_oi_sum_first = pe_oi_1
    trade.ce_oi_sum_second = ce_oi_2
    trade.pe_oi_sum_second = pe_oi_2
    trade.cond_side = cond_side
    trade.cond_momentum = cond_momentum

    if side is None:
        if not cond_side:
            trade.skip_reason = "pcr_at_threshold"
        else:
            trade.skip_reason = "momentum_insufficient"
        return trade

    trade.side = side

    # Strike selection at the SIGNAL bar (no look-ahead).
    atm = atm_strike_from(slice_second)
    if atm is None:
        trade.skip_reason = "atm_missing_at_signal"
        return trade
    atm_strike, spot = atm
    trade.atm_strike = atm_strike
    trade.spot_at_entry = spot

    sell_abs = int(ctx.sell_offset_abs)
    buy_abs = int(ctx.buy_offset_abs)
    if side == "CE":
        sell_off, buy_off = sell_abs, buy_abs
    else:
        sell_off, buy_off = -sell_abs, -buy_abs

    sell_row_signal = lookup_by_offset(slice_second, side, sell_off)
    buy_row_signal = lookup_by_offset(slice_second, side, buy_off)
    if sell_row_signal is None or buy_row_signal is None:
        trade.skip_reason = "spread_leg_missing_at_signal"
        return trade
    sell_strike = float(sell_row_signal["strike"])
    buy_strike = float(buy_row_signal["strike"])

    if slice_fill.empty:
        trade.skip_reason = "no_entry_fill_bar"
        return trade
    sell_row_fill = lookup_by_strike(slice_fill, side, sell_strike)
    buy_row_fill = lookup_by_strike(slice_fill, side, buy_strike)
    if sell_row_fill is None or buy_row_fill is None:
        trade.skip_reason = "spread_leg_missing_at_fill"
        return trade
    sell_entry = float(sell_row_fill["open"])
    buy_entry = float(buy_row_fill["open"])

    # Intraday exit scan: SL > TP within the same minute close.
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
    if tp_active or sl_active:
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
            sr = lookup_by_strike(slice_t, side, sell_strike)
            br = lookup_by_strike(slice_t, side, buy_strike)
            if sr is None or br is None:
                continue
            s_t = float(sr["close"])
            b_t = float(br["close"])
            spot_t = float(sr["spot"])
            live_pts = (sell_entry + b_t) - (s_t + buy_entry)
            live_inr = live_pts * ctx.lot_size * ctx.lots
            tp_hit = tp_active and live_inr >= tp_threshold_inr
            sl_hit = sl_active and live_inr <= -sl_threshold_inr
            if not (sl_hit or tp_hit):
                continue
            reason = "SL" if sl_hit else "TP"

            fill_t = _plus_one_minute(t)
            slice_exit = day_df[time_col_day == fill_t]
            sr_exit = lookup_by_strike(slice_exit, side, sell_strike)
            br_exit = lookup_by_strike(slice_exit, side, buy_strike)
            if sr_exit is None or br_exit is None:
                continue
            sell_exit = float(sr_exit["open"])
            buy_exit = float(br_exit["open"])
            spot_exit = spot_t
            exit_signal_time_str = t[:5]
            exit_time_str = fill_t[:5]
            exit_reason = reason
            break

    if sell_exit is None:
        if slice_forced.empty:
            trade.skip_reason = "no_force_exit_bar"
            return trade
        sell_exit_row = lookup_by_strike(slice_forced, side, sell_strike)
        buy_exit_row = lookup_by_strike(slice_forced, side, buy_strike)
        if sell_exit_row is None or buy_exit_row is None:
            trade.skip_reason = "exit_leg_missing_at_force_exit"
            return trade
        sell_exit = float(sell_exit_row["close"])
        buy_exit = float(buy_exit_row["close"])
        spot_exit = float(sell_exit_row["spot"])
        exit_signal_time_str = ctx.force_exit_time[:5]
        exit_time_str = ctx.force_exit_time[:5]

    contracts = ctx.lot_size * ctx.lots
    net_credit_pts = sell_entry - buy_entry
    pnl_pts = (sell_entry + buy_exit) - (sell_exit + buy_entry)
    pnl_inr = pnl_pts * contracts

    trade.signal_time = ctx.pcr_second_time[:5]
    trade.entry_time = entry_fill_time[:5]
    trade.spot_at_exit = spot_exit
    trade.exit_signal_time = exit_signal_time_str
    trade.exit_time = exit_time_str
    trade.exit_reason = exit_reason
    trade.net_credit_pts = net_credit_pts
    trade.net_credit_inr = net_credit_pts * contracts
    trade.pnl_pts = pnl_pts
    trade.pnl_inr = pnl_inr

    sell_key = f"{side.lower()}_short"
    buy_key = f"{side.lower()}_long"
    trade.legs = {
        sell_key: LegFill(
            option_type=side, side="SELL", lots=ctx.lots,
            strike_offset=sell_off, strike=sell_strike,
            entry_price=sell_entry, exit_price=sell_exit,
        ),
        buy_key: LegFill(
            option_type=side, side="BUY", lots=ctx.lots,
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
    expiry_type: str = "WEEK", expiry_code: int = 1,
    pcr_first_time: str = DEFAULT_PCR_FIRST_TIME,
    force_exit_time: str = DEFAULT_FORCE_EXIT_TIME,
) -> pd.DataFrame:
    """Predicate-pushdown load: NIFTY <expiry_type> code=<expiry_code>, only
    minute bars in [pcr_first_time, force_exit_time]."""
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

    df = df[(df["datetime"].str.slice(0, 10) >= start_date)
            & (df["datetime"].str.slice(0, 10) <= end_date)]

    lower = _norm_time(pcr_first_time)
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
    entry = config.get("entry", {}) or {}
    exit_cfg = config.get("exit", {}) or {}
    structure = config.get("structure", {}) or {}
    return {
        "pcr_first_time": _norm_time(entry.get("pcr_first_time", DEFAULT_PCR_FIRST_TIME)),
        "pcr_second_time": _norm_time(entry.get("pcr_second_time", DEFAULT_PCR_SECOND_TIME)),
        "pcr_threshold": float(entry.get("pcr_threshold", DEFAULT_PCR_THRESHOLD)),
        "min_pcr_delta": float(entry.get("min_pcr_delta", DEFAULT_MIN_PCR_DELTA)),
        "pcr_strikes_each_side": int(entry.get("pcr_strikes_each_side", DEFAULT_PCR_STRIKES_EACH_SIDE)),
        "force_exit_time": _norm_time(exit_cfg.get("force_exit_time", DEFAULT_FORCE_EXIT_TIME)),
        "tp_inr": float(exit_cfg.get("tp_inr", 1200.0)),
        "sl_inr": float(exit_cfg.get("sl_inr", 2000.0)),
        "lots": int(structure.get("lots", 1)),
        "sell_offset_abs": int(structure.get("sell_offset_abs", 2)),
        "buy_offset_abs": int(structure.get("buy_offset_abs", 6)),
        "reference_capital": float(config["sizing"]["reference_capital"]),
    }


def run_backtest(df: pd.DataFrame, config: dict) -> dict:
    p = parse_config(config)
    bt_start = config["backtest_start"]
    bt_end = config["backtest_end"]
    capital = p["reference_capital"]

    trades: List[PcrMomentumTrade] = []
    running_equity = capital

    if df.empty:
        return {"trades": trades, "config": config}

    df = df[(df["_date"] >= bt_start) & (df["_date"] <= bt_end)]

    for date_str, day_df in df.groupby("_date", sort=True):
        try:
            d = _date.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue
        ctx = DayContext(
            date=d,
            pcr_first_time=p["pcr_first_time"],
            pcr_second_time=p["pcr_second_time"],
            force_exit_time=p["force_exit_time"],
            pcr_threshold=p["pcr_threshold"],
            min_pcr_delta=p["min_pcr_delta"],
            pcr_strikes_each_side=p["pcr_strikes_each_side"],
            lots=p["lots"],
            sell_offset_abs=p["sell_offset_abs"],
            buy_offset_abs=p["buy_offset_abs"],
            tp_inr=p["tp_inr"],
            sl_inr=p["sl_inr"],
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

def _count_by(items, keyfn):
    counts: Dict[str, int] = {}
    for it in items:
        k = keyfn(it)
        counts[k] = counts.get(k, 0) + 1
    return counts


def summarize_metrics(trades: List[PcrMomentumTrade], starting_capital: float) -> dict:
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
        "ce_trades": sum(1 for t in placed if t.side == "CE"),
        "pe_trades": sum(1 for t in placed if t.side == "PE"),
        "skip_reason_counts": _count_by(
            [t for t in trades if t.skip_reason], lambda t: t.skip_reason
        ),
    }


def trades_to_dataframe(trades: List[PcrMomentumTrade]) -> pd.DataFrame:
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


def write_trades_csv(trades: List[PcrMomentumTrade], path) -> None:
    trades_to_dataframe(trades).to_csv(path, index=False)


def write_equity_csv(trades: List[PcrMomentumTrade], capital: float, path) -> None:
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
        expiry_type=str(expiry_cfg.get("expiry_type", "WEEK")).upper(),
        expiry_code=int(expiry_cfg.get("expiry_code", 1)),
        pcr_first_time=_norm_time(entry_cfg.get("pcr_first_time", DEFAULT_PCR_FIRST_TIME)),
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
    trades_path = out / f"pcr_momentum_trades_{start_str}_{end_str}.csv"
    equity_path = out / f"pcr_momentum_equity_{start_str}_{end_str}.csv"
    write_trades_csv(trades, trades_path)
    write_equity_csv(trades, capital, equity_path)
    print_summary(summary)
    return {
        "trades": trades, "summary": summary,
        "trades_csv": str(trades_path),
        "equity_csv": str(equity_path),
    }
