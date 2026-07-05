"""Run the IV/HV-ratio iron condor backtest.

    python run_iv_hv_condor.py --start 2020-08-03 --end 2026-05-22 --out condor_trades.csv
"""
import argparse
import json

from engine.iv_hv_iron_condor_backtest import run, DEFAULT_OPTIONS_PATH, DEFAULT_SPOT_PATH


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-08-03")
    ap.add_argument("--end", default="2026-05-22")
    ap.add_argument("--config", default="saved_strategies/iv_hv_iron_condor.json")
    ap.add_argument("--out", default="iv_hv_condor_trades.csv")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = dict(json.load(f))
    cfg["backtest_start"] = args.start
    cfg["backtest_end"] = args.end
    print(f"Loading options {args.start}..{args.end} ...")
    result = run(cfg, options_path=DEFAULT_OPTIONS_PATH, spot_path=DEFAULT_SPOT_PATH)
    s, stats = result["summary"], result["stats"]
    print(f"  {result['days_processed']} trading days | {stats['signals']} signals | "
          f"{s['all_trades']} trades executed")
    print(f"\nRELIABLE trades (headline): {s['total_trades']}  Win%: {s['win_rate']*100:.1f}  "
          f"Total P&L: Rs {s['total_pnl_inr']:,.0f}  MaxDD: Rs {s['max_drawdown_inr']:,.0f}")
    print(f"Exit mix: {s['exit_reason_counts']}")
    print(f"EXCLUDED (a leg left the +/-10 window -> unreliable fill): "
          f"{s['excluded_fallback_trades']} trades, freeze-P&L Rs {s['excluded_fallback_pnl_inr']:,.0f}")
    print("  ^ excluded trades skew to large-move days, so the headline is OPTIMISTIC, not all-in.")
    print(f"Skipped signals: overlap {stats['skipped_overlap']}, no-legs {stats['skipped_no_legs']}, "
          f"un-formable {stats['skipped_unformable']}, low-credit {stats['skipped_low_credit']}")
    print(f"Sanity-flagged (in reliable set): {s['sanity_flagged']}  "
          f"(filtered P&L: Rs {s['total_pnl_inr_sanity_filtered']:,.0f})")
    result["trades_df"].to_csv(args.out, index=False)
    print(f"Wrote {args.out}  (fill_fallback column marks excluded trades)")


if __name__ == "__main__":
    main()
