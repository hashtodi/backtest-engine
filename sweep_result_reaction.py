"""
Grid sweep for the Post-Result Reaction strategy across BOTH quarters.

Scores each parameter combo by the WORSE of the two quarters' deployed return
(tiebreak = sum), so a config that wins one quarter but bombs the other ranks low.

Data is preloaded once per stock and reused across all combos.

WARNING: ~15 (Q4) + ~12 (Q3) trades total. A grid search over this little data
overfits badly — treat the winner as a hypothesis for more quarters, not a proven edge.
"""

import csv
import itertools
import os

import pandas as pd

from engine.result_reaction_backtest import ResultReactionEngine

Q4_CSV = 'quarterly_result_dates_15stocks.csv'
Q3_CSV = 'q3_result_dates.csv'
DATE_COLS = ('last_result_date', 'q3_result_date', 'result_date', 'date')


def load_dates(path):
    out = {}
    for row in csv.DictReader(open(path)):
        t = (row.get('ticker') or '').strip()
        d = next((row[c].strip() for c in DATE_COLS if row.get(c)), '')
        if t and d:
            out[t] = d
    return out


def preload(symbols):
    """Process each stock's parquet once via the engine loader; cache the frame."""
    loader = ResultReactionEngine({})
    cache = {}
    for s in symbols:
        cache[s] = loader._load_stock(s)
    return cache


def quarter_return(result_dates, cache, params):
    eng = ResultReactionEngine(result_dates, **params)
    eng._load_stock = lambda s: cache.get(s)
    trades = eng.run()
    traded = [t for t in trades if t.exit_reason in ('SL', 'TSL', 'TP', 'TIME', 'OPEN')]
    if not traded:
        return None
    pnl = sum(t.pnl for t in traded)
    invested = sum(t.qty1 * t.entry1_price + t.qty2 * t.entry2_price for t in traded)
    ret = (pnl / invested * 100) if invested else 0.0
    worst = min((t.pnl_pct for t in traded), default=0.0)
    return dict(n=len(traded), pnl=pnl, invested=invested, ret=ret, worst=worst)


GRID = dict(
    second_entry_pct=[None, -20, -15, -10, -5, 10],
    sl_pct=[None, 8, 10, 15],
    trail_pct=[None, 5, 8, 10, 15],
    tp_pct=[10, 15, 20],
    tp_pct_after_second=[None, 0],
    max_hold_days=[30, 60, 75, None],
)


def combos():
    keys = list(GRID)
    seen = set()
    for vals in itertools.product(*(GRID[k] for k in keys)):
        p = dict(zip(keys, vals))
        # tp_pct_after_second only matters when a 2nd entry exists
        if p['second_entry_pct'] is None:
            p['tp_pct_after_second'] = None
        # skip degenerate "no exit at all" configs (no SL, no trail, no time cap, huge TP-less)
        key = tuple(sorted(p.items(), key=lambda kv: kv[0]))
        if key in seen:
            continue
        seen.add(key)
        yield p


def main():
    q4 = load_dates(Q4_CSV)
    q3 = load_dates(Q3_CSV)
    cache = preload(sorted(set(q4) | set(q3)))
    print(f'Preloaded {len(cache)} stocks. Sweeping...')

    rows = []
    all_combos = list(combos())
    for i, p in enumerate(all_combos):
        if i % 200 == 0:
            print(f'  {i}/{len(all_combos)}', end='\r')
        r4 = quarter_return(q4, cache, p)
        r3 = quarter_return(q3, cache, p)
        if not r4 or not r3:
            continue
        rows.append({
            **p,
            'q4_ret': round(r4['ret'], 2), 'q3_ret': round(r3['ret'], 2),
            'q4_pnl': round(r4['pnl']), 'q3_pnl': round(r3['pnl']),
            'q4_n': r4['n'], 'q3_n': r3['n'],
            'worst_trade_pct': round(min(r4['worst'], r3['worst']), 1),
            'score_worse': round(min(r4['ret'], r3['ret']), 2),
            'sum_ret': round(r4['ret'] + r3['ret'], 2),
        })
    print(f'\nEvaluated {len(rows)} combos.')

    df = pd.DataFrame(rows).sort_values(['score_worse', 'sum_ret'], ascending=False)
    df.to_csv('sweep_result_reaction_results.csv', index=False)

    show = ['second_entry_pct', 'sl_pct', 'trail_pct', 'tp_pct', 'tp_pct_after_second',
            'max_hold_days', 'q4_ret', 'q3_ret', 'score_worse', 'sum_ret', 'worst_trade_pct']
    pd.set_option('display.width', 240); pd.set_option('display.max_columns', 30)
    print('\n===== TOP 15 by worse-quarter return =====')
    print(df[show].head(15).to_string(index=False))
    print('\n===== TOP 10 by SUM of both quarters =====')
    print(df.sort_values('sum_ret', ascending=False)[show].head(10).to_string(index=False))
    print(f'\nFull leaderboard: {os.path.abspath("sweep_result_reaction_results.csv")}')


if __name__ == '__main__':
    main()
