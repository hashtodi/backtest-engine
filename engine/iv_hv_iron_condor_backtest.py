"""IV/HV-ratio NIFTY weekly iron condor (S165). See
docs/superpowers/specs/2026-07-02-iv-hv-iron-condor-design.md.

Gross P&L only. Weekly code-1 (0DTE on expiry day). Delta computed from IV.
"""
import os
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from engine.black_scholes import bs_delta
from engine.historical_vol import compute_hv20
import config

DEFAULT_OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"
DEFAULT_SPOT_PATH = "data/spot/nifty/NIFTY_1m.parquet"
DEFAULT_OUTPUT_DIR = "output/iv_hv_iron_condor"

OPT_COLS = ["datetime", "option_type", "strike", "strike_offset", "spot",
            "open", "close", "iv"]


def _norm_time(t: str) -> str:
    """Normalize H:MM / HH:MM / HH:MM:SS -> HH:MM."""
    parts = str(t).split(":")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


@dataclass
class DayContext:
    iv_rv_ratio_min: float = 1.3
    hv_lookback: int = 20
    window_start: str = "09:45"
    window_end: str = "11:30"
    tp_pct: float = 0.50
    sl_pct: float = 2.00
    hard_exit_time: str = "15:10"
    sell_ce_delta: float = 0.20
    buy_ce_delta: float = 0.08
    sell_pe_delta: float = -0.20
    buy_pe_delta: float = -0.08
    min_credit_pts: float = 0.0
    max_trades_per_day: int = 1
    strike_step: int = 50
    risk_free_rate: float = 0.065
    dividend_yield: float = 0.0
    lots: int = 4
    lot_size: int = 65


def parse_config(cfg: dict) -> DayContext:
    sig = cfg.get("signal", {})
    ent = cfg.get("entry", {})
    ex = cfg.get("exit", {})
    st = cfg.get("structure", {})
    gr = cfg.get("greeks", {})
    sz = cfg.get("sizing", {})
    d = DayContext()
    return DayContext(
        iv_rv_ratio_min=float(sig.get("iv_rv_ratio_min", d.iv_rv_ratio_min)),
        hv_lookback=int(sig.get("hv_lookback", d.hv_lookback)),
        window_start=_norm_time(ent.get("window_start", d.window_start)),
        window_end=_norm_time(ent.get("window_end", d.window_end)),
        tp_pct=float(ex.get("tp_pct", d.tp_pct)),
        sl_pct=float(ex.get("sl_pct", d.sl_pct)),
        hard_exit_time=_norm_time(ex.get("hard_exit_time", d.hard_exit_time)),
        sell_ce_delta=float(st.get("sell_ce_delta", d.sell_ce_delta)),
        buy_ce_delta=float(st.get("buy_ce_delta", d.buy_ce_delta)),
        sell_pe_delta=float(st.get("sell_pe_delta", d.sell_pe_delta)),
        buy_pe_delta=float(st.get("buy_pe_delta", d.buy_pe_delta)),
        min_credit_pts=float(st.get("min_credit_pts", d.min_credit_pts)),
        max_trades_per_day=int(st.get("max_trades_per_day", d.max_trades_per_day)),
        strike_step=int(st.get("strike_step", d.strike_step)),
        risk_free_rate=float(gr.get("risk_free_rate", d.risk_free_rate)),
        dividend_yield=float(gr.get("dividend_yield", d.dividend_yield)),
        lots=int(sz.get("lots", d.lots)),
        lot_size=int(sz.get("lot_size", d.lot_size)),
    )


def load_options(options_path: str, start_date: str, end_date: str) -> pd.DataFrame:
    df = pd.read_parquet(options_path, columns=OPT_COLS,
        filters=[("underlying", "==", "NIFTY"),
                 ("expiry_type", "==", "WEEK"), ("expiry_code", "==", 1)])
    # string-slice date filter (datetime is ISO string) — inclusive range
    d10 = df["datetime"].str.slice(0, 10)
    df = df[(d10 >= start_date) & (d10 <= end_date)].copy()
    df["_dt"] = pd.to_datetime(df["datetime"].str.slice(0, 19))  # naive IST
    df["_date"] = df["_dt"].dt.date
    df["_time"] = df["_dt"].dt.strftime("%H:%M")
    return df.sort_values("_dt").reset_index(drop=True)


def find_signals(df: pd.DataFrame, hv_map: dict, ctx: DayContext) -> pd.DataFrame:
    """Stage 1: one signal row per day where ATM_IV/HV_20d first crosses the
    threshold inside the entry window."""
    atm = df[df["strike_offset"] == 0]
    g = (atm.groupby(["_dt", "_date", "_time"], as_index=False)
            .agg(atm_iv=("iv", "mean")))
    g["hv"] = g["_date"].map(hv_map)
    g = g[(g["_time"] >= ctx.window_start) & (g["_time"] <= ctx.window_end)]
    g = g.dropna(subset=["atm_iv", "hv"])
    g = g[(g["atm_iv"] > 0) & (g["hv"] > 0)]
    if g.empty:
        return g.assign(ratio=[], direction=[])
    g = g.assign(ratio=g["atm_iv"] / g["hv"])
    hits = g[g["ratio"] > ctx.iv_rv_ratio_min].sort_values("_dt")
    signals = hits.groupby("_date", as_index=False).first()
    signals["direction"] = "bearish"
    return signals.sort_values("_dt").reset_index(drop=True)


@dataclass
class LegFill:
    option_type: str
    strike: float
    strike_offset: int
    delta: float
    entry: float


def minutes_to_expiry(signal_dt: datetime, expiry_date) -> float:
    expiry_dt = datetime(expiry_date.year, expiry_date.month, expiry_date.day, 15, 30)
    return (expiry_dt - signal_dt).total_seconds() / 60.0


def add_delta(bar: pd.DataFrame, spot: float, T: float, ctx: DayContext) -> pd.DataFrame:
    bar = bar.copy()
    bar["delta"] = [
        bs_delta(ot, spot, k, iv / 100.0, T, ctx.risk_free_rate, ctx.dividend_yield)
        for ot, k, iv in zip(bar["option_type"], bar["strike"], bar["iv"])
    ]
    return bar


def _nearest(bar: pd.DataFrame, option_type: str, target: float):
    c = bar[(bar["option_type"] == option_type) & bar["delta"].notna()]
    if c.empty:
        return None
    row = c.loc[(c["delta"] - target).abs().idxmin()]
    return LegFill(option_type, float(row["strike"]), int(row["strike_offset"]),
                   float(row["delta"]), float(row["close"]))


def select_legs(bar: pd.DataFrame, spot: float, T: float, ctx: DayContext) -> dict:
    if "delta" not in bar.columns:
        bar = add_delta(bar, spot, T, ctx)
    return {
        "sell_ce": _nearest(bar, "CE", ctx.sell_ce_delta),
        "buy_ce":  _nearest(bar, "CE", ctx.buy_ce_delta),
        "sell_pe": _nearest(bar, "PE", ctx.sell_pe_delta),
        "buy_pe":  _nearest(bar, "PE", ctx.buy_pe_delta),
    }


def net_credit_pts(legs: dict) -> float:
    net = (legs["sell_ce"].entry + legs["sell_pe"].entry
           - legs["buy_ce"].entry - legs["buy_pe"].entry)
    return abs(net)


def is_formable(legs: dict) -> bool:
    """A real condor needs each wing at a different strike than its short.
    When the option chain is too narrow (e.g. multi-DTE days where the deepest
    available strike is still ~0.2 delta), the 0.08 wing collapses onto the
    0.20 short -> a 0-width spread that cannot be traded. Skip those days."""
    return (legs["buy_ce"].strike != legs["sell_ce"].strike
            and legs["buy_pe"].strike != legs["sell_pe"].strike)


def _leg_price_maps(day_df: pd.DataFrame, legs: dict) -> dict:
    """{leg_key: {time: close}} for the 4 locked contracts."""
    maps = {}
    for key, leg in legs.items():
        sub = day_df[(day_df["option_type"] == leg.option_type)
                     & (day_df["strike"] == leg.strike)]
        maps[key] = dict(zip(sub["_time"], sub["close"]))
    return maps


def _running_pnl_pts(legs: dict, cur: dict) -> float:
    pnl = 0.0
    for key, leg in legs.items():
        c = cur[key]
        if key.startswith("sell"):
            pnl += leg.entry - c
        else:
            pnl += c - leg.entry
    return pnl


def simulate_trade(day_df: pd.DataFrame, legs: dict, credit: float,
                   entry_dt: datetime, ctx: DayContext) -> dict:
    """Stage 2: monitor each minute close after entry to hard_exit_time.
    Exit on first of TP / SL / TIME. Fills at the exit-bar close.

    DATA CAVEAT: only +/-10 offsets around each minute's ATM are stored, so a
    locked strike disappears when spot drifts >~500 pts. For minutes where a leg
    is out of the window we carry forward its last-seen close and set
    `fill_fallback=True`. This is a KNOWN bias — it understates losses when a
    SHORT leg goes deep ITM and vanishes. Trades with fill_fallback should be
    treated as unreliable until the missing legs are repriced (see project notes).
    ~26% of trades touch this; the summary reports the count."""
    tp = ctx.tp_pct * credit
    sl = -ctx.sl_pct * credit
    maps = _leg_price_maps(day_df, legs)
    entry_time = entry_dt.strftime("%H:%M")
    day_times = set(day_df["_time"])
    times = sorted({t for m in maps.values() for t in m} & day_times)
    last_valid = {k: legs[k].entry for k in legs}
    exit_reason = exit_time = exit_prices = None
    exit_missing = []
    missing_minutes = 0
    for t in times:
        if t <= entry_time or t > ctx.hard_exit_time:
            continue
        miss = [k for k in legs if t not in maps[k]]
        if miss:
            missing_minutes += 1
        cur = {}
        for k in legs:
            if t in maps[k]:
                last_valid[k] = maps[k][t]
            cur[k] = last_valid[k]
        pnl = _running_pnl_pts(legs, cur)
        if pnl >= tp:
            exit_reason, exit_time, exit_prices, exit_missing = "TP", t, dict(cur), miss
            break
        if pnl <= sl:
            exit_reason, exit_time, exit_prices, exit_missing = "SL", t, dict(cur), miss
            break
        if t == ctx.hard_exit_time:
            exit_reason, exit_time, exit_prices, exit_missing = "TIME", t, dict(cur), miss
            break
    if exit_reason is None:  # no bar reached hard_exit_time — settle on last seen
        exit_reason = "TIME"
        exit_time = times[-1] if times else entry_time
        exit_prices = dict(last_valid)
        exit_missing = [k for k in legs
                        if not times or times[-1] not in maps[k]]
    pnl_pts = _running_pnl_pts(legs, exit_prices)
    return {"exit_time": exit_time, "exit_reason": exit_reason,
            "pnl_pts": pnl_pts, "pnl_inr": pnl_pts * ctx.lot_size * ctx.lots,
            "exit_prices": exit_prices,
            "fill_fallback": bool(exit_missing),
            "missing_at_exit": list(exit_missing),
            "missing_minutes": missing_minutes}


def sanity_flag(legs: dict, pnl_pts: float) -> bool:
    """A condor's MTM P&L is bounded by its spread width; anything beyond that
    is a likely bad-tick fill. Flag (do not drop)."""
    ce_w = abs(legs["buy_ce"].strike - legs["sell_ce"].strike)
    pe_w = abs(legs["sell_pe"].strike - legs["buy_pe"].strike)
    return abs(pnl_pts) > max(ce_w, pe_w)


def run_backtest(options_df: pd.DataFrame, hv_map: dict, ctx: DayContext):
    """Iterate signal days: select legs, simulate, collect trade rows.
    Honors no-overlap (signal must be strictly after the previous exit).

    Returns (trades: list[dict], stats: dict) where stats records how many
    signals were skipped and why (overlap / no valid legs / un-formable / low credit)."""
    signals = find_signals(options_df, hv_map, ctx)
    by_day = {d: g for d, g in options_df.groupby("_date")}
    trades, equity, prev_exit_dt = [], 0.0, None
    stats = {"signals": int(len(signals)), "skipped_overlap": 0,
             "skipped_no_legs": 0, "skipped_unformable": 0, "skipped_low_credit": 0}
    for _, sig in signals.iterrows():
        sig_dt = sig["_dt"].to_pydatetime()
        if prev_exit_dt is not None and sig_dt <= prev_exit_dt:
            stats["skipped_overlap"] += 1
            continue  # no overlapping trades
        day_df = by_day[sig["_date"]]
        bar = day_df[day_df["_dt"] == sig["_dt"]]
        spot = float(bar["spot"].iloc[0])
        expiry = config.get_nearest_weekly_expiry(sig["_date"])
        T = minutes_to_expiry(sig_dt, expiry) / 525600.0
        legs = select_legs(bar, spot, T, ctx)
        if any(v is None for v in legs.values()):
            stats["skipped_no_legs"] += 1
            continue
        if not is_formable(legs):
            stats["skipped_unformable"] += 1
            continue  # wing collapsed onto short (narrow chain) — un-tradeable
        credit = net_credit_pts(legs)
        if credit < ctx.min_credit_pts:
            stats["skipped_low_credit"] += 1
            continue
        res = simulate_trade(day_df, legs, credit, sig_dt, ctx)
        equity += res["pnl_inr"]
        exit_dt = datetime.combine(
            sig["_date"], datetime.strptime(res["exit_time"], "%H:%M").time())
        prev_exit_dt = exit_dt
        row = {"date": str(sig["_date"]), "signal_time": sig["_time"],
               "direction": sig["direction"], "atm_iv": round(sig["atm_iv"], 3),
               "hv_20d": round(sig["hv"], 3), "ratio": round(sig["ratio"], 3),
               "spot": spot, "expiry": str(expiry),
               "net_credit_pts": round(credit, 2),
               "tp_pts": round(ctx.tp_pct * credit, 2),
               "sl_pts": round(-ctx.sl_pct * credit, 2),
               "exit_time": res["exit_time"], "exit_reason": res["exit_reason"],
               "pnl_pts": round(res["pnl_pts"], 2), "pnl_inr": round(res["pnl_inr"], 2),
               "running_equity_inr": round(equity, 2),
               "sanity_flag": sanity_flag(legs, res["pnl_pts"]),
               "fill_fallback": res["fill_fallback"],
               "missing_at_exit": ",".join(res["missing_at_exit"]),
               "missing_minutes": res["missing_minutes"]}
        for k, leg in legs.items():
            row[f"{k}_strike"] = leg.strike
            row[f"{k}_offset"] = leg.strike_offset
            row[f"{k}_delta"] = round(leg.delta, 3)
            row[f"{k}_entry"] = leg.entry
            row[f"{k}_exit"] = round(res["exit_prices"][k], 2)
        trades.append(row)
    stats["trades"] = len(trades)
    return trades, stats


def trades_to_dataframe(trades: list) -> pd.DataFrame:
    return pd.DataFrame(trades)


def summarize_metrics(trades: list) -> dict:
    """Headline metrics over RELIABLE trades only. Trades where a locked leg left
    the +/-10 window (fill_fallback) are EXCLUDED from the headline and reported
    separately — their exit fill is unreliable (see simulate_trade).

    IMPORTANT: excluded trades skew to volatile / large-move days, so the headline
    is OPTIMISTIC about calm regimes and is NOT an all-in result. Read it together
    with `excluded_fallback_trades` / `excluded_fallback_pnl_inr`."""
    reliable = [t for t in trades if not t.get("fill_fallback")]
    fallback = [t for t in trades if t.get("fill_fallback")]
    n = len(reliable)
    pnls = [t["pnl_inr"] for t in reliable]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    counts = {}
    for t in reliable:
        counts[t["exit_reason"]] = counts.get(t["exit_reason"], 0) + 1
    eq = peak = mdd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    srt = sorted(pnls)
    median = 0.0
    if srt:
        mid = len(srt) // 2
        median = srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2
    flagged = sum(1 for t in reliable if t.get("sanity_flag"))
    sanity_ok = sum(t["pnl_inr"] for t in reliable if not t.get("sanity_flag"))
    return {"total_trades": n, "all_trades": len(trades),
            "wins": wins, "losses": losses,
            "win_rate": round(wins / n, 4) if n else 0.0,
            "total_pnl_inr": round(sum(pnls), 2),
            "mean_pnl_inr": round(sum(pnls) / n, 2) if n else 0.0,
            "median_pnl_inr": round(median, 2),
            "max_drawdown_inr": round(mdd, 2),
            "best_trade_inr": round(max(pnls), 2) if pnls else 0.0,
            "worst_trade_inr": round(min(pnls), 2) if pnls else 0.0,
            "exit_reason_counts": counts,
            "sanity_flagged": flagged,
            "total_pnl_inr_sanity_filtered": round(sanity_ok, 2),
            "excluded_fallback_trades": len(fallback),
            "excluded_fallback_pnl_inr": round(sum(t["pnl_inr"] for t in fallback), 2)}


def run(config_dict: dict, options_path: str = DEFAULT_OPTIONS_PATH,
        spot_path: str = DEFAULT_SPOT_PATH,
        output_dir: str = DEFAULT_OUTPUT_DIR) -> dict:
    """Orchestrate a full backtest from a config dict. Writes a trades CSV to
    output_dir and returns everything the CLI and UI need to render results."""
    ctx = parse_config(config_dict)
    start = config_dict["backtest_start"]
    end = config_dict["backtest_end"]
    df = load_options(options_path, start, end)
    days = int(df["_date"].nunique())
    hv = compute_hv20(spot_path, ctx.hv_lookback)
    trades, stats = run_backtest(df, hv, ctx)
    tdf = trades_to_dataframe(trades)
    os.makedirs(output_dir, exist_ok=True)
    trades_csv = os.path.join(output_dir, f"iv_hv_condor_{start}_{end}.csv")
    tdf.to_csv(trades_csv, index=False)
    return {"summary": summarize_metrics(trades), "stats": stats,
            "days_processed": days, "trades_csv": trades_csv, "trades_df": tdf}
