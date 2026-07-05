"""
Run the Post-Result Reaction Averaging backtest over the result-date CSV.

Usage:
    python run_result_reaction_backtest.py
    python run_result_reaction_backtest.py --csv quarterly_result_dates_15stocks.csv \
        --capital 100000 --avg-down 10 --sl 15 --tp 15 --out result_reaction_trades.csv
"""

import argparse
import csv as csvmod
import os

from engine.result_reaction_backtest import ResultReactionEngine, trades_to_dataframe


DATE_COLS = ('last_result_date', 'q3_result_date', 'q2_result_date', 'result_date', 'date')


def load_result_dates(path: str) -> dict:
    out = {}
    with open(path) as f:
        for row in csvmod.DictReader(f):
            t = (row.get('ticker') or '').strip()
            d = ''
            for c in DATE_COLS:
                if row.get(c):
                    d = row[c].strip()
                    break
            if t and d:
                out[t] = d
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default='quarterly_result_dates_15stocks.csv')
    ap.add_argument('--capital', type=float, default=100000.0)
    ap.add_argument('--second-entry', type=float, default=10.0,
                    help='signed %% offset of entry-2 from entry-1 (+10 = pyramid up, -10 = average down)')
    ap.add_argument('--no-second-entry', action='store_true', help='single entry only (no 2nd leg)')
    ap.add_argument('--sl', type=float, default=10.0)
    ap.add_argument('--no-sl', action='store_true', help='disable the fixed stop-loss')
    ap.add_argument('--trail', type=float, default=None, help='%% trailing stop from the post-entry peak')
    ap.add_argument('--tp', type=float, default=15.0)
    ap.add_argument('--tp-after-second', type=float, default=None,
                    help='TP%% to use after the 2nd entry fills (e.g. 0 = exit at breakeven/avg)')
    ap.add_argument('--max-hold-days', type=int, default=0,
                    help='calendar days from result date to force-exit (0 = no cap)')
    ap.add_argument('--out', default='result_reaction_trades.csv')
    args = ap.parse_args()

    result_dates = load_result_dates(args.csv)
    print(f'Loaded {len(result_dates)} stocks from {args.csv}')

    eng = ResultReactionEngine(
        result_dates,
        capital_per_stock=args.capital,
        second_entry_pct=(None if args.no_second_entry else args.second_entry),
        sl_pct=(None if args.no_sl else args.sl),
        trail_pct=args.trail,
        tp_pct=args.tp,
        tp_pct_after_second=args.tp_after_second,
        max_hold_days=(args.max_hold_days or None),
    )

    def progress(i, n, sym):
        print(f'  [{i + 1}/{n}] {sym}', end='\r')

    trades = eng.run(progress_callback=progress)
    print()

    df = trades_to_dataframe(trades)
    df.to_csv(args.out, index=False)

    # ---- Summary ----
    traded = df[df['exit_reason'].isin(['SL', 'TSL', 'TP', 'TIME', 'OPEN'])]
    skipped = df[df['exit_reason'].isin(['NO_ENTRY', 'NO_DATA'])]

    print('\n================ SUMMARY ================')
    print(f'Stocks               : {len(df)}')
    print(f'Traded (entry filled): {len(traded)}')
    print(f'  TP                  : {(df.exit_reason == "TP").sum()}')
    print(f'  SL                  : {(df.exit_reason == "SL").sum()}')
    print(f'  TSL (trailing)      : {(df.exit_reason == "TSL").sum()}')
    print(f'  TIME (max-hold)     : {(df.exit_reason == "TIME").sum()}')
    print(f'  Still OPEN          : {(df.exit_reason == "OPEN").sum()}')
    print(f'  Entry-2 filled      : {(df.qty2 > 0).sum()}')
    print(f'Skipped              : {len(skipped)} {list(skipped.symbol) if len(skipped) else ""}')
    if len(traded):
        total = traded['pnl'].sum()
        invested = (traded['qty1'] * traded['entry1_price']
                    + traded['qty2'] * traded['entry2_price']).sum()
        wins = (traded['pnl'] > 0).sum()
        print(f'Total P&L            : Rs {total:,.0f}')
        print(f'Capital deployed     : Rs {invested:,.0f}')
        print(f'Return on deployed   : {total / invested * 100:.2f}%' if invested else '')
        print(f'Win rate             : {wins}/{len(traded)} = {wins / len(traded) * 100:.0f}%')
        print(f'Avg P&L / trade      : Rs {traded["pnl"].mean():,.0f}')
    print(f'\nWrote {os.path.abspath(args.out)}')


if __name__ == '__main__':
    main()
