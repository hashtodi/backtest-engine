# Volume-Breakout Re-entry Strategy — Design Spec

**Date:** 2026-06-30
**Status:** Draft (building per "go ahead"; assumptions flagged for correction)

## Context

A new entry/exit model for the post-earnings basket (Q2/Q3/Q4-FY26 stock lists with their
earning dates). Instead of the result-day high, the entry **level** is the high of the
**latest pre-earnings daily volume-breakout candle**. Exits use a fixed SL / TP with a
**stepped profit-lock ladder**, plus **one** re-entry after a stop-out, all bounded by an
80-day time cap. Long only, cash equity.

## The Level

- Aggregate 1-min data to **daily** OHLCV per stock (vol = sum, high = max, etc.).
- `volMA = SMA of the prior 75 daily volumes` (the 75 days strictly before each candle).
- A **breakout candle** = a daily candle (date **strictly before** the earning date) whose
  `day_volume > 5 × volMA`.
- **level** = the daily **high** of the **latest** (most recent before earning) breakout
  candle. If none qualifies in available history → skip stock (`NO_LEVEL`).
The breakout-candle high is the **entry trigger**. The actual **entry fill becomes the
BASE** for all SL/TP/lock/re-entry levels.

## Entry (first) — fill = base

- Start the **next trading day after the earning date**; window = up to **80 calendar
  days** after the earning date.
- If that next day's **open < breakout-high** → enter at the **open**; `base = open`.
- Else (open ≥ breakout-high) → wait for the first bar that **CLOSES ≤ breakout-high** →
  enter at the **breakout-high**; `base = breakout-high`.
- If neither happens within 80 days → `NO_ENTRY`.
- `SL = base × 0.95`, `TP = base × 1.08`. Profit-lock ladder (vs base):
  close ≥ 1.05 → SL→1.01; ≥1.06 → 1.02; ≥1.07 → 1.03; ≥1.08 → TP.

  Example: open after earning = 80 → base 80, SL 76, TP 86.4, locks at 84/85→80.8/81.6…
  (everything off 80, not the breakout-high of 100).

## Exit (every entry) — ALL on the 1-min CLOSE, fill at the level

Per 1-min bar after entry, evaluated on the bar **close** (never high/low):

1. **Stop:** if `close ≤ current_sl` → exit at `current_sl` (fill at the exact level).
   `current_sl` starts at `base × 0.95`.
2. **TP:** if `close ≥ base × 1.08` → exit at the TP level.
3. **Ratchet the lock** using this bar's close, after the stop/TP checks: close ≥
   `base×1.07` → `sl = max(sl, base×1.03)`; ≥`1.06` → `1.02`; ≥`1.05` → `1.01`. (Up only.)
4. **Time cap:** at `earning_date + 80 days`, force-exit at the last bar's close on/before
   the cutoff (`TIME`); no entries after the cutoff.

Exit reasons: `SL` (incl. locked-profit stops — reported as SL at the lock level, so a
"SL" can be a +1/2/3% win), `TP`, `TIME`, `OPEN`, `NO_ENTRY`, `NO_LEVEL`, `NO_DATA`.

## Re-entry (max 1, only after a stop-out) — same base

- Only after the **first** position exits via **SL** (−5% or a locked stop). Not after TP /
  TIME / OPEN.
- Re-arm sequence after the stop-out, on the 1-min **close**, within the 80-day window:
  1. a bar must **close below** the stop level (`close < base × 0.95`), then
  2. a later bar must **close above the base** (`close > base`) → **re-enter at `base`**.
- Re-entry uses the **same base** and the same SL/TP/lock rules.
- **At most one** re-entry per stock per quarter.

## Sizing

- Single leg, `capital_per_stock` (default ₹1,00,000) fully deployed per entry;
  `qty = floor(capital / base)`. Re-entry reuses the same capital and base.
- `pnl = qty × (exit − base)`; `pnl_pct = pnl / (qty × base) × 100`.

## Scope

Run on all stocks (with earning dates) in:
- Q4: `quarterly_result_dates_15stocks.csv`
- Q3: `q3_result_dates.csv`
- Q2: `q2_result_dates.csv`

## Architecture

- New engine `engine/volbreakout_reentry_backtest.py` (own dataclass, daily-level
  detection, 1-min execution, re-entry loop). One **trade row per entry** (so a stock can
  produce up to 2 rows: `leg=1` original, `leg=2` re-entry), plus stub rows for
  `NO_LEVEL` / `NO_ENTRY`.
- Runner `run_volbreakout_reentry.py` over a result-date CSV.
- Tests `tests/test_volbreakout_reentry_backtest.py`.

## Resolved conventions (confirmed)

1. 80 days = **calendar** (not trading) days.
2. volMA = SMA of **prior 75** daily volumes, excluding the candle itself; multiple = 5×.
3. Breakout search = **all** history strictly before earning; latest qualifier (no max-age).
4. Sizing = full ₹1,00,000 per entry, single leg.
5. **Base = entry fill** (open-below or breakout-high on a close-touch); SL/TP/locks/re-entry
   all off the base.
6. **All triggers on the 1-min close** (never high/low); exits **fill at the exact level**.
7. Re-entry: close < `base×0.95`, then close > `base` → re-enter at `base`; max 1; only
   after an SL exit.

## Verification

- Unit tests: level detection from synthetic daily volume, entry (open-below vs touch),
  each lock step, locked-SL exit, TP, TIME, the full re-entry sequence (stop → below 95 →
  above 100 → re-enter), max-1 re-entry, NO_LEVEL/NO_ENTRY.
- Per-quarter run + independent reconstruction match on real data; manual trace of one
  stock's level + ladder.
