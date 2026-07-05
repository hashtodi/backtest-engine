"""
Zero Credit Backtest Engine — 4-leg premium-targeted NIFTY weekly options.

Strategy:
  Daily entry. At 09:20 each trading day, buy 1xCE + 1xPE at strikes whose
  09:20 open is closest to Rs 100, and sell 2xCE + 2xPE at strikes whose
  09:20 open is closest to Rs 50. Net premium paid is approx 0
  ("zero credit"). Exit at Rs 1000 combined unrealized P&L (configurable,
  intra-day 1-min check) or 15:20 close. No stop loss. One trade per day.
  See docs/superpowers/specs/2026-05-08-zero-credit-strategy-design.md.
"""

import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

LOT_SIZE_NIFTY = 65  # Mirrors config.LOT_SIZE['NIFTY']; pinned for unit tests.

LEG_KEY_ORDER = ["ce_long", "pe_long", "ce_short", "pe_short"]


@dataclass
class LegSpec:
    """A single leg's static spec (independent of any specific trade)."""
    option_type: str          # "CE" | "PE"
    side: str                 # "BUY" | "SELL"
    lots: int
    premium_target_inr: float


@dataclass
class LegFill:
    """Resolved leg at a specific trade: actual strike + entry/exit prices."""
    option_type: str
    side: str
    lots: int
    premium_target_inr: float
    strike: float
    entry_price: float
    exit_price: Optional[float] = None


@dataclass
class ZeroCreditTrade:
    date: str                 # ISO date string of the entry day
    entry_time: str
    atm_strike: float
    spot_at_entry: float

    net_debit_pts: float
    net_debit_inr: float
    tp_target_inr: float

    exit_time: str
    exit_reason: str          # "TP" | "TIME" | "data_gap_force_exit"
    pnl_pts: float
    pnl_inr: float
    return_pct: float
    running_equity_inr: float

    skip_reason: Optional[str]
    legs: Dict[str, LegFill] = field(default_factory=dict)


@dataclass
class PickResult:
    """Outcome of a single-leg strike pick."""
    skipped: bool
    skip_reason: Optional[str]
    option_type: Optional[str]
    strike: Optional[float]
    entry_price: Optional[float]


def pick_strike_by_premium(
    slice_df: pd.DataFrame,
    option_type: str,
    target_premium_inr: float,
    tolerance_inr: float,
    atm_strike: float,
) -> PickResult:
    """Pick the strike whose `open` is closest to target_premium_inr.

    Rules (in order):
      1. Filter to rows with the given option_type.
      2. Compute |open - target| for each row; smallest wins.
      3. Tiebreaker: smallest |strike - atm_strike|.
      4. Final tiebreak: lower strike for CE / higher strike for PE.
      5. If the winner's |open - target| > tolerance_inr, return skipped.
    """
    if slice_df.empty:
        return PickResult(skipped=True, skip_reason="no_strike_within_tolerance",
                          option_type=None, strike=None, entry_price=None)

    side = slice_df[slice_df["option_type"] == option_type]
    if side.empty:
        return PickResult(skipped=True, skip_reason="no_strike_within_tolerance",
                          option_type=None, strike=None, entry_price=None)

    side = side.copy()
    side["_dpremium"] = (side["open"] - target_premium_inr).abs()
    side["_dstrike"] = (side["strike"] - atm_strike).abs()

    # Sort: primary asc by |dpremium|, secondary asc by |dstrike|, tertiary
    # by strike (asc for CE, desc for PE -> lower CE wins / higher PE wins).
    sort_strike_ascending = (option_type == "CE")
    side = side.sort_values(
        by=["_dpremium", "_dstrike", "strike"],
        ascending=[True, True, sort_strike_ascending],
    )

    winner = side.iloc[0]
    if float(winner["_dpremium"]) > tolerance_inr:
        return PickResult(skipped=True, skip_reason="no_strike_within_tolerance",
                          option_type=option_type,
                          strike=float(winner["strike"]),
                          entry_price=float(winner["open"]))

    return PickResult(
        skipped=False, skip_reason=None,
        option_type=option_type,
        strike=float(winner["strike"]),
        entry_price=float(winner["open"]),
    )


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


def _leg_signed_value(leg: LegFill, price: float) -> float:
    """+lots*price for BUY, -lots*price for SELL."""
    sign = 1 if leg.side == "BUY" else -1
    return sign * leg.lots * price


def compute_entry_economics(
    legs: Dict[str, LegFill],
    tp_target_inr_fixed: float,
    lot_size: int = LOT_SIZE_NIFTY,
) -> Tuple[float, float, float]:
    """Return (net_debit_pts, net_debit_inr, tp_target_inr).

    Sign convention: +1 for BUY, -1 for SELL -> signed_sum = total paid -
    total received = net debit. Positive = debit (cash out); negative = credit.
    TP is a fixed rupee target from config.
    """
    net_debit_pts = sum(_leg_signed_value(leg, leg.entry_price) for leg in legs.values())
    net_debit_inr = net_debit_pts * lot_size
    return net_debit_pts, net_debit_inr, float(tp_target_inr_fixed)


def compute_mtm_inr(
    legs: Dict[str, LegFill],
    current_prices: Dict[str, float],
    net_debit_pts: float,
    lot_size: int = LOT_SIZE_NIFTY,
) -> float:
    """Unrealized P&L vs entry given current per-leg prices.

    With sign=+1 for BUY and -1 for SELL, signed_sum(prices) is the position
    value. Since net_debit_pts == signed_sum(entry_prices), mtm_pts =
    signed_now - net_debit_pts.
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
    """TP-only scan; kept for backwards compat with unit tests. Production
    path uses scan_for_exit_trigger which handles TP and SL together."""
    for bar in bars:
        prices = bar["prices"]
        if any(k not in prices for k in legs):
            continue
        mtm = compute_mtm_inr(legs, prices, net_debit_pts, lot_size)
        if mtm >= tp_target_inr:
            return bar["datetime"], dict(prices), mtm
    return None


def scan_for_exit_trigger(
    legs: Dict[str, LegFill],
    bars: List[Dict],
    net_debit_pts: float,
    tp_target_inr: float,
    sl_target_inr: Optional[float],
    lot_size: int = LOT_SIZE_NIFTY,
) -> Optional[Tuple[pd.Timestamp, Dict[str, float], float, str]]:
    """Walk bars; return (ts, prices, mtm_inr, reason) on first TP/SL trigger.

    Trigger when:
      - mtm_inr >= tp_target_inr -> reason="TP"
      - mtm_inr <= -sl_target_inr -> reason="SL"
    SL is disabled when sl_target_inr is None or <= 0.
    Bars where any leg's price is missing are skipped.
    """
    sl_active = sl_target_inr is not None and sl_target_inr > 0
    sl_threshold = -float(sl_target_inr) if sl_active else None
    for bar in bars:
        prices = bar["prices"]
        if any(k not in prices for k in legs):
            continue
        mtm = compute_mtm_inr(legs, prices, net_debit_pts, lot_size)
        if mtm >= tp_target_inr:
            return bar["datetime"], dict(prices), mtm, "TP"
        if sl_active and mtm <= sl_threshold:
            return bar["datetime"], dict(prices), mtm, "SL"
    return None


def build_bar_stream(
    df: pd.DataFrame,
    legs: Dict[str, LegFill],
    max_gap_minutes: int = 30,
):
    """Yield per-minute bars with carry-forward (<= max_gap_minutes) handling.

    Each yielded dict has:
        datetime: pd.Timestamp
        prices:   Dict[leg_key, float]  (bar 'close' for each leg)
        force_exit: bool                (True iff any leg's gap > max_gap_minutes)

    On force_exit we yield the LAST FULLY-OBSERVED bar (its complete prices),
    not the trigger bar. Iteration stops after emitting force_exit.

    Lookup key: (option_type, strike) - strikes are locked at entry.

    Implementation: pivot to (datetime x leg_key) -> close, ffill within
    `max_gap_minutes` rows (1 row == 1 minute on this dataset), then iterate
    once over the dense matrix. This avoids the per-minute groupby+iterrows
    that dominated the old loop.
    """
    if df.empty:
        return

    leg_lookup = {(l.option_type, float(l.strike)): k for k, l in legs.items()}
    leg_keys_list = list(legs.keys())

    df = df[["datetime", "option_type", "strike", "close"]].copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    keys = list(zip(df["option_type"].astype(str), df["strike"].astype(float)))
    df["_leg"] = [leg_lookup.get(p) for p in keys]
    df = df.dropna(subset=["_leg"])
    if df.empty:
        return

    pivoted = df.pivot_table(
        index="datetime", columns="_leg", values="close", aggfunc="last"
    ).sort_index()
    # Make sure every leg has a column even if some never appeared.
    pivoted = pivoted.reindex(columns=leg_keys_list)

    available = pivoted.ffill(limit=max_gap_minutes)
    mask = available.notna().all(axis=1).values
    ts_arr = pivoted.index.to_list()
    prices_mat = available.values
    n_legs = len(leg_keys_list)

    last_full_idx = -1
    for i in range(len(ts_arr)):
        if mask[i]:
            prices = {leg_keys_list[j]: float(prices_mat[i, j]) for j in range(n_legs)}
            yield {"datetime": pd.Timestamp(ts_arr[i]), "prices": prices,
                   "force_exit": False}
            last_full_idx = i
        else:
            if last_full_idx < 0:
                return
            last_prices = {
                leg_keys_list[j]: float(prices_mat[last_full_idx, j])
                for j in range(n_legs)
            }
            yield {"datetime": pd.Timestamp(ts_arr[last_full_idx]),
                   "prices": last_prices, "force_exit": True}
            return


@dataclass
class DayContext:
    date: _date
    entry_time_str: str                      # "HH:MM"
    time_exit_str: str                       # "HH:MM"
    buy_premium_target_inr: float
    sell_premium_target_inr: float
    buy_lots: int
    sell_lots: int
    premium_match_tolerance_inr: float
    tp_target_inr: float
    data_gap_force_exit_minutes: int
    sl_target_inr: Optional[float] = None
    lot_size: int = LOT_SIZE_NIFTY


LEG_DEFINITIONS: List[Tuple[str, str, str, str]] = [
    # (leg_key, option_type, side, premium_field)
    ("ce_long",  "CE", "BUY",  "buy"),
    ("pe_long",  "PE", "BUY",  "buy"),
    ("ce_short", "CE", "SELL", "sell"),
    ("pe_short", "PE", "SELL", "sell"),
]


def _make_skip_trade(ctx: "DayContext", reason: str) -> "ZeroCreditTrade":
    return ZeroCreditTrade(
        date=ctx.date.isoformat(),
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
        running_equity_inr=float("nan"),
        skip_reason=reason,
        legs={},
    )


def _entry_slice(df: pd.DataFrame, ctx: "DayContext") -> pd.DataFrame:
    if df.empty:
        return df
    target_ts = f"{ctx.date.isoformat()}T{ctx.entry_time_str}:00+05:30"
    # WEEK / expiry_code==1 filtering happens once at load time in run() for
    # speed, but we keep these checks here too so unit tests that pass mixed
    # dataframes (e.g. WEEK + MONTH rows) still get the right contracts.
    if "expiry_type" in df.columns and "expiry_code" in df.columns:
        return df[
            (df["datetime"] == target_ts)
            & (df["expiry_code"] == 1)
            & (df["expiry_type"] == "WEEK")
        ]
    return df[df["datetime"] == target_ts]


def _holding_slice(
    df: pd.DataFrame,
    ctx: "DayContext",
    legs: Dict[str, LegFill],
) -> pd.DataFrame:
    """All bars after entry up through time-exit timestamp, restricted to the
    locked (option_type, strike) pairs. Vectorized for speed."""
    entry_ts = f"{ctx.date.isoformat()}T{ctx.entry_time_str}:00+05:30"
    exit_ts  = f"{ctx.date.isoformat()}T{ctx.time_exit_str}:00+05:30"

    if "expiry_type" in df.columns and "expiry_code" in df.columns:
        sub = df[
            (df["datetime"] > entry_ts)
            & (df["datetime"] <= exit_ts)
            & (df["expiry_code"] == 1)
            & (df["expiry_type"] == "WEEK")
        ]
    else:
        sub = df[(df["datetime"] > entry_ts) & (df["datetime"] <= exit_ts)]
    if sub.empty:
        return sub

    # Vectorized locked-pair membership: build a string key and isin().
    locked_keys = {f"{l.option_type}_{float(l.strike)}" for l in legs.values()}
    sub_keys = sub["option_type"].astype(str) + "_" + sub["strike"].astype(float).astype(str)
    return sub[sub_keys.isin(locked_keys)].sort_values("datetime")


def run_one_day(df: pd.DataFrame, ctx: "DayContext") -> "ZeroCreditTrade":
    """Run the strategy for one trading day against `df` (NIFTY options 1-min)."""
    entry_slice = _entry_slice(df, ctx)
    if entry_slice.empty:
        return _make_skip_trade(ctx, "no_entry_bar")

    atm_strike, spot_at_entry = resolve_atm_strike(entry_slice)
    if atm_strike is None:
        return _make_skip_trade(ctx, "no_atm_row")

    legs: Dict[str, LegFill] = {}
    for leg_key, opt_type, side, premium_field in LEG_DEFINITIONS:
        target = (ctx.buy_premium_target_inr if premium_field == "buy"
                  else ctx.sell_premium_target_inr)
        lots = ctx.buy_lots if side == "BUY" else ctx.sell_lots
        pick = pick_strike_by_premium(
            entry_slice, option_type=opt_type,
            target_premium_inr=target,
            tolerance_inr=ctx.premium_match_tolerance_inr,
            atm_strike=atm_strike,
        )
        if pick.skipped:
            return _make_skip_trade(
                ctx, f"no_strike_within_tolerance: {leg_key}"
            )
        legs[leg_key] = LegFill(
            option_type=opt_type, side=side, lots=lots,
            premium_target_inr=target,
            strike=pick.strike,
            entry_price=pick.entry_price,
        )

    net_pts, net_inr, tp = compute_entry_economics(
        legs, ctx.tp_target_inr, lot_size=ctx.lot_size
    )

    holding_df = _holding_slice(df, ctx, legs)
    bars = list(build_bar_stream(
        holding_df, legs, max_gap_minutes=ctx.data_gap_force_exit_minutes
    ))

    exit_ts_limit = pd.Timestamp(
        f"{ctx.date.isoformat()}T{ctx.time_exit_str}:00+05:30"
    )
    pre_exit_bars = [
        b for b in bars
        if pd.Timestamp(b["datetime"]) < exit_ts_limit and not b.get("force_exit")
    ]

    trigger_result = scan_for_exit_trigger(
        legs, pre_exit_bars, net_pts, tp,
        sl_target_inr=ctx.sl_target_inr, lot_size=ctx.lot_size,
    )

    if trigger_result is not None:
        exit_ts, exit_prices, _mtm, exit_reason = trigger_result
    else:
        force_bar = next((b for b in bars if b.get("force_exit")), None)
        if force_bar is not None:
            exit_ts = pd.Timestamp(force_bar["datetime"])
            exit_prices = force_bar["prices"]
            exit_reason = "data_gap_force_exit"
        else:
            # Time exit: latest non-force-exit bar in the stream is at-or-before
            # time_exit (holding_slice already capped it). Walk-back behaviour
            # is implicit: if the exact 15:20 bar is missing, the prior bar wins.
            last_normal = next(
                (b for b in reversed(bars) if not b.get("force_exit")), None
            )
            if last_normal is None:
                return _make_skip_trade(ctx, "no_time_exit_bar")
            exit_ts = pd.Timestamp(last_normal["datetime"])
            exit_prices = last_normal["prices"]
            exit_reason = "TIME"

    for k, leg in legs.items():
        leg.exit_price = exit_prices[k]
    exit_value_pts = sum(_leg_signed_value(l, l.exit_price) for l in legs.values())
    pnl_pts = exit_value_pts - net_pts
    pnl_inr = pnl_pts * ctx.lot_size

    return ZeroCreditTrade(
        date=ctx.date.isoformat(),
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
        return_pct=0.0,
        running_equity_inr=0.0,
        skip_reason=None,
        legs=legs,
    )


def parse_config(config: dict) -> dict:
    """Flatten a JSON config into the kwargs needed by run_backtest."""
    structure = config["structure"]
    exit_cfg = config["exit"]
    sl_raw = exit_cfg.get("sl_target_inr")
    sl_target = float(sl_raw) if sl_raw is not None else None
    return {
        "entry_time": config["entry"]["entry_time"],
        "time_exit": exit_cfg["time_exit"],
        "buy_premium_target_inr": float(structure["buy_premium_target_inr"]),
        "sell_premium_target_inr": float(structure["sell_premium_target_inr"]),
        "buy_lots": int(structure["buy_lots"]),
        "sell_lots": int(structure["sell_lots"]),
        "premium_match_tolerance_inr": float(structure["premium_match_tolerance_inr"]),
        "tp_target_inr": float(exit_cfg["tp_target_inr"]),
        "sl_target_inr": sl_target,
        "data_gap_force_exit_minutes": int(exit_cfg["data_gap_force_exit_minutes"]),
        "reference_capital": float(config["sizing"]["reference_capital"]),
    }


def _trading_days_from_df(df: pd.DataFrame) -> List[_date]:
    """Fast date extraction: when datetime is an ISO-8601 string, slice the
    first 10 chars instead of doing per-element pd.to_datetime."""
    if df.empty:
        return []
    if pd.api.types.is_string_dtype(df["datetime"]) or df["datetime"].dtype == object:
        return sorted({d for d in df["datetime"].str[:10].unique()
                       if isinstance(d, str)},
                      key=lambda s: s)  # ISO strings sort lexically
    # Fallback for tz-aware datetime64 inputs (used in some unit tests).
    return sorted({pd.to_datetime(ts).date() for ts in df["datetime"].unique()})


def _date_key(df: pd.DataFrame) -> pd.Series:
    """Return a per-row date key suitable for groupby. Cheap on string columns,
    falls back to .dt.date for datetime64 inputs."""
    if pd.api.types.is_string_dtype(df["datetime"]) or df["datetime"].dtype == object:
        return df["datetime"].str[:10]
    return pd.to_datetime(df["datetime"]).dt.date.astype(str)


def run_backtest(df: pd.DataFrame, config: dict) -> dict:
    """Run the strategy for every trading day in [backtest_start, backtest_end].

    Pre-groups the input df by date once so each `run_one_day` call sees a
    small per-day slice instead of re-filtering the full parquet.
    """
    p = parse_config(config)
    bt_start = pd.to_datetime(config["backtest_start"]).date()
    bt_end   = pd.to_datetime(config["backtest_end"]).date()
    capital = p["reference_capital"]

    trades: List[ZeroCreditTrade] = []
    running_equity = capital

    if df.empty:
        return {"trades": trades, "config": config}

    df = df.copy()
    df["_date"] = _date_key(df)

    for date_str, day_df in df.groupby("_date", sort=True):
        try:
            d = _date.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue
        if not (bt_start <= d <= bt_end):
            continue
        ctx = DayContext(
            date=d,
            entry_time_str=p["entry_time"],
            time_exit_str=p["time_exit"],
            buy_premium_target_inr=p["buy_premium_target_inr"],
            sell_premium_target_inr=p["sell_premium_target_inr"],
            buy_lots=p["buy_lots"],
            sell_lots=p["sell_lots"],
            premium_match_tolerance_inr=p["premium_match_tolerance_inr"],
            tp_target_inr=p["tp_target_inr"],
            sl_target_inr=p["sl_target_inr"],
            data_gap_force_exit_minutes=p["data_gap_force_exit_minutes"],
        )
        trade = run_one_day(day_df, ctx)
        running_equity += trade.pnl_inr
        trade.return_pct = trade.pnl_inr / capital if capital else 0.0
        trade.running_equity_inr = running_equity
        trades.append(trade)

    return {"trades": trades, "config": config}


def _is_nan(x) -> bool:
    try:
        return x != x  # NaN != NaN
    except Exception:
        return False


def build_equity_curve(
    trades: List[ZeroCreditTrade],
    starting_capital: float,
) -> pd.DataFrame:
    """One row per trade attempt: date, equity_inr, drawdown_inr, drawdown_pct,
    in_trade. Skipped days carry forward running_equity_inr unchanged."""
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
    trades: List[ZeroCreditTrade],
    starting_capital: float,
) -> dict:
    """Trimmed summary per spec section 8.3 - no Sharpe / Sortino / annualized."""
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
        "pct_profitable_days": len(wins) / len(placed) if placed else 0.0,
        "mean_pnl_inr": statistics.fmean(pnls) if pnls else 0.0,
        "median_pnl_inr": statistics.median(pnls) if pnls else 0.0,
        "total_pnl_inr": sum(pnls),
        "total_return_pct": sum(pnls) / starting_capital if starting_capital else 0.0,
        "max_drawdown_inr": max_dd_inr,
        "max_drawdown_pct": max_dd_pct,
        "max_consecutive_losses": max_consecutive_losses(pnls),
        "best_trade_inr": max(pnls) if pnls else 0.0,
        "worst_trade_inr": min(pnls) if pnls else 0.0,
        "exit_reason_counts": _count_by(placed, lambda t: t.exit_reason),
        "skip_reason_counts": _count_by(
            [t for t in trades if t.skip_reason], lambda t: t.skip_reason
        ),
    }


def trades_to_dataframe(trades: List[ZeroCreditTrade]) -> pd.DataFrame:
    """Flatten trades (with per-leg strikes/prices) into a DataFrame."""
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


def write_trades_csv(trades: List[ZeroCreditTrade], path) -> None:
    df = trades_to_dataframe(trades)
    df.to_csv(path, index=False)


def write_equity_csv(
    trades: List[ZeroCreditTrade],
    starting_capital: float,
    path,
) -> None:
    df = build_equity_curve(trades, starting_capital)
    df.to_csv(path, index=False)


def print_summary(summary: dict) -> None:
    s = summary
    lines = [
        f"Total days processed: {s['total_days_processed']}",
        f"Trades placed: {s['trades_placed']}",
        f"Trades skipped: {s['trades_skipped']}",
        f"Wins (P&L > 0): {s['wins']}  ({s['win_rate']*100:.2f}%)",
        f"Losses (P&L < 0): {s['losses']}  ({s['loss_rate']*100:.2f}%)",
        f"% profitable days: {s['pct_profitable_days']*100:.2f}%",
        f"Mean P&L (Rs): {s['mean_pnl_inr']:.2f}",
        f"Median P&L (Rs): {s['median_pnl_inr']:.2f}",
        f"Total P&L (Rs): {s['total_pnl_inr']:.2f}",
        f"Total return on reference capital: {s['total_return_pct']*100:.2f}%",
        f"Max drawdown (Rs / pct): {s['max_drawdown_inr']:.2f} / {s['max_drawdown_pct']*100:.2f}%",
        f"Max consecutive losing days: {s['max_consecutive_losses']}",
        f"Best day: Rs {s['best_trade_inr']:.2f}    Worst day: Rs {s['worst_trade_inr']:.2f}",
        f"Exit reason counts: {s['exit_reason_counts']}",
        f"Skip reason counts: {s['skip_reason_counts']}",
    ]
    for line in lines:
        print(line)


def run(
    config: dict,
    options_path: str,
    output_dir: str,
) -> dict:
    """Top-level entrypoint. Loads parquet, runs backtest, writes CSVs, prints summary."""
    df = pd.read_parquet(options_path)
    df = df[df["underlying"] == "NIFTY"]

    # Strategy never reads non-WEEK contracts or expiry_code != 1, so filter
    # at load time. This shrinks the working set ~5x and makes every
    # downstream filter much cheaper.
    df = df[(df["expiry_type"] == "WEEK") & (df["expiry_code"] == 1)]

    # Normalize datetime to ISO-8601 strings of the form
    # "YYYY-MM-DDTHH:MM:SS+05:30" so all downstream filters work uniformly.
    if pd.api.types.is_datetime64_any_dtype(df["datetime"]):
        df = df.assign(datetime=df["datetime"].dt.strftime("%Y-%m-%dT%H:%M:%S%z"))
        df["datetime"] = df["datetime"].str.replace(
            r"([+\-]\d{2})(\d{2})$", r"\1:\2", regex=True
        )

    bt_start_str = f"{config['backtest_start']}T00:00:00+05:30"
    bt_end_str   = f"{config['backtest_end']}T23:59:59+05:30"
    df = df[(df["datetime"] >= bt_start_str) & (df["datetime"] <= bt_end_str)]

    backtest_result = run_backtest(df, config)
    trades = backtest_result["trades"]

    capital = float(config["sizing"]["reference_capital"])
    summary = summarize_metrics(trades, capital)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    start_str = config["backtest_start"]
    end_str = config["backtest_end"]
    trades_path = out / f"zero_credit_trades_{start_str}_{end_str}.csv"
    equity_path = out / f"zero_credit_equity_{start_str}_{end_str}.csv"

    write_trades_csv(trades, trades_path)
    write_equity_csv(trades, capital, equity_path)

    print_summary(summary)
    return {
        "trades": trades, "summary": summary,
        "trades_csv": str(trades_path),
        "equity_csv": str(equity_path),
    }
