"""Tests for the Post-Result Reaction Averaging engine.

Logic is exercised with synthetic 1-min frames injected via _load_stock, so no parquet
files are touched. One bar per "day" is enough for most paths; multiple bars are used
where intraday ordering matters.
"""

import datetime
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.result_reaction_backtest import ResultReactionEngine, ResultReactionTrade


def make_df(bars):
    """bars: list of (date 'YYYY-MM-DD', time 'HH:MM', open, high, low, close)."""
    rows = [
        {
            'date': datetime.date.fromisoformat(d),
            'dt_str': f'{d} {t}',
            'open': float(o), 'high': float(h), 'low': float(l), 'close': float(c),
        }
        for d, t, o, h, l, c in bars
    ]
    return pd.DataFrame(rows)


def run(df, result_date, **kw):
    # Default to the original "average-down -10% / SL 15%" scenario so the pre-existing
    # tests stay valid; up-direction tests pass second_entry_pct/sl_pct explicitly.
    kw.setdefault('second_entry_pct', -10.0)
    kw.setdefault('sl_pct', 15.0)
    kw.setdefault('tp_pct', 15.0)
    eng = ResultReactionEngine({'X': result_date}, **kw)
    eng._load_stock = lambda sym: df          # inject synthetic data
    return eng._run_stock('X', result_date)


# Default sizing: capital 100000 -> 50000/leg.
CAP = 100000.0


def test_entry1_next_day_open_then_tp():
    # Result day high = 100. Next day opens at 98 (entry1). Price rallies to TP (112.7).
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),   # result_session, high=100
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1 = min(98,100)=98
        ('2026-01-05', '09:15', 100, 113, 100, 112), # high 113 >= TP 112.7 -> TP
    ])
    t = run(df, '2026-01-01')
    assert t.exit_reason == 'TP'
    assert t.marked_high == 100.0
    assert t.entry1_price == 98.0
    assert t.qty1 == int(50000 // 98)          # 510
    assert t.qty2 == 0
    assert t.avg_price == 98.0
    assert t.tp_price == pytest.approx(112.7, abs=0.01)
    assert t.exit_price == pytest.approx(112.7, abs=0.01)
    assert t.pnl == pytest.approx(t.qty1 * (112.7 - 98), abs=0.5)


def test_entry2_fill_then_tp_reanchors_average():
    # entry1=98 (q1=510), level2=88.2; dip fills entry2=88.2 (q2=566).
    # qty-weighted avg = (510*98 + 566*88.2)/1076 = 92.845; TP = 106.77.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98
        ('2026-01-05', '09:15', 96, 96, 88, 90),     # low 88 <= 88.2 -> entry2=88.2
        ('2026-01-06', '09:15', 95, 108, 95, 107),   # high 108 >= TP 106.77 -> TP
    ])
    t = run(df, '2026-01-01')
    assert t.entry1_price == 98.0
    assert t.entry2_price == pytest.approx(88.2, abs=0.01)   # filled at level (open 96 > level)
    assert t.qty1 == 510
    assert t.qty2 == 566
    assert t.avg_price == pytest.approx(92.845, abs=0.01)
    assert t.exit_reason == 'TP'
    assert t.tp_price == pytest.approx(106.772, abs=0.01)


def test_entry2_then_sl():
    # entry1=98 (q1=510), entry2=88.2 (q2=566); qty-wtd avg=92.845, SL=78.92.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98
        ('2026-01-05', '09:15', 96, 96, 88, 90),     # entry2=88.2
        ('2026-01-06', '09:15', 85, 86, 78, 79),     # low 78 <= SL 78.92 -> SL
    ])
    t = run(df, '2026-01-01')
    assert t.exit_reason == 'SL'
    assert t.avg_price == pytest.approx(92.845, abs=0.01)
    assert t.sl_price == pytest.approx(78.918, abs=0.01)
    assert t.exit_price == pytest.approx(78.918, abs=0.01)   # open 85 > SL, fill at SL
    assert t.pnl < 0


def test_entry1_delayed_when_gap_up_above_high():
    # Price stays above the marked high (100) for two days, then dips on day 3.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),    # high=100
        ('2026-01-02', '09:15', 105, 107, 101, 106),  # all above 100 -> no entry
        ('2026-01-05', '09:15', 104, 106, 102, 105),  # all above 100 -> no entry
        ('2026-01-06', '09:15', 103, 104, 98, 99),    # low 98 < 100 -> entry1=min(103,100)=100
        ('2026-01-07', '09:15', 100, 116, 100, 115),  # TP 100*1.15=115 -> TP
    ])
    t = run(df, '2026-01-01')
    assert t.observation_start == '2026-01-02'
    assert t.entry1_time.startswith('2026-01-06')
    assert t.entry1_price == 100.0
    assert t.exit_reason == 'TP'


def test_entry2_gap_down_fills_at_open():
    # entry1=98, level2=88.2; a day GAPS open to 85 (< level2) -> entry2 fills at 85.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98
        ('2026-01-05', '09:15', 85, 90, 84, 89),     # open 85 < 88.2 -> entry2=85
        ('2026-01-06', '09:15', 95, 110, 95, 109),   # avg=91.5 TP=105.225 -> TP
    ])
    t = run(df, '2026-01-01')
    assert t.entry2_price == 85.0
    # q1=510, q2=floor(50000/85)=588; avg=(510*98+588*85)/1098=91.038
    assert t.avg_price == pytest.approx(91.038, abs=0.01)
    assert t.exit_reason == 'TP'


def test_open_at_end_of_data():
    # entry1=98, price meanders between SL(83.3) and TP(112.7), never hits -> OPEN.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98 (no entry2: low never <=88.2)
        ('2026-01-05', '09:15', 99, 105, 95, 101),
        ('2026-01-06', '09:15', 101, 104, 99, 100),  # last close 100 -> OPEN mark-to-market
    ])
    t = run(df, '2026-01-01')
    assert t.exit_reason == 'OPEN'
    assert t.qty2 == 0
    assert t.exit_price == 100.0
    assert t.exit_time.startswith('2026-01-06')


def test_non_trading_result_date_uses_prev_session_high():
    # Result date = Sunday 2026-01-04 (no bars). Prev session Fri 01-02 high=100.
    df = make_df([
        ('2026-01-02', '09:15', 95, 100, 94, 99),    # Friday -> result_session, high=100
        ('2026-01-05', '09:15', 98, 99, 97, 98),     # Monday -> observation, entry1=98
        ('2026-01-06', '09:15', 100, 113, 100, 112), # TP
    ])
    t = run(df, '2026-01-04')
    assert t.result_session == '2026-01-02'
    assert t.observation_start == '2026-01-05'
    assert t.marked_high == 100.0
    assert t.entry1_price == 98.0


def test_no_entry_when_price_never_below_high():
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),    # high=100
        ('2026-01-02', '09:15', 101, 105, 100, 104),  # low 100 not < 100
        ('2026-01-05', '09:15', 106, 110, 101, 108),  # stays above
    ])
    t = run(df, '2026-01-01')
    assert t.exit_reason == 'NO_ENTRY'
    assert t.pnl == 0.0


def test_no_data_after_result_date():
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
    ])
    t = run(df, '2026-01-01')   # nothing strictly after 2026-01-01
    assert t.exit_reason == 'NO_DATA'


def test_tp_gap_up_fills_at_open():
    # entry1=98, TP=112.7; a bar opens at 115 (gap above TP) -> exit at 115.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98
        ('2026-01-05', '09:15', 115, 118, 114, 117),  # open 115 >= TP 112.7 -> exit 115
    ])
    t = run(df, '2026-01-01')
    assert t.exit_reason == 'TP'
    assert t.exit_price == 115.0


def test_max_hold_time_exit():
    # Cap 10 calendar days from result (2026-01-01) -> cutoff 2026-01-11. No SL/TP hit;
    # data exists beyond cutoff -> force exit at last bar <= cutoff (2026-01-09) close.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98
        ('2026-01-05', '09:15', 99, 103, 96, 100),
        ('2026-01-09', '09:15', 100, 104, 97, 101),  # last bar <= cutoff -> TIME exit, close 101
        ('2026-01-12', '09:15', 101, 105, 98, 102),  # beyond cutoff (makes it capped)
        ('2026-01-15', '09:15', 102, 106, 99, 103),
    ])
    t = run(df, '2026-01-01', max_hold_days=10)
    assert t.exit_reason == 'TIME'
    assert t.exit_time.startswith('2026-01-09')
    assert t.exit_price == 101.0


def test_max_hold_does_not_override_earlier_tp():
    # TP on 2026-01-05 is well before the 30-day cutoff -> TP wins, not TIME.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98
        ('2026-01-05', '09:15', 100, 113, 100, 112), # TP 112.7
        ('2026-01-20', '09:15', 100, 101, 99, 100),
    ])
    t = run(df, '2026-01-01', max_hold_days=30)
    assert t.exit_reason == 'TP'
    assert t.exit_time.startswith('2026-01-05')


def test_max_hold_open_when_data_ends_before_cutoff():
    # Cap 100 days but data ends 2026-01-06 (before cutoff) -> still OPEN, not TIME.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98
        ('2026-01-06', '09:15', 99, 104, 96, 100),   # last bar, close 100
    ])
    t = run(df, '2026-01-01', max_hold_days=100)
    assert t.exit_reason == 'OPEN'
    assert t.exit_price == 100.0


def test_entry2_pyramid_up_fill_then_tp():
    # NEW strategy: second_entry +10% (pyramid up), SL 10%.
    # entry1=98 (q1=510), level2=98*1.10=107.8. Price rises -> entry2 fills at 107.8
    # (q2=463). qty-wtd avg=102.66; SL=92.40 (10%), TP=118.06 (15%).
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98
        ('2026-01-05', '09:15', 100, 108, 99, 107),  # high 108 >= 107.8 -> entry2=107.8
        ('2026-01-06', '09:15', 108, 119, 107, 118), # high 119 >= TP 118.06 -> TP
    ])
    t = run(df, '2026-01-01', second_entry_pct=10.0, sl_pct=10.0)
    assert t.entry2_price == pytest.approx(107.8, abs=0.01)
    assert t.qty1 == 510
    assert t.qty2 == 463
    assert t.avg_price == pytest.approx(102.663, abs=0.01)
    assert t.sl_price == pytest.approx(92.397, abs=0.01)
    assert t.tp_price == pytest.approx(118.063, abs=0.01)
    assert t.exit_reason == 'TP'


def test_entry2_up_gap_fills_at_open():
    # Bar gaps open ABOVE level2 (110 > 107.8) -> entry2 fills at the open, 110.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98
        ('2026-01-05', '09:15', 110, 112, 109, 111),  # open 110 >= 107.8 -> entry2=110
        ('2026-01-06', '09:15', 115, 121, 114, 120),  # avg 103.65, TP 119.2 -> TP
    ])
    t = run(df, '2026-01-01', second_entry_pct=10.0, sl_pct=10.0)
    assert t.entry2_price == 110.0
    assert t.avg_price == pytest.approx(103.652, abs=0.02)
    assert t.exit_reason == 'TP'


def test_up_strategy_sl_before_entry2():
    # Price drops first: SL (10% -> 88.2) hits before the +10% entry-2 (107.8) is reached.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98, SL=88.2
        ('2026-01-05', '09:15', 96, 97, 87, 88),     # low 87 <= 88.2 -> SL, entry2 never fills
    ])
    t = run(df, '2026-01-01', second_entry_pct=10.0, sl_pct=10.0)
    assert t.exit_reason == 'SL'
    assert t.qty2 == 0
    assert t.sl_price == pytest.approx(88.2, abs=0.01)
    assert t.exit_price == pytest.approx(88.2, abs=0.01)


def test_entry2_fill_defers_exit_to_next_bar():
    # entry2 fills on 01-05; exits are deferred so SL resolves on 01-06 (not same bar).
    # entry1=98 (q1=510), level2=88.2. 01-05 low 80 -> entry2=min(88,88.2)=88 (q2=568).
    # qty-wtd avg=92.731, SL=78.82. 01-06 low 70 <= 78.82 -> SL on the NEXT bar.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98
        ('2026-01-05', '09:15', 88, 89, 80, 82),     # entry2 fills, exit deferred
        ('2026-01-06', '09:15', 79, 80, 70, 72),     # SL here
    ])
    t = run(df, '2026-01-01')
    assert t.entry2_price == 88.0
    assert t.avg_price == pytest.approx(92.731, abs=0.01)
    assert t.exit_reason == 'SL'
    assert t.exit_time.startswith('2026-01-06')
    assert t.sl_price == pytest.approx(78.821, abs=0.01)


def test_no_sl_holds_through_drop():
    # NEW: second -20%, no SL, TP-after-2nd 0%. A drop that doesn't reach -20% does NOT
    # exit (SL disabled); position rides to OPEN at end of data.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98, level2=78.4, tp=112.7
        ('2026-01-05', '09:15', 90, 91, 85, 86),     # low 85 > 78.4 (no entry2), no SL -> hold
        ('2026-01-06', '09:15', 86, 90, 84, 89),     # last close 89 -> OPEN
    ])
    t = run(df, '2026-01-01', second_entry_pct=-20.0, sl_pct=None, tp_pct=15.0, tp_pct_after_second=0.0)
    assert t.exit_reason == 'OPEN'
    assert t.qty2 == 0
    assert t.sl_price == 0.0
    assert t.exit_price == 89.0


def test_tp_after_second_breakeven_with_defer():
    # NEW config: entry1=98, entry2 at -20% (78.4), no SL, TP-after-2nd = 0% (=avg/breakeven).
    # 01-05 fills entry2 AND its high (96) already exceeds the new breakeven TP (~87.1) ->
    # must NOT exit on 01-05 (deferred); exits on 01-06 when high crosses the breakeven.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),     # entry1=98 (q1=510)
        ('2026-01-05', '09:15', 95, 96, 78, 79),     # entry2=78.4 (q2=637); high 96 > TP but DEFERRED
        ('2026-01-06', '09:15', 80, 88, 79, 87),     # high 88 >= breakeven 87.12 -> TP
    ])
    t = run(df, '2026-01-01', second_entry_pct=-20.0, sl_pct=None, tp_pct=15.0, tp_pct_after_second=0.0)
    assert t.entry2_price == pytest.approx(78.4, abs=0.01)
    assert t.avg_price == pytest.approx(87.115, abs=0.02)
    assert t.tp_price == pytest.approx(87.115, abs=0.02)   # 0% above avg
    assert t.sl_price == 0.0
    assert t.exit_reason == 'TP'
    assert t.exit_time.startswith('2026-01-06')            # proves the 01-05 defer


def test_trailing_stop_fires_from_peak():
    # No fixed SL, single entry, trail 10%. entry1=98. Price peaks at 130 then falls;
    # trailing level = peak*0.90 = 117. Peak uses PRIOR-bar highs (no intrabar look-ahead).
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),      # entry1=98, peak=98
        ('2026-01-05', '09:15', 100, 130, 100, 128),  # peak rises to 130 (updated after exit check)
        ('2026-01-06', '09:15', 125, 126, 110, 112),  # trail=130*0.90=117; low 110<=117 -> TSL@117
    ])
    t = run(df, '2026-01-01', second_entry_pct=None, sl_pct=None, trail_pct=10.0, tp_pct=100.0)
    assert t.exit_reason == 'TSL'
    assert t.exit_time.startswith('2026-01-06')
    assert t.exit_price == pytest.approx(117.0, abs=0.01)
    assert t.qty2 == 0


def test_trailing_tighter_than_fixed_sl_wins():
    # Both stops on; after a run-up the trailing level sits ABOVE the fixed SL, so it binds.
    # entry1=100, fixed SL=90 (10%); peak 130 -> trail(15%)=110.5 -> TSL at 110.5.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 100, 101, 99, 100),   # entry1=100, SL=90
        ('2026-01-05', '09:15', 105, 130, 104, 128),  # peak 130
        ('2026-01-06', '09:15', 120, 121, 108, 109),  # trail=130*0.85=110.5; low108<=110.5 -> TSL
    ])
    t = run(df, '2026-01-01', second_entry_pct=None, sl_pct=10.0, trail_pct=15.0, tp_pct=100.0)
    assert t.exit_reason == 'TSL'
    assert t.exit_price == pytest.approx(110.5, abs=0.01)


def test_single_entry_no_second_leg():
    # second_entry_pct=None -> only one leg ever; TP off entry1.
    df = make_df([
        ('2026-01-01', '09:15', 95, 100, 94, 99),
        ('2026-01-02', '09:15', 98, 99, 97, 98),      # entry1=98
        ('2026-01-05', '09:15', 90, 91, 80, 81),      # -20% zone, but no 2nd entry configured
        ('2026-01-06', '09:15', 100, 113, 100, 112),  # TP 98*1.15=112.7
    ])
    t = run(df, '2026-01-01', second_entry_pct=None, sl_pct=None, tp_pct=15.0)
    assert t.qty2 == 0
    assert t.entry2_price == 0.0
    assert t.avg_price == 98.0
    assert t.exit_reason == 'TP'
    assert t.exit_price == pytest.approx(112.7, abs=0.01)
