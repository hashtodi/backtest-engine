# Post-Result Reaction Averaging — Design Spec

**Date:** 2026-06-26
**Status:** Draft for review
**Author:** brainstormed with Harsh

## Context

We want to test a discretionary idea on a small basket of cash-equity names: after a
company declares quarterly results, the result-day high acts as a reference level. The
day after results we begin scaling into a long position, average down once if the stock
falls 10% from the first fill, and exit on a symmetric ±15% band around the average
entry price.

The universe is the **15 stocks** that exist in `data/stocks/` AND appear in the
quarterly-results list, captured in `quarterly_result_dates_15stocks.csv` (built earlier
this session from the CMOTS `Eventdatewisedetails` corporate-action feed). Each stock has
one Q4-FY26 result date (16 Apr – 29 May 2026).

**Data dependency — RESOLVED (2026-06-26):** the 1-min parquet files were refreshed and
now run through `2026-06-25`. All 15 stocks have **19–48 trading days of data after their
result date** (verified), so every name is tradeable. The engine must still gracefully
**skip + flag** any stock whose data does not reach its observation window, for re-runs on
other quarters.

## Universe & Inputs

- **Instrument:** cash equity (spot), **long only**.
- **Stocks (15):** EMMVEE, NEULANDLAB, RAILTEL, NAVINFLUOR, VIJAYA, ANGELONE, CRAFTSMAN,
  SHYAMMETL, MINDACORP, SBFC, CGCL, HFCL, IIFL, POONAWALLA, NSLNISP.
- **Result dates:** read from `quarterly_result_dates_15stocks.csv`
  (columns: `ticker, co_code, company_name, last_result_date, exchange, note`).
- **Price data:** `data/stocks/<TICKER>/<TICKER>_1m.parquet`
  (columns `ts, datetime, open, high, low, close, volume`; IST, start-stamped 1-min bars).
- **Scope:** latest quarter only (one trade per stock, ~15 trades) — a proof-of-concept /
  sanity run, not a statistically powered backtest.

## Strategy Logic

For each stock with result date `R`:

### 1. Reference high (the "marked high")
- `result_session` = the trading session dated `R`. If `R` is a non-trading day
  (weekend/holiday — no bars exist for `R`), use the **previous trading day**.
- `marked_high` = `max(high)` over all 1-min bars of `result_session`.
- `observation_start` = the **first trading day strictly after `R`**.
- Note: results are frequently declared after market hours, so `marked_high` is the
  pre-news session high — intentional (we trade the next-day reaction).

### 2. Entry-1 (50% leg) — "first bar below the marked high"
- Walk 1-min bars from `observation_start` 09:15 onward.
- Trigger: the **first bar where price dips below `marked_high`**.
- **No time cap on the wait.** We monitor minute-by-minute indefinitely and enter at the
  *exact bar* the condition first becomes true. This may be:
  - the **next-day open** (common case — the day after results usually opens below the
    result-day high), or
  - **10 / 30 / 50+ trading days later** (when the stock opens above the result-day high
    and keeps trading above it until it eventually pulls back below).
- Fill price `entry1 = min(bar.open, marked_high)`:
  - bar opens below the high → fill at the open (the exact minute it was already below);
  - bar opens above the high but trades below intrabar → fill at `marked_high` (the level
    as price crosses down through it that minute).
- If price never goes below `marked_high` within available data → **no trade**
  (exit_reason `NO_ENTRY`).

### 3. Entry-2 (50% leg) — signed offset off entry-1
- Level `entry2_level = entry1 × (1 + second_entry_pct/100)`, a **signed** offset off
  entry-1 (not off the marked high). Current strategy: **+10% (pyramid up)**.
  - **Up (+):** fills on a rise — `bar.high >= entry2_level` → `entry2 = entry2_level`;
    if a session gaps open above the level → `entry2 = bar.open`.
  - **Down (−):** fills on a drop — `bar.low <= entry2_level` → `entry2 = entry2_level`;
    if it gaps open below → `entry2 = bar.open`.
- The entry-2 order stays live until the position closes.

### 4. Average price & exits
- `avg = entry1` if only entry-1 filled; else the **quantity-weighted (cost-basis)
  average** `(qty1×entry1 + qty2×entry2) / (qty1 + qty2)`. Because each leg deploys equal
  rupees, the cheaper second leg holds more shares, so the weighted average sits below the
  simple mean of the two prices.
- `TP = avg × (1 + tp_pct/100)` (+15% of avg). `SL = avg × (1 − sl_pct/100)` **if a stop
  is enabled**; `sl_pct = None` disables the stop (TP/time exits only).
- **Re-anchor on the 2nd entry:** when entry-2 fills, `avg` updates and TP/SL recompute.
  TP can switch to `tp_pct_after_second` (e.g. **0% = exit at the average / breakeven**),
  modelling "average down, then scratch out at cost".
- Exit detection (long), per 1-min bar (after entry-2 has been processed):
  - SL hit (if enabled) if `bar.low <= SL` → fill at `SL` (or `bar.open` if it gaps below);
  - TP hit if `bar.high >= TP` → fill at `TP` (or `bar.open` if it gaps above).
- **Intra-bar ordering:** on the bar where entry-2 fills, exits are **deferred to the next
  bar** — we never resolve the re-anchored TP/SL on the same minute the 2nd leg fills,
  since intrabar order is unknown (this matters when the breakeven TP sits just above the
  2nd-entry fill). If a later bar would hit both SL and TP, assume **SL first**.
- **Hold across days.** Optional `max_hold_days` cap (calendar days from the result date):
  if neither SL nor TP fires by `result_date + max_hold_days`, force-exit at the last bar
  on/before the cutoff at its close (exit_reason `TIME`). With no cap (or if the data ends
  before the cutoff), a still-open position closes as `OPEN`, marked-to-market at the last
  `close`. Entry-1 must also occur within the capped window, else `NO_ENTRY`.

### 5. Position sizing
- Fixed rupee notional per stock, `capital_per_stock` (default ₹1,00,000), split 50/50.
- `leg_capital = capital_per_stock / 2`.
- `qty1 = floor(leg_capital / entry1)`, `qty2 = floor(leg_capital / entry2)`
  (equal rupee per leg → unequal share counts).
- If only entry-1 fills, position = `qty1`.
- `pnl = Σ_legs qty_leg × (exit − entry_leg)`; `pnl_pct = pnl / rupees_invested × 100`.

## Worked Example

`marked_high = 100`, next day opens at 98:
- Entry-1: 98 (first bar below 100). `qty1 = floor(50000/98) = 510`.
- Entry-2 level: `98 × 0.90 = 88.2`. Price later dips to 88.2 → fill. `qty2 = floor(50000/88.2) = 566`.
- `avg = (510×98 + 566×88.2)/(510+566) = 92.845` (quantity-weighted). `SL = 78.92`, `TP = 106.77`.
- If TP hits: `pnl = 510×(106.77−98) + 566×(106.77−88.2) = 4,473 + 10,511 = ₹14,984`.

## Edge Cases & Assumptions

| Case | Handling |
|------|----------|
| Result date is a non-trading day | use previous trading day's session for `marked_high` |
| No data after `observation_start` (current blocker) | skip stock, flag `NO_DATA` |
| Entry-1 never triggers | no trade |
| Gap through a level (entry-2 / SL / TP) | fill at session open |
| Both SL & TP in one bar | SL first (conservative) |
| Position open at end of data | `OPEN`, mark-to-market at last close |
| SL/TP anchor | **quantity-weighted (cost-basis) average** `(q1·e1+q2·e2)/(q1+q2)`; equal-rupee legs → more shares in the cheaper leg → avg below the simple mean |

## Architecture

Follow the existing `engine/stock_backtest.py` conventions (self-contained, per-stock
1-min parquet, day-by-day, dataclass trade, `trades_to_dataframe`).

- **New engine:** `engine/result_reaction_backtest.py`
  - `@dataclass ResultReactionTrade` with fields: `symbol, result_date, result_session,
    marked_high, observation_start, entry1_time, entry1_price, qty1, entry2_time,
    entry2_price, qty2, avg_price, sl_price, tp_price, exit_time, exit_price,
    exit_reason (SL|TP|OPEN|NO_DATA|NO_ENTRY), pnl, pnl_pct, days_held`.
  - `class ResultReactionEngine`:
    - `__init__(result_csv, capital_per_stock=100000, sl_pct=15, tp_pct=15,
      avg_down_pct=10, stocks=None)`.
    - `_load_stock(symbol)` — reuse the loader pattern from `stock_backtest.py`
      (parse tz, `date`/`time` columns, sort by `ts`).
    - `_run_stock(symbol, result_date)` — implements the logic above.
    - `run(progress_callback)` — iterate the 15 (symbol, result_date) rows.
  - `trades_to_dataframe(trades)`.
- **Runner script:** `run_result_reaction_backtest.py` (top-level, like `run_backtest.py`)
  — instantiate engine, run, print summary (count, win rate, total/avg pnl, open trades),
  write `result_reaction_trades.csv`.
- **UI runner:** optional, later — a `ui/result_reaction_backtest_runner.py` mirroring the
  other Streamlit runners, only if the user wants it in the dashboard.

## Parameters (all overridable)

| Param | Default | Meaning |
|-------|---------|---------|
| `capital_per_stock` | 100000 | ₹ per stock, split 50/50 |
| `second_entry_pct` | −20 | entry-2 = entry1 × (1 + this/100); **signed** (+ = pyramid up, − = average down) |
| `sl_pct` | None | SL = avg × (1 − this/100); **None = no stop** (TP/time exits only) |
| `tp_pct` | 15 | TP before the 2nd entry = avg × (1 + this/100) |
| `tp_pct_after_second` | 0 | TP after the 2nd entry; **0 = exit at the average (breakeven)**; None = keep `tp_pct` |
| `max_hold_days` | None | calendar days from result date to force-exit (`TIME`); None = no cap |
| `entry_split` | 50/50 | leg allocation |

## Verification Plan

1. **Unit-level (TDD):** synthetic 1-min frames exercising each path — entry-1 at open,
   gap-up delayed entry-1, entry-2 fill, entry-2 never fills, SL hit, TP hit, gap-through
   fills, OPEN-at-EOD, non-trading result date, no-data skip.
2. **Per-stock manual trace:** pick one stock (e.g. ANGELONE, result 16 Apr 2026), print
   the bar-by-bar decision log and confirm marked_high, entry1, entry2, SL/TP, exit by
   hand against the parquet.
3. **End-to-end:** run the script over all 15, eyeball the CSV; confirm no look-ahead
   (entries/exits only use the triggering bar and later), and that any still-open trades
   are correctly marked `OPEN`.

## Open Questions to Confirm

1. `capital_per_stock` default of ₹1,00,000 OK?
2. ~~Simple-mean SL/TP anchor~~ → **resolved: quantity-weighted (cost-basis) average.**
3. Engine/file name `result_reaction_backtest.py` OK, or prefer another?
4. Need the Streamlit UI runner now, or is the CLI script + CSV enough for this PoC?
