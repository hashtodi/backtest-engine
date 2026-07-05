"""
Boom ST — Marketplace-format report generator.

Runs the Boom ST backtest on NIFTY for the available data window and writes
a printable Markdown writeup matching the marketplace screenshot layout
(Description / Strategy Logic / Risk / Highlights / Statistics / Daily Summary
/ Month Wise PNL).
"""

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from engine.boom_st_backtest import BoomStBacktestEngine, trades_to_dataframe  # noqa: E402

# ---------- Run config ----------
INSTRUMENT      = "NIFTY"
START_DATE      = "2025-01-01"
END_DATE        = "2026-04-23"
SMA_PERIOD      = 13
ST_SIG          = (4, 11)
ST_ENT          = (3, 10)
SL_PCT          = 5.0
TP_PCT          = 7.5
TRADING_START   = "09:30"
TRADING_END     = "14:45"
CAPITAL_REQUIRED = 100_000   # assumed deployable capital for ROI calc

OUT_CSV = ROOT / "boom_st_nifty_marketplace.csv"
OUT_MD  = ROOT / "Boom-ST-Strategy-Writeup.md"


def run_backtest() -> pd.DataFrame:
    engine = BoomStBacktestEngine(
        start_date=START_DATE, end_date=END_DATE,
        sma_period=SMA_PERIOD,
        st_signal_factor=ST_SIG[0], st_signal_atr=ST_SIG[1],
        st_entry_factor=ST_ENT[0], st_entry_atr=ST_ENT[1],
        sl_pct=SL_PCT, tp_pct=TP_PCT,
        trading_start=TRADING_START, trading_end=TRADING_END,
        instrument=INSTRUMENT,
    )

    def progress(i, total, ds):
        if i % 25 == 0 or i == total - 1:
            print(f"  [{i+1}/{total}] {ds}", flush=True)

    print(f"Running Boom ST backtest on {INSTRUMENT} from {START_DATE} to {END_DATE}…")
    trades = engine.run(progress_callback=progress)
    print(f"Done. {len(trades)} trades.")
    df = trades_to_dataframe(trades)
    df.to_csv(OUT_CSV, index=False)
    return df


def compute_stats(df: pd.DataFrame) -> dict:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["pnl_inr"] = df["pnl_inr"].astype(float)

    daily = df.groupby("date")["pnl_inr"].sum().sort_index()
    total_trading_days = len(daily)
    win_days = int((daily > 0).sum())
    loss_days = int((daily < 0).sum())
    win_rate = win_days / total_trading_days * 100 if total_trading_days else 0.0

    # Streaks (day-level)
    sign = np.sign(daily.values)
    max_win_streak = max_loss_streak = cur_w = cur_l = 0
    for s in sign:
        if s > 0:
            cur_w += 1; cur_l = 0
        elif s < 0:
            cur_l += 1; cur_w = 0
        else:
            cur_w = cur_l = 0
        max_win_streak = max(max_win_streak, cur_w)
        max_loss_streak = max(max_loss_streak, cur_l)

    total_profit = float(daily.sum())
    daily_ret_pct = daily / CAPITAL_REQUIRED * 100
    avg_monthly_profit = total_profit / max(1, len({(d.year, d.month) for d in daily.index}))
    total_roi_pct = total_profit / CAPITAL_REQUIRED * 100
    months_count = len({(d.year, d.month) for d in daily.index})
    avg_monthly_roi_pct = total_roi_pct / max(1, months_count)

    # Sharpe / Sortino (annualised, daily series)
    std_daily = daily_ret_pct.std()
    mean_daily = daily_ret_pct.mean()
    ann_factor = np.sqrt(252)
    sharpe = (mean_daily / std_daily) * ann_factor if std_daily > 0 else 0
    downside = daily_ret_pct[daily_ret_pct < 0].std()
    sortino = (mean_daily / downside) * ann_factor if downside and downside > 0 else 0

    # Equity curve & drawdown
    equity = daily.cumsum() + CAPITAL_REQUIRED
    peak = equity.cummax()
    dd_inr = equity - peak
    dd_pct = dd_inr / peak * 100
    max_dd_inr = float(dd_inr.min()) if len(dd_inr) else 0
    max_dd_pct = float(dd_pct.min()) if len(dd_pct) else 0

    profit_days = daily[daily > 0]
    loss_days_s = daily[daily < 0]
    avg_profit_on_profit_days = float(profit_days.mean()) if len(profit_days) else 0
    avg_loss_on_loss_days = float(loss_days_s.mean()) if len(loss_days_s) else 0
    max_profit_day = float(daily.max()) if len(daily) else 0
    max_loss_day = float(daily.min()) if len(daily) else 0

    # Avg trades per day (buy + sell legs = 2 per round-trip)
    avg_trades_per_day = len(df) * 2 / max(1, total_trading_days)

    # Annualised stdev (returns %) and total ROI
    ann_std = std_daily * ann_factor

    return dict(
        total_trading_days=total_trading_days,
        win_days=win_days,
        loss_days=loss_days,
        max_winning_streak_days=max_win_streak,
        max_losing_streak_days=max_loss_streak,
        win_rate=win_rate,
        avg_monthly_profit=avg_monthly_profit,
        total_profit=total_profit,
        avg_monthly_roi_pct=avg_monthly_roi_pct,
        total_roi_pct=total_roi_pct,
        std_dev_ann=ann_std,
        sharpe=sharpe,
        sortino=sortino,
        max_profit_day=max_profit_day,
        max_loss_day=max_loss_day,
        avg_profit_on_profit_days=avg_profit_on_profit_days,
        avg_loss_on_loss_days=avg_loss_on_loss_days,
        avg_trades_per_day=avg_trades_per_day,
        max_dd_inr=max_dd_inr,
        max_dd_pct=max_dd_pct,
        daily=daily,
    )


def daily_summary(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    daily = df.groupby("date")["pnl_inr"].sum()
    daily.index = pd.to_datetime(daily.index)
    out = []
    for dow, name in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]):
        sub = daily[daily.index.dayofweek == dow]
        if len(sub) == 0:
            out.append([name, 0, 0, 0, 0])
            continue
        ret_pct = sub.sum() / CAPITAL_REQUIRED * 100
        out.append([
            name,
            round(ret_pct, 2),
            round(float(sub.max()), 2),
            round(float(sub.min()), 2),
            round(float(sub.sum()), 2),
        ])
    return pd.DataFrame(out, columns=["Day", "Returns (%)", "Max profit", "Max loss", "PNL (Rs)"])


def month_wise(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.to_period("M")
    g = df.groupby("month").agg(total_trades=("pnl_inr", "size"),
                                 pnl=("pnl_inr", "sum"))
    g["pnl_pct"] = g["pnl"] / CAPITAL_REQUIRED * 100
    g = g.reset_index()
    g["Month"] = g["month"].dt.strftime("%b, %y")
    return g[["Month", "total_trades", "pnl", "pnl_pct"]].rename(
        columns={"total_trades": "Total Trades", "pnl": "PNL (Rs)", "pnl_pct": "PNL (%)"}
    )


def build_markdown(df: pd.DataFrame, stats: dict, daily_df: pd.DataFrame, month_df: pd.DataFrame) -> str:
    inr = lambda x: f"INR {x:,.2f}"
    pct = lambda x: f"{x:.2f}%"

    sl_exits = int((df["exit_reason"] == "SL").sum())
    tp_exits = int((df["exit_reason"] == "TP").sum())
    eod_exits = int((df["exit_reason"] == "EOD").sum())

    md = []
    md.append("# Boom ST Entry — NIFTY")
    md.append("")
    md.append("| | |")
    md.append("|---|---|")
    md.append(f"| **Tags** | Bullish, Directional, Option Buying |")
    md.append(f"| **Margin** | ₹ {CAPITAL_REQUIRED:,} |")
    md.append(f"| **Exchange** | NFO |")
    md.append("")
    md.append("## Description")
    md.append("")
    md.append(
        "This is a **directional, option-buying intraday strategy** on the NIFTY index. "
        "It looks for explosive momentum on individual ATM option contracts using a "
        "triple-confluence filter (SMA + two SuperTrends), then enters on a pullback to "
        "the faster SuperTrend value via a limit order. CE and PE are tracked independently "
        "so the strategy can be long both calls and puts simultaneously when both are "
        "trending. Fixed % SL / TP and forced EOD exit keep risk bounded."
    )
    md.append("")
    md.append("## Strategy Logic")
    md.append("")
    md.append("1. **Signal — Triple Momentum Filter** (per ATM contract, CE & PE independent):")
    md.append("    - Option close **>** SMA(13)")
    md.append(f"    - Option close **>** SuperTrend({ST_SIG[0]},{ST_SIG[1]}) value")
    md.append(f"    - Option close **>** SuperTrend({ST_ENT[0]},{ST_ENT[1]}) value")
    md.append("")
    md.append("2. **Entry — Pullback Limit Order:**")
    md.append(f"    - Place limit at signal-candle's SuperTrend({ST_ENT[0]},{ST_ENT[1]}) value")
    md.append("    - Fill if next candle's range covers the limit price")
    md.append(f"    - Cancel if SuperTrend({ST_ENT[0]},{ST_ENT[1]}) flips bearish before fill")
    md.append("")
    md.append("3. **Strike Selection:** ATM only, nearest weekly expiry")
    md.append("")
    md.append("## Risk Management")
    md.append("")
    md.append(f"1. **Fixed % Target** — Locks in profit at **{TP_PCT}%** above entry")
    md.append(f"2. **Fixed % Stop Loss** — Caps loss at **{SL_PCT}%** below entry")
    md.append(f"3. **EOD Force Exit** — All positions flat by **{TRADING_END}**, no overnight risk")
    md.append("4. **Independent CE / PE** — Either side can stop out without affecting the other")
    md.append("")
    md.append("## Key Highlights")
    md.append("")
    md.append("1. **Intraday only** — no overnight gap exposure")
    md.append("2. **Pullback entry** — avoids chasing the candle after signal")
    md.append("3. **CE + PE simultaneous** — captures both directional booms in choppy days")
    md.append("4. **Pure option-buying** — no margin call risk, capital required = premium × lot")
    md.append("")
    md.append("## Strategy Details")
    md.append("")
    md.append("| | |")
    md.append("|---|---|")
    md.append("| **Instrument** | NIFTY |")
    md.append(f"| **Capital Required** | ₹ {CAPITAL_REQUIRED:,} |")
    md.append(f"| **Type** | Intraday (Buy & Sell same day) |")
    md.append(f"| **Trading Window** | {TRADING_START} – {TRADING_END} IST |")
    md.append(f"| **Average Trades per Day (Buy + Sell)** | {stats['avg_trades_per_day']:.1f} |")
    md.append(f"| **Backtest Window** | {START_DATE} → {END_DATE} |")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## Statistics")
    md.append("")
    md.append("| No | Name | Value |")
    md.append("|---|---|---|")
    md.append(f"| 1 | Capital Required | INR {CAPITAL_REQUIRED:,.2f} |")
    md.append(f"| 2 | Total Trading Days | {stats['total_trading_days']} |")
    md.append(f"| 3 | Win Days | {stats['win_days']} |")
    md.append(f"| 4 | Loss Days | {stats['loss_days']} |")
    md.append(f"| 5 | Max Winning Streak Days | {stats['max_winning_streak_days']} |")
    md.append(f"| 6 | Max Losing Streak Days | {stats['max_losing_streak_days']} |")
    md.append(f"| 7 | Win Rate | {pct(stats['win_rate'])} |")
    md.append(f"| 8 | Avg Monthly Profit | {inr(stats['avg_monthly_profit'])} |")
    md.append(f"| 9 | Total Profit | {inr(stats['total_profit'])} |")
    md.append(f"| 10 | Avg Monthly ROI | {pct(stats['avg_monthly_roi_pct'])} |")
    md.append(f"| 11 | Total ROI | {pct(stats['total_roi_pct'])} |")
    md.append(f"| 12 | Standard Deviation (Annualised) | {pct(stats['std_dev_ann'])} |")
    md.append(f"| 13 | Sharpe Ratio (Annualised) | {stats['sharpe']:.2f} |")
    md.append(f"| 14 | Sortino Ratio (Annualised) | {stats['sortino']:.2f} |")
    md.append(f"| 15 | Max Profit in a Day | {inr(stats['max_profit_day'])} |")
    md.append(f"| 16 | Max Loss in a Day | {inr(stats['max_loss_day'])} |")
    md.append(f"| 17 | Avg Profit on Profit Days | {inr(stats['avg_profit_on_profit_days'])} |")
    md.append(f"| 18 | Avg Loss on Loss Days | {inr(stats['avg_loss_on_loss_days'])} |")
    md.append(f"| 19 | Avg no. of trades (Buy + Sell) per trading day | {stats['avg_trades_per_day']:.2f} |")
    md.append(f"| 20 | Max Drawdown | {inr(stats['max_dd_inr'])} |")
    md.append(f"| 21 | Max Drawdown % | {pct(stats['max_dd_pct'])} |")
    md.append(f"| 22 | SL Exits / TP Exits / EOD Exits | {sl_exits} / {tp_exits} / {eod_exits} |")
    md.append("")
    md.append("## Daily Summary")
    md.append("")
    md.append("| Day | Returns (%) | Max profit | Max loss | PNL (Rs) |")
    md.append("|---|---|---|---|---|")
    for _, r in daily_df.iterrows():
        md.append(f"| {r['Day']} | {r['Returns (%)']:.2f} | {r['Max profit']:.2f} | {r['Max loss']:.2f} | {r['PNL (Rs)']:.2f} |")
    md.append("")
    md.append("## Month Wise PNL")
    md.append("")
    md.append("| Month | Total Trades | PNL (Rs) | PNL (%) |")
    md.append("|---|---|---|---|")
    for _, r in month_df.iterrows():
        md.append(f"| {r['Month']} | {int(r['Total Trades'])} | {r['PNL (Rs)']:.2f} | {r['PNL (%)']:.2f} |")
    md.append("")
    md.append("---")
    md.append("")
    md.append(f"_Backtest run on {pd.Timestamp.now().strftime('%Y-%m-%d')} · "
              f"engine: `engine/boom_st_backtest.py` · "
              f"raw trades: `{OUT_CSV.name}`_")
    md.append("")
    return "\n".join(md)


def main():
    df = run_backtest()
    if df.empty:
        print("No trades produced — aborting report.")
        return
    stats = compute_stats(df)
    daily_df = daily_summary(df)
    month_df = month_wise(df)
    md = build_markdown(df, stats, daily_df, month_df)
    OUT_MD.write_text(md)
    print(f"\nReport written to: {OUT_MD}")
    print(f"Trades CSV:        {OUT_CSV}")
    print(f"\nSummary: {len(df)} trades · "
          f"WR {stats['win_rate']:.1f}% · "
          f"Total PnL ₹{stats['total_profit']:,.0f} · "
          f"ROI {stats['total_roi_pct']:.1f}% · "
          f"MaxDD {stats['max_dd_pct']:.1f}%")


if __name__ == "__main__":
    main()
