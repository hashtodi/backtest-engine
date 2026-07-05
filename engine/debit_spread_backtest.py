"""
Debit Spread Backtest Engine — 1-3-2 Broken-Wing Condor on NIFTY weeklies.

Strategy:
  Calendar-driven. Two trading days before every NIFTY weekly expiry, at
  11:00 AM, enter a 6-leg combined CE+PE 1-3-2 ratio structure. Exit at
  1.5x net debit (intra-day 1-min check) or 15:25 close on expiry day.
  No stop loss. See docs/superpowers/specs/2026-05-08-debit-spread-design.md.
"""

import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date as _date, datetime as _datetime, time as _time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

LOT_SIZE_NIFTY = 65  # Mirrors config.LOT_SIZE['NIFTY']; pinned for unit tests.


@dataclass
class LegSpec:
    """A single leg's static spec (independent of any specific trade)."""
    option_type: str       # "CE" | "PE"
    side: str              # "BUY" | "SELL"
    lots: int
    strike_offset: int


@dataclass
class LegFill:
    """Resolved leg at a specific trade: actual strike + entry/exit prices."""
    option_type: str
    side: str
    lots: int
    strike_offset: int
    strike: float
    entry_price: float
    exit_price: Optional[float] = None


@dataclass
class DebitSpreadTrade:
    expiry_date: str
    entry_date: str
    entry_time: str
    atm_strike: float
    spot_at_entry: float

    net_debit_pts: float
    net_debit_inr: float
    tp_target_inr: float

    exit_time: str
    exit_reason: str          # "TP" | "EXPIRY" | "data_gap_force_exit"
    pnl_pts: float
    pnl_inr: float
    return_pct: float
    running_equity_inr: float

    skip_reason: Optional[str]
    legs: Dict[str, LegFill] = field(default_factory=dict)


def compute_entry_date(
    expiry_date: _date,
    trading_days: List[_date],
    days_before: int,
) -> _date:
    """Return the date that is `days_before` trading days before `expiry_date`.

    `trading_days` must contain `expiry_date` itself. We walk back through the
    sorted trading-day list. Holiday-shifted expiries are handled because the
    shifted date is what's passed in; we just need at least `days_before`
    earlier trading days available.
    """
    sorted_days = sorted(set(trading_days))
    if expiry_date not in sorted_days:
        raise ValueError(f"Expiry {expiry_date} not in trading_days list")
    idx = sorted_days.index(expiry_date)
    if idx - days_before < 0:
        raise ValueError(
            f"Not enough trading days before {expiry_date} "
            f"(need {days_before}, have {idx})"
        )
    return sorted_days[idx - days_before]


LEG_KEY_ORDER = [
    "ce_itm", "ce_short", "ce_far",
    "pe_itm", "pe_short", "pe_far",
]

DEFAULT_LEG_SPECS: Dict[str, LegSpec] = {
    "ce_itm":   LegSpec(option_type="CE", side="BUY",  lots=1, strike_offset=-1),
    "ce_short": LegSpec(option_type="CE", side="SELL", lots=3, strike_offset=4),
    "ce_far":   LegSpec(option_type="CE", side="BUY",  lots=2, strike_offset=5),
    "pe_itm":   LegSpec(option_type="PE", side="BUY",  lots=1, strike_offset=1),
    "pe_short": LegSpec(option_type="PE", side="SELL", lots=3, strike_offset=-4),
    "pe_far":   LegSpec(option_type="PE", side="BUY",  lots=2, strike_offset=-5),
}


def _leg_signed_value(leg: LegFill, price: float) -> float:
    """+lots*price for BUY, -lots*price for SELL."""
    sign = 1 if leg.side == "BUY" else -1
    return sign * leg.lots * price


def compute_entry_economics(
    legs: Dict[str, LegFill],
    tp_multiple: float,
    lot_size: int = LOT_SIZE_NIFTY,
) -> Tuple[float, float, float]:
    """Return (net_debit_pts, net_debit_inr, tp_target_inr).

    With sign=+1 for BUY and sign=-1 for SELL, sum(sign*lots*price) equals
    (total paid for longs) - (total received from shorts) = net debit directly.
    Positive = debit (cash out); negative = credit (cash in).
    tp_target = max(0, net_debit_inr) * tp_multiple.
    """
    net_debit_pts = sum(_leg_signed_value(leg, leg.entry_price) for leg in legs.values())
    net_debit_inr = net_debit_pts * lot_size
    tp_target_inr = max(0.0, net_debit_inr) * tp_multiple
    return net_debit_pts, net_debit_inr, tp_target_inr


def compute_mtm_inr(
    legs: Dict[str, LegFill],
    current_prices: Dict[str, float],
    net_debit_pts: float,
    lot_size: int = LOT_SIZE_NIFTY,
) -> float:
    """Unrealized P&L vs entry given current per-leg prices.

    With sign=+1 for BUY and -1 for SELL: signed_sum(prices) is the position
    value at those prices. Since net_debit_pts == signed_sum(entry_prices),
    we have: mtm_pts = signed_now - net_debit_pts.
    """
    signed_now = sum(_leg_signed_value(leg, current_prices[k]) for k, leg in legs.items())
    mtm_pts = signed_now - net_debit_pts
    return mtm_pts * lot_size


def scan_for_tp_exit(
    legs: Dict[str, LegFill],
    bars: List[Dict],
    net_debit_pts: float,
    tp_target_inr: float,
    lot_size: int = LOT_SIZE_NIFTY,
) -> Optional[Tuple[pd.Timestamp, Dict[str, float], float]]:
    """Walk the bar series; return (timestamp, leg_prices, mtm_inr) when TP fires.

    Credit case (tp_target == 0): exit at first bar with mtm STRICTLY > 0.
    Debit case:                  exit at first bar with mtm >= tp_target.
    Returns None if no bar satisfies the trigger.
    """
    is_credit_case = tp_target_inr == 0.0
    for bar in bars:
        prices = bar["prices"]
        if any(k not in prices for k in legs):
            continue
        mtm = compute_mtm_inr(legs, prices, net_debit_pts, lot_size)
        if is_credit_case:
            if mtm > 0:
                return bar["datetime"], dict(prices), mtm
        else:
            if mtm >= tp_target_inr:
                return bar["datetime"], dict(prices), mtm
    return None


def build_bar_stream(
    df: pd.DataFrame,
    legs: Dict[str, LegFill],
    max_gap_minutes: int = 30,
):
    """Yield per-minute bars with carry-forward (≤ max_gap_minutes) handling.

    Each yielded dict has:
        datetime: pd.Timestamp
        prices:   Dict[leg_key, float]  (bar 'close' for each leg)
        force_exit: bool                (True iff any leg's gap > max_gap_minutes)

    On force_exit we yield the LAST FULLY-OBSERVED bar (its complete prices),
    not the trigger bar — that's the last MTM we can compute reliably.
    Iteration stops after emitting force_exit.
    """
    if df.empty:
        return

    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    leg_lookup = {(l.option_type, int(l.strike_offset)): k for k, l in legs.items()}
    df = df.sort_values("datetime")

    last_seen: Dict[str, Tuple[pd.Timestamp, float]] = {}
    last_full_bar: Optional[Tuple[pd.Timestamp, Dict[str, float]]] = None

    for ts, grp in df.groupby("datetime"):
        for _, row in grp.iterrows():
            key = leg_lookup.get((row["option_type"], int(row["strike_offset"])))
            if key is None:
                continue
            last_seen[key] = (ts, float(row["close"]))

        prices: Dict[str, float] = {}
        force_exit = False
        for k in legs:
            if k not in last_seen:
                force_exit = True
                break
            seen_ts, seen_price = last_seen[k]
            gap_min = (ts - seen_ts).total_seconds() / 60.0
            if gap_min > max_gap_minutes:
                force_exit = True
                break
            prices[k] = seen_price

        if force_exit:
            if last_full_bar is None:
                return
            last_ts, last_prices = last_full_bar
            yield {"datetime": last_ts, "prices": dict(last_prices), "force_exit": True}
            return

        last_full_bar = (ts, dict(prices))
        yield {"datetime": ts, "prices": prices, "force_exit": False}


@dataclass
class WeekContext:
    expiry_date: _date
    entry_date: _date
    entry_time_str: str                       # "HH:MM"
    expiry_squareoff_time_str: str            # "HH:MM"
    tp_multiple: float
    data_gap_force_exit_minutes: int
    leg_specs: Dict[str, LegSpec]
    lot_size: int = LOT_SIZE_NIFTY
    tp_target_inr_fixed: Optional[float] = None  # if set, overrides tp_multiple


def _make_skip_trade(ctx: "WeekContext", reason: str) -> "DebitSpreadTrade":
    return DebitSpreadTrade(
        expiry_date=ctx.expiry_date.isoformat(),
        entry_date=ctx.entry_date.isoformat(),
        entry_time=ctx.entry_time_str,
        atm_strike=float("nan"),
        spot_at_entry=float("nan"),
        net_debit_pts=float("nan"),
        net_debit_inr=float("nan"),
        tp_target_inr=float("nan"),
        exit_time="",
        exit_reason="",
        pnl_pts=0.0,
        pnl_inr=0.0,
        return_pct=0.0,
        running_equity_inr=float("nan"),  # filled in by caller
        skip_reason=reason,
        legs={},
    )


def _entry_slice(df: pd.DataFrame, ctx: "WeekContext") -> pd.DataFrame:
    target_ts = f"{ctx.entry_date.isoformat()}T{ctx.entry_time_str}:00+05:30"
    # CRITICAL: filter expiry_type=='WEEK' to avoid mixing weekly and monthly
    # contracts that can both have expiry_code==1 on the same minute.
    return df[
        (df["datetime"] == target_ts)
        & (df["expiry_code"] == 1)
        & (df["expiry_type"] == "WEEK")
    ]


def _holding_slice(df: pd.DataFrame, ctx: "WeekContext", legs: Dict[str, LegFill]) -> pd.DataFrame:
    """All bars after entry up through expiry squareoff timestamp, restricted
    to the locked strikes. Vectorized for performance.
    """
    entry_ts = f"{ctx.entry_date.isoformat()}T{ctx.entry_time_str}:00+05:30"
    expiry_ts = f"{ctx.expiry_date.isoformat()}T{ctx.expiry_squareoff_time_str}:00+05:30"

    sub = df[
        (df["datetime"] > entry_ts)
        & (df["datetime"] <= expiry_ts)
        & (df["expiry_code"] == 1)
        & (df["expiry_type"] == "WEEK")
    ]
    if sub.empty:
        return sub

    locked_pairs = {(l.option_type, int(l.strike_offset)) for l in legs.values()}
    pairs = list(zip(sub["option_type"].astype(str), sub["strike_offset"].astype(int)))
    mask = [p in locked_pairs for p in pairs]
    return sub[mask].sort_values("datetime")


def _expiry_squareoff_prices(
    holding_df: pd.DataFrame,
    ctx: "WeekContext",
    legs: Dict[str, LegFill],
) -> Optional[Tuple[pd.Timestamp, Dict[str, float]]]:
    """Find the latest available bar on expiry_date at-or-before expiry_squareoff_time
    that has ALL six legs. Return (ts, leg_prices) or None."""
    deadline_ts = pd.Timestamp(
        f"{ctx.expiry_date.isoformat()}T{ctx.expiry_squareoff_time_str}:00+05:30"
    )
    expiry_bars = holding_df[
        (pd.to_datetime(holding_df["datetime"]) <= deadline_ts)
        & (pd.to_datetime(holding_df["datetime"]).dt.date == ctx.expiry_date)
    ].sort_values("datetime", ascending=False)

    leg_lookup = {(l.option_type, int(l.strike_offset)): k for k, l in legs.items()}
    for ts, grp in expiry_bars.groupby("datetime", sort=False):
        prices = {}
        for _, row in grp.iterrows():
            k = leg_lookup.get((row["option_type"], int(row["strike_offset"])))
            if k:
                prices[k] = float(row["close"])
        if set(prices.keys()) == set(legs.keys()):
            return pd.Timestamp(ts), prices
    return None


def run_one_week(df: pd.DataFrame, ctx: "WeekContext") -> "DebitSpreadTrade":
    """Run the strategy for one expiry week against `df` (NIFTY options 1-min)."""
    entry_slice = _entry_slice(df, ctx)
    if entry_slice.empty:
        return _make_skip_trade(ctx, "no_entry_bar")

    atm_strike, spot_at_entry = resolve_atm_strike(entry_slice)
    if atm_strike is None:
        return _make_skip_trade(ctx, "no_atm_row")

    legs, missing = fetch_legs_at(entry_slice, ctx.leg_specs)
    if missing:
        return _make_skip_trade(ctx, f"missing_strike: {','.join(missing)}")

    net_pts, net_inr, tp = compute_entry_economics(
        legs, ctx.tp_multiple, lot_size=ctx.lot_size
    )
    # Fixed-rupee TP overrides the multiplier when configured.
    if ctx.tp_target_inr_fixed is not None:
        tp = float(ctx.tp_target_inr_fixed)

    holding_df = _holding_slice(df, ctx, legs)
    bars = list(build_bar_stream(
        holding_df, legs, max_gap_minutes=ctx.data_gap_force_exit_minutes
    ))

    expiry_ts = pd.Timestamp(
        f"{ctx.expiry_date.isoformat()}T{ctx.expiry_squareoff_time_str}:00+05:30"
    )
    pre_squareoff_bars = [
        b for b in bars
        if pd.Timestamp(b["datetime"]) < expiry_ts and not b.get("force_exit")
    ]

    tp_result = scan_for_tp_exit(
        legs, pre_squareoff_bars, net_pts, tp, lot_size=ctx.lot_size
    )

    if tp_result is not None:
        exit_ts, exit_prices, _mtm = tp_result
        exit_reason = "TP"
    else:
        force_bar = next((b for b in bars if b.get("force_exit")), None)
        if force_bar is not None:
            exit_ts = pd.Timestamp(force_bar["datetime"])
            exit_prices = force_bar["prices"]
            exit_reason = "data_gap_force_exit"
        else:
            squareoff = _expiry_squareoff_prices(holding_df, ctx, legs)
            if squareoff is None:
                return _make_skip_trade(ctx, "no_squareoff_bar_on_expiry")
            exit_ts, exit_prices = squareoff
            exit_reason = "EXPIRY"

    # Apply exit prices and compute PnL.  net_debit_pts == entry_cost_pts under
    # the +1 BUY / -1 SELL sign convention, so pnl = exit_signed - net_debit.
    for k, leg in legs.items():
        leg.exit_price = exit_prices[k]
    exit_value_pts = sum(_leg_signed_value(l, l.exit_price) for l in legs.values())
    pnl_pts = exit_value_pts - net_pts
    pnl_inr = pnl_pts * ctx.lot_size

    return DebitSpreadTrade(
        expiry_date=ctx.expiry_date.isoformat(),
        entry_date=ctx.entry_date.isoformat(),
        entry_time=ctx.entry_time_str,
        atm_strike=atm_strike,
        spot_at_entry=spot_at_entry,
        net_debit_pts=net_pts,
        net_debit_inr=net_inr,
        tp_target_inr=tp,
        exit_time=exit_ts.strftime("%H:%M"),
        exit_reason=exit_reason,
        pnl_pts=pnl_pts,
        pnl_inr=pnl_inr,
        return_pct=0.0,                    # filled in by caller after equity update
        running_equity_inr=0.0,            # filled in by caller
        skip_reason=None,
        legs=legs,
    )


def _is_nan(x) -> bool:
    try:
        return x != x  # NaN != NaN
    except Exception:
        return False


def build_equity_curve(
    trades: List[DebitSpreadTrade],
    starting_capital: float,
) -> pd.DataFrame:
    """One row per trade attempt (in chronological order):
        date, equity_inr, drawdown_inr, drawdown_pct, in_trade.
    """
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
            "date": t.expiry_date,
            "equity_inr": equity,
            "drawdown_inr": dd_inr,
            "drawdown_pct": dd_pct,
            "in_trade": t.skip_reason is None,
        })
    return pd.DataFrame(rows)


def compute_sharpe(
    returns: List[float],
    risk_free_rate: float,
    periods_per_year: int,
) -> float:
    """Annualized Sharpe = (mean_return - period_rfr) / stdev_return * sqrt(periods)."""
    if len(returns) < 2:
        return float("nan")
    period_rfr = risk_free_rate / periods_per_year
    mean_r = statistics.fmean(returns)
    sd = statistics.stdev(returns)
    if sd == 0:
        return float("nan")
    return (mean_r - period_rfr) / sd * math.sqrt(periods_per_year)


def compute_sortino(
    returns: List[float],
    risk_free_rate: float,
    periods_per_year: int,
) -> float:
    """Sortino uses downside deviation = stdev of min(0, r - period_rfr)."""
    if len(returns) < 2:
        return float("nan")
    period_rfr = risk_free_rate / periods_per_year
    mean_r = statistics.fmean(returns)
    downside = [min(0.0, r - period_rfr) for r in returns]
    if all(d == 0 for d in downside):
        return float("nan")
    sd_down = statistics.stdev(downside)
    if sd_down == 0:
        return float("nan")
    return (mean_r - period_rfr) / sd_down * math.sqrt(periods_per_year)


def max_consecutive_losses(pnls: List[float]) -> int:
    """Length of the longest run where pnl < 0 strictly."""
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


def summarize_metrics(
    trades: List[DebitSpreadTrade],
    starting_capital: float,
    risk_free_rate: float,
    periods_per_year: int,
) -> dict:
    """Compute every metric we report. Skipped trades contribute pnl=0,
    return=0 (preserves time-series length for ratios)."""
    pnls = [t.pnl_inr for t in trades]
    returns = [t.return_pct for t in trades]
    placed = [t for t in trades if t.skip_reason is None]
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
        "total_weeks_processed": len(trades),
        "trades_placed": len(placed),
        "trades_skipped": len(trades) - len(placed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(placed) if placed else 0.0,
        "loss_rate": len(losses) / len(placed) if placed else 0.0,
        "pct_profitable_weeks": len(wins) / len(placed) if placed else 0.0,
        "mean_pnl_inr": statistics.fmean(pnls) if pnls else 0.0,
        "median_pnl_inr": statistics.median(pnls) if pnls else 0.0,
        "total_pnl_inr": sum(pnls),
        "total_return_pct": sum(pnls) / starting_capital if starting_capital else 0.0,
        "max_drawdown_inr": max_dd_inr,
        "max_drawdown_pct": max_dd_pct,
        "max_consecutive_losses": max_consecutive_losses(pnls),
        "sharpe": compute_sharpe(returns, risk_free_rate, periods_per_year),
        "sortino": compute_sortino(returns, risk_free_rate, periods_per_year),
        "best_trade_inr": max(pnls) if pnls else 0.0,
        "worst_trade_inr": min(pnls) if pnls else 0.0,
        "exit_reason_counts": _count_by(placed, lambda t: t.exit_reason),
        "skip_reason_counts": _count_by(
            [t for t in trades if t.skip_reason], lambda t: t.skip_reason
        ),
    }


def write_trades_csv(trades: List[DebitSpreadTrade], path) -> None:
    df = trades_to_dataframe(trades)
    df.to_csv(path, index=False)


def write_equity_csv(trades: List[DebitSpreadTrade], starting_capital: float, path) -> None:
    df = build_equity_curve(trades, starting_capital)
    df.to_csv(path, index=False)


def print_summary(summary: dict) -> None:
    s = summary
    lines = [
        f"Total weeks processed: {s['total_weeks_processed']}",
        f"Trades placed: {s['trades_placed']}",
        f"Trades skipped: {s['trades_skipped']}",
        f"Wins (P&L > 0): {s['wins']}  ({s['win_rate']*100:.2f}%)",
        f"Losses (P&L < 0): {s['losses']}  ({s['loss_rate']*100:.2f}%)",
        f"% profitable weeks: {s['pct_profitable_weeks']*100:.2f}%",
        f"Mean P&L (Rs): {s['mean_pnl_inr']:.2f}",
        f"Median P&L (Rs): {s['median_pnl_inr']:.2f}",
        f"Total P&L (Rs): {s['total_pnl_inr']:.2f}",
        f"Total return on reference capital: {s['total_return_pct']*100:.2f}%",
        f"Max drawdown (Rs / pct): {s['max_drawdown_inr']:.2f} / {s['max_drawdown_pct']*100:.2f}%",
        f"Max consecutive losing weeks: {s['max_consecutive_losses']}",
        f"Sharpe (weekly, ann. sqrt(52)): {s['sharpe']:.4f}",
        f"Sortino (weekly, ann. sqrt(52)): {s['sortino']:.4f}",
        f"Best trade: Rs {s['best_trade_inr']:.2f}    Worst trade: Rs {s['worst_trade_inr']:.2f}",
        f"Exit reason counts: {s['exit_reason_counts']}",
        f"Skip reason counts: {s['skip_reason_counts']}",
    ]
    for line in lines:
        print(line)


def _load_expiry_dates() -> List[_date]:
    """Pull NIFTY weekly expiries from config.py."""
    from config import NIFTY_WEEKLY_EXPIRY_DATES
    return list(NIFTY_WEEKLY_EXPIRY_DATES)


def run(
    config: dict,
    options_path: str,
    output_dir: str,
) -> dict:
    """Top-level entrypoint. Loads parquet, runs backtest, writes CSVs, prints summary."""
    df = pd.read_parquet(options_path)
    df = df[df["underlying"] == "NIFTY"]

    # Normalize datetime column: parquet stores it as tz-aware datetime64,
    # but the rest of the engine compares it to ISO 8601 strings of the form
    # "YYYY-MM-DDTHH:MM:SS+05:30".  Convert once here so all downstream
    # filters (entry slice, holding slice, expiry squareoff) work uniformly.
    if pd.api.types.is_datetime64_any_dtype(df["datetime"]):
        # tz-aware → ISO 8601 with offset
        df = df.assign(datetime=df["datetime"].dt.strftime("%Y-%m-%dT%H:%M:%S%z"))
        # strftime %z gives "+0530" — insert the colon to match "+05:30"
        df["datetime"] = df["datetime"].str.replace(
            r"([+\-]\d{2})(\d{2})$", r"\1:\2", regex=True
        )

    # Range filter (string comparison works since ISO 8601 sorts lexically).
    bt_start_str = f"{config['backtest_start']}T00:00:00+05:30"
    bt_end_str   = f"{config['backtest_end']}T23:59:59+05:30"
    df = df[(df["datetime"] >= bt_start_str) & (df["datetime"] <= bt_end_str)]

    expiry_dates = _load_expiry_dates()

    backtest_result = run_backtest(df, config, expiry_dates)
    trades = backtest_result["trades"]

    capital = float(config["sizing"]["reference_capital"])
    rfr = float(config["metrics"]["risk_free_rate"])
    annualization = int(config["metrics"]["annualization_factor"])

    summary = summarize_metrics(trades, capital, rfr, annualization)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    start_str = config["backtest_start"]
    end_str = config["backtest_end"]
    trades_path = out / f"debit_spread_trades_{start_str}_{end_str}.csv"
    equity_path = out / f"debit_spread_equity_{start_str}_{end_str}.csv"

    write_trades_csv(trades, trades_path)
    write_equity_csv(trades, capital, equity_path)

    print_summary(summary)
    return {
        "trades": trades, "summary": summary,
        "trades_csv": str(trades_path),
        "equity_csv": str(equity_path),
    }


def parse_config(config: dict) -> Dict[str, LegSpec]:
    """Convert config['structure'] into a dict of leg_key -> LegSpec.

    Leg keys are derived deterministically:
      ce_legs[0] -> ce_itm,  ce_legs[1] -> ce_short, ce_legs[2] -> ce_far
      pe_legs[0] -> pe_itm,  pe_legs[1] -> pe_short, pe_legs[2] -> pe_far
    The order in the JSON must match this ITM/short/far convention.
    """
    keys_ce = ["ce_itm", "ce_short", "ce_far"]
    keys_pe = ["pe_itm", "pe_short", "pe_far"]

    structure = config["structure"]
    legs: Dict[str, LegSpec] = {}
    for k, leg_dict in zip(keys_ce, structure["ce_legs"]):
        legs[k] = LegSpec(
            option_type="CE", side=leg_dict["side"],
            lots=int(leg_dict["lots"]),
            strike_offset=int(leg_dict["strike_offset"]),
        )
    for k, leg_dict in zip(keys_pe, structure["pe_legs"]):
        legs[k] = LegSpec(
            option_type="PE", side=leg_dict["side"],
            lots=int(leg_dict["lots"]),
            strike_offset=int(leg_dict["strike_offset"]),
        )
    return legs


def _trading_days_from_df(df: pd.DataFrame) -> List[_date]:
    return sorted({pd.to_datetime(ts).date() for ts in df["datetime"].unique()})


def run_backtest(
    df: pd.DataFrame,
    config: dict,
    expiry_dates: List[_date],
) -> dict:
    """Run the strategy for every expiry in `expiry_dates` against `df`."""
    leg_specs = parse_config(config)
    trading_days = _trading_days_from_df(df)
    days_before = int(config["entry"]["days_before_expiry"])
    entry_time = config["entry"]["entry_time"]
    squareoff_time = config["exit"]["expiry_squareoff_time"]
    tp_mult = float(config["exit"]["tp_multiple_of_max_loss"])
    tp_fixed = config["exit"].get("tp_target_inr")
    tp_fixed = float(tp_fixed) if tp_fixed is not None else None
    gap_minutes = int(config["exit"]["data_gap_force_exit_minutes"])
    capital = float(config["sizing"]["reference_capital"])

    backtest_start = pd.to_datetime(config["backtest_start"]).date()
    backtest_end   = pd.to_datetime(config["backtest_end"]).date()

    trades: List[DebitSpreadTrade] = []
    running_equity = capital

    def _append_skip(reason: str, expiry: _date) -> None:
        skip = _make_skip_trade(
            WeekContext(
                expiry_date=expiry, entry_date=expiry,
                entry_time_str=entry_time,
                expiry_squareoff_time_str=squareoff_time,
                tp_multiple=tp_mult, data_gap_force_exit_minutes=gap_minutes,
                leg_specs=leg_specs, tp_target_inr_fixed=tp_fixed,
            ),
            reason,
        )
        skip.running_equity_inr = running_equity
        trades.append(skip)

    for expiry in sorted(expiry_dates):
        if expiry < backtest_start or expiry > backtest_end:
            continue
        if expiry not in trading_days:
            _append_skip(f"expiry_not_in_data: {expiry}", expiry)
            continue
        try:
            entry_d = compute_entry_date(expiry, trading_days, days_before)
        except ValueError as e:
            _append_skip(f"compute_entry_date_error: {e}", expiry)
            continue

        ctx = WeekContext(
            expiry_date=expiry, entry_date=entry_d,
            entry_time_str=entry_time,
            expiry_squareoff_time_str=squareoff_time,
            tp_multiple=tp_mult, data_gap_force_exit_minutes=gap_minutes,
            leg_specs=leg_specs, tp_target_inr_fixed=tp_fixed,
        )
        trade = run_one_week(df, ctx)
        running_equity += trade.pnl_inr
        trade.return_pct = trade.pnl_inr / capital if capital else 0.0
        trade.running_equity_inr = running_equity
        trades.append(trade)

    return {"trades": trades, "config": config}


def fetch_legs_at(
    slice_df: pd.DataFrame,
    leg_specs: Dict[str, LegSpec],
) -> Tuple[Dict[str, LegFill], List[str]]:
    """Resolve each leg in `leg_specs` against `slice_df` (a single timestamp).

    Returns (legs, missing) where:
      - legs is keyed by leg_key (subset of leg_specs); each LegFill has its
        entry_price set to the row's `open`.
      - missing is the list of leg_keys we couldn't resolve.
    """
    legs: Dict[str, LegFill] = {}
    missing: List[str] = []
    if slice_df.empty:
        return {}, list(leg_specs.keys())

    for leg_key, spec in leg_specs.items():
        match = slice_df[
            (slice_df["option_type"] == spec.option_type)
            & (slice_df["strike_offset"] == spec.strike_offset)
        ]
        if match.empty:
            missing.append(leg_key)
            continue
        row = match.iloc[0]
        legs[leg_key] = LegFill(
            option_type=spec.option_type,
            side=spec.side,
            lots=spec.lots,
            strike_offset=spec.strike_offset,
            strike=float(row["strike"]),
            entry_price=float(row["open"]),
        )
    return legs, missing


def resolve_atm_strike(slice_df: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
    """Return (atm_strike, spot) from a 1-min slice. None if no ATM row."""
    if slice_df.empty:
        return None, None
    atm_rows = slice_df[slice_df["moneyness"] == "ATM"]
    if atm_rows.empty:
        return None, None
    if len(atm_rows) > 1:
        atm_rows = atm_rows.assign(
            _abs_dist=(atm_rows["strike"] - atm_rows["spot"]).abs()
        ).sort_values("_abs_dist")
    row = atm_rows.iloc[0]
    return float(row["strike"]), float(row["spot"])


def trades_to_dataframe(trades: List[DebitSpreadTrade]) -> pd.DataFrame:
    """Flatten trades (including per-leg strikes/prices) into a DataFrame."""
    if not trades:
        return pd.DataFrame()

    rows = []
    for t in trades:
        row = {k: v for k, v in asdict(t).items() if k != "legs"}
        for leg_key, leg in t.legs.items():
            row[f"{leg_key}_strike"] = leg.strike
            row[f"{leg_key}_entry"] = leg.entry_price
            row[f"{leg_key}_exit"] = leg.exit_price
        rows.append(row)
    return pd.DataFrame(rows)
