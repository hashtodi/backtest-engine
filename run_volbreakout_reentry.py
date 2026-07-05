"""
Run the Volume-Breakout Re-entry strategy over a result-date CSV (any quarter).

    python run_volbreakout_reentry.py --csv q2_result_dates.csv --out q2_volbreakout.csv
"""

import argparse
import csv as csvmod
import os

from engine.volbreakout_reentry_backtest import VolBreakoutReentryEngine, trades_to_dataframe

DATE_COLS = ('last_result_date', 'q3_result_date', 'q2_result_date', 'result_date', 'date')


def load_dates(path):
    out = {}
    for row in csvmod.DictReader(open(path)):
        t = (row.get('ticker') or '').strip()
        d = next((row[c].strip() for c in DATE_COLS if row.get(c)), '')
        if t and d:
            out[t] = d
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', required=True)
    ap.add_argument('--capital', type=float, default=100000.0)
    ap.add_argument('--vol-ma', type=int, default=75)
    ap.add_argument('--vol-mult', type=float, default=5.0)
    ap.add_argument('--sl', type=float, default=5.0)
    ap.add_argument('--tp', type=float, default=8.0)
    ap.add_argument('--max-hold-days', type=int, default=80)
    ap.add_argument('--out', default='volbreakout_trades.csv')
    args = ap.parse_args()

    dates = load_dates(args.csv)
    eng = VolBreakoutReentryEngine(
        dates, capital_per_stock=args.capital, vol_ma_period=args.vol_ma,
        vol_mult=args.vol_mult, sl_pct=args.sl, tp_pct=args.tp, max_hold_days=args.max_hold_days,
    )
    trades = eng.run(progress_callback=lambda i, n, s: print(f'  [{i+1}/{n}] {s}', end='\r'))
    print()
    df = trades_to_dataframe(trades)
    df.to_csv(args.out, index=False)

    entries = df[df.leg > 0]
    traded = entries[entries.exit_reason.isin(['SL', 'TP', 'TIME', 'OPEN'])]
    print('\n================ SUMMARY ================')
    print(f'Stocks                : {df.symbol.nunique()}')
    print(f'Entries (legs)        : {len(traded)}  (leg1={len(traded[traded.leg==1])}, re-entries={len(traded[traded.leg>=2])})')
    for r in ['TP', 'SL', 'TIME', 'OPEN']:
        print(f'  {r:<5}               : {(entries.exit_reason==r).sum()}')
    skipped = df[df.exit_reason.isin(['NO_ENTRY', 'NO_LEVEL', 'NO_DATA'])]
    print(f'  skipped             : {len(skipped)} {sorted(set(skipped.symbol))}')
    if len(traded):
        pnl = traded.pnl.sum()
        invested = (traded.qty * traded.entry_price).sum()
        wins = (traded.pnl > 0).sum()
        print(f'Total P&L             : Rs {pnl:,.0f}')
        print(f'Capital deployed      : Rs {invested:,.0f}')
        print(f'Return on deployed    : {pnl/invested*100:.2f}%' if invested else '')
        print(f'Win rate (legs)       : {wins}/{len(traded)} = {wins/len(traded)*100:.0f}%')
    print(f'\nWrote {os.path.abspath(args.out)}')


if __name__ == '__main__':
    main()
