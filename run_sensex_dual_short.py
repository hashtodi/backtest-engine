"""CLI runner for the SENSEX dual short-premium backtest.

Usage:
    python run_sensex_dual_short.py                       # full window
    python run_sensex_dual_short.py --start 2025-09-01 --end 2025-09-30
"""
import argparse
import logging
import pandas as pd

from engine.sensex_dual_short_backtest import SensexDualShortBacktest, summarize

DEFAULT_START = "2023-05-15"
DEFAULT_END = "2026-04-23"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="SENSEX", choices=["SENSEX", "NIFTY"])
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--dte-mode", default="trading", choices=["trading", "calendar"],
                    dest="dte_mode")
    ap.add_argument("--out", default="sensex_dual_short_trades.csv")
    args = ap.parse_args()

    eng = SensexDualShortBacktest(args.start, args.end, instrument=args.instrument,
                                  dte_mode=args.dte_mode)
    eng.run()

    df = pd.DataFrame(eng.trades)
    df.to_csv(args.out, index=False)

    s = summarize(eng.trades, eng.skips, eng.non_trading_days)
    print(f"\n===== {args.instrument} Dual Short-Premium Backtest =====")
    print(f"Window: {args.start} -> {args.end}")
    print(f"Total P&L:  Rs {s['total_pnl_inr']:,.0f}")
    print(f"  Part 1:   Rs {s['p1_pnl_inr']:,.0f}")
    print(f"  Part 2:   Rs {s['p2_pnl_inr']:,.0f}")
    print(f"  Observed (fully-seen exits): Rs {s['observed_pnl_inr']:,.0f} "
          f"({(1 - s['blind_pnl_pct']) * 100:.1f}%)")
    print(f"  Blind (EOD_LAST_AVAILABLE):  Rs {s['blind_pnl_inr']:,.0f} "
          f"({s['blind_pnl_pct'] * 100:.1f}%) across {s['n_blind_legs']} legs "
          f"[approx: squared off at last-seen price after strike left the +/-10 window]")
    print(f"Legs: {s['n_legs']} | round-trips: {s['n_round_trips']} | re-entries: {s['n_reentries']}")
    print(f"Win rate (per leg): {s['win_rate']*100:.1f}%")
    print(f"Avg win: Rs {s['avg_win_inr']:,.0f} | Avg loss: Rs {s['avg_loss_inr']:,.0f}")
    print(f"Exit reasons: {s['exit_reason_counts']}")
    print(f"Non-trading days (DTE not 0-3): {s['non_trading_days']}")
    print(f"Skip ledger: {s['skips']}")
    print(f"\nTrades written to {args.out} ({len(df)} rows)")


if __name__ == "__main__":
    main()
