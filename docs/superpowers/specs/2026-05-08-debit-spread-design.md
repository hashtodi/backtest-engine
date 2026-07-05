# Debit Spread — 1-3-2 Broken-Wing Condor Backtest Design

**Date:** 2026-05-08
**Slug:** `debit_spread`
**Display name:** Debit Spread
**Instruments (v1):** NIFTY only (extensible to FinNifty / Midcap Nifty / SENSEX)
**Scope:** Weekly expiry, calendar-driven entry, no signal/indicator inputs

---

## 1. Overview

A purely systematic, calendar-driven options structure on NIFTY weeklies. Every week, two trading days before expiry, at 11:00 AM, we put on a six-leg defined-risk spread combining a 1-3-2 ratio call structure and a mirrored 1-3-2 ratio put structure. The structure has no net contracts (1 − 3 + 2 = 0 per side), giving a structurally bounded loss; the long far-OTM legs cap the losses from the 3 short OTM legs on extreme moves.

We exit on whichever comes first: combined unrealized P&L reaching `1.5 × net_debit_at_entry`, or 15:25 close on expiry day.

No directional bias, no signal, no stop loss. One trade per week.

---

## 2. Trade Construction

### 2.1 Entry calendar

For every date `expiry_date` in `config.NIFTY_WEEKLY_EXPIRY_DATES` that falls within `[backtest_start, backtest_end]`:

```
entry_date = expiry_date − 2 trading days
```

Trading days are derived from the distinct dates present in the option parquet (the same approach existing engines use). Holidays are naturally skipped because they have no data.

If `entry_date` is itself a holiday or has no option data, the week is skipped with `skip_reason = no_data_on_entry_day`.

### 2.2 Entry time

11:00:00 IST. We use the **open** of the 11:00 1-min bar as each leg's entry price.

### 2.3 ATM resolution

At the 11:00 1-min slice for `underlying='NIFTY'`, `expiry_code=1`, **`expiry_type='WEEK'`**, locate the row with `moneyness=='ATM'`. Take its `strike` as `atm_strike` for the day. If multiple rows tag ATM, take the one with smallest `|strike - spot|`.

**Critical filter rule:** `expiry_type=='WEEK'` is required throughout (entry slice, holding slice, expiry squareoff). The parquet contains both weekly and monthly contracts at the same `expiry_code==1` for any given minute, and they have very different premiums and IVs. Filtering only by `expiry_code` will mix legs across two different expiries and produce nonsensical fills (e.g., a SELL leg priced at the monthly's higher premium combined with a BUY leg priced at the weekly's lower premium, producing a fake "credit" entry).

### 2.4 Six legs

NIFTY strikes are 50 points apart, and the parquet's `strike_offset` column is in **strike-count units** (e.g. `−1` = one strike below ATM = 50 points below). Therefore the user's "50 / 200 / 250 point" offsets map cleanly to offsets `1 / 4 / 5`.

| # | Side | Lots | option_type | strike_offset | Notes |
|---|------|------|-------------|---------------|-------|
| 1 | BUY  | 1 | CE | −1 | 50-pt ITM call |
| 2 | SELL | 3 | CE | +4 | 200-pt OTM call |
| 3 | BUY  | 2 | CE | +5 | 250-pt OTM call |
| 4 | BUY  | 1 | PE | +1 | 50-pt ITM put |
| 5 | SELL | 3 | PE | −4 | 200-pt OTM put |
| 6 | BUY  | 2 | PE | −5 | 250-pt OTM put |

Quantity per leg = `lots × LOT_SIZE['NIFTY']` = `lots × 65`.

If any of the 6 required `(option_type, strike_offset)` rows is missing at 11:00:00, the week is skipped with `skip_reason = missing_strike: <leg_code>`.

### 2.5 Net debit and TP target

```
sign_i        = +1 if BUY else −1
net_debit_pts = Σ_i (sign_i × lots_i × leg_open_i)         # in points
                  # Positive = debit (cash out); negative = credit (cash in).
                  # Equivalent to: total_paid_for_longs - total_received_from_shorts.
net_debit_inr = net_debit_pts × 65
tp_target_inr = max(0, net_debit_inr) × 1.5
```

If `net_debit_inr ≤ 0` (the structure was opened for a credit), `tp_target_inr = 0`, meaning TP triggers at the first strictly-positive unrealized P&L print after entry.

---

## 3. Exit Logic

### 3.1 Take profit (1-min intra-day check)

Between the entry bar (exclusive) and the expiry-day exit bar (exclusive), at every 1-min bar of every trading day in the holding window, mark all 6 locked strikes:

```
mtm_pts = Σ_i (sign_i × lots_i × leg_close_i_at_bar) − net_debit_pts
mtm_inr = mtm_pts × 65
```

If `mtm_inr ≥ tp_target_inr` (and for the credit case, `mtm_inr > 0`), exit all 6 legs at that bar's close. `exit_reason = TP`.

### 3.2 Hold to expiry

If TP never fires, square off all 6 legs at the **15:25 1-min bar close on expiry_date**. `exit_reason = EXPIRY`.

### 3.3 No stop loss

The structure caps the loss; we let it run.

### 3.4 Edge cases

| Case | Behavior |
|------|----------|
| 11:00 bar missing entirely | Skip week, `no_entry_bar` |
| Any of 6 legs missing at 11:00 | Skip week, `missing_strike: <leg_code>` |
| Net entry is a credit | `tp_target = 0`; TP triggers at first strictly-positive MTM bar |
| Single-bar gap during TP scan | Carry-forward last leg price within ≤ 30 min |
| Data gap > 30 min for any leg mid-trade | Force exit at last good MTM bar, `exit_reason = data_gap_force_exit` |
| 15:25 expiry bar missing | Walk back to last available bar before 15:25 on expiry date |
| `entry_date` not present in option data | Skip, `no_data_on_entry_day` |

---

## 4. Mark-to-market & Equity Curve

Two layers of MTM:

- **Intra-day (1-min) MTM:** used only for TP trigger evaluation during a live trade. Not persisted.
- **Daily equity points:** at each trading day's 15:25 bar close (or last available bar of the day), append `(date, equity_inr, drawdown_inr, drawdown_pct)` to the equity curve. Days with no open position contribute a flat point equal to running equity.

```
running_equity_inr = 300_000 + cumulative_realized_pnl_inr
```

For days inside an open trade, equity also reflects the current-day MTM: `running_equity + unrealized_mtm`. (For days outside any trade, unrealized = 0.)

Max drawdown is computed off this daily equity series.

---

## 5. P&L Calculation

```
exit_value_pts = Σ_i (sign_i × lots_i × leg_exit_price_i)   # signed sum at exit
pnl_pts        = exit_value_pts − net_debit_pts             # since net_debit_pts == signed sum at entry
pnl_inr        = pnl_pts × 65
return_pct     = pnl_inr / 300_000
```

Costs (slippage / brokerage / STT / GST) are **not** modeled in v1.

---

## 6. Sizing & Capital

- **Sets per trade:** 1 (fixed). Always trade exactly the 1-3-2 / 1-3-2 structure once.
- **Reference capital:** ₹3,00,000. Used as the denominator for return % and drawdown %, not as a sizing driver.
- **Quantities per leg:** 65 / 195 / 130 / 65 / 195 / 130.

---

## 7. Configuration

`saved_strategies/debit_spread.json`:

```json
{
  "name": "debit_spread",
  "strategy_type": "debit_spread",
  "instruments": ["NIFTY"],
  "entry": {
    "days_before_expiry": 2,
    "entry_time": "11:00"
  },
  "structure": {
    "ce_legs": [
      { "side": "BUY",  "lots": 1, "strike_offset": -1 },
      { "side": "SELL", "lots": 3, "strike_offset":  4 },
      { "side": "BUY",  "lots": 2, "strike_offset":  5 }
    ],
    "pe_legs": [
      { "side": "BUY",  "lots": 1, "strike_offset":  1 },
      { "side": "SELL", "lots": 3, "strike_offset": -4 },
      { "side": "BUY",  "lots": 2, "strike_offset": -5 }
    ]
  },
  "exit": {
    "tp_multiple_of_max_loss": 1.5,
    "expiry_squareoff_time": "15:25",
    "data_gap_force_exit_minutes": 30
  },
  "sizing": {
    "sets_per_trade": 1,
    "reference_capital": 300000
  },
  "metrics": {
    "risk_free_rate": 0.06,
    "annualization_factor": 52
  },
  "backtest_start": "2025-01-01",
  "backtest_end":   "2026-05-05"
}
```

The `structure` block is parameterized so other ratio variants (1-2-1, different offsets) can be tested by editing the JSON without engine changes.

---

## 8. Output Artifacts

### 8.1 Trades CSV — `debit_spread_trades_<start>_<end>.csv`

One row per weekly trade attempt (including skips):

```
expiry_date, entry_date, entry_time, atm_strike, spot_at_entry,
ce_itm_strike,  ce_itm_open,   ce_itm_exit,
ce_short_strike,ce_short_open, ce_short_exit,
ce_far_strike,  ce_far_open,   ce_far_exit,
pe_itm_strike,  pe_itm_open,   pe_itm_exit,
pe_short_strike,pe_short_open, pe_short_exit,
pe_far_strike,  pe_far_open,   pe_far_exit,
net_debit_pts, net_debit_inr, tp_target_inr,
exit_time, exit_reason,
pnl_pts, pnl_inr, return_pct,
running_equity_inr,
skip_reason
```

### 8.2 Daily equity CSV — `debit_spread_equity_<start>_<end>.csv`

```
date, equity_inr, drawdown_inr, drawdown_pct, in_trade (bool)
```

### 8.3 Stdout & summary block

```
Total weeks processed:          N
Trades placed:                  T
Trades skipped:                 N − T   (per-reason breakdown)
Wins (P&L > 0):                 W   (W/T %)
Losses (P&L < 0):               L   (L/T %)
% profitable weeks:             W/T
Mean P&L (₹):                   ...
Median P&L (₹):                 ...
Total P&L (₹):                  ...
Total return on ₹3,00,000:      ...%
Max drawdown (₹ and %):         ...   (peak-to-trough on daily equity)
Max consecutive losing weeks:   ...
Sharpe (weekly, √52, RFR 6%):   ...
Sortino (weekly, √52, RFR 6%):  ...
Best trade / Worst trade:       ...
Exit-reason breakdown:          TP=..%, EXPIRY=..%, data_gap=..%
```

### 8.4 Sharpe / Sortino formulas

Weekly returns `r_w = pnl_w / 300_000` (one per placed trade). Skipped weeks contribute `r_w = 0`.

```
mean_r       = mean(r_w)
sd_r         = stdev(r_w, ddof=1)
sd_down      = stdev([min(0, r_w − weekly_rfr) for r_w in returns], ddof=1)
weekly_rfr   = 0.06 / 52

sharpe   = (mean_r − weekly_rfr) / sd_r       × √52
sortino  = (mean_r − weekly_rfr) / sd_down    × √52
```

If `sd_r == 0` or `sd_down == 0`, output `n/a`.

---

## 9. UI Integration

In `ui/backtest_runner.py`, add a "Debit Spread" tab/option following the same pattern as `gamma_blast`, `ha_nr7`, etc.:

- Date-range picker (defaults to data range).
- Editable `tp_multiple_of_max_loss` (default 1.5).
- Editable `entry_time` (default 11:00).
- Editable `days_before_expiry` (default 2).
- Reference capital (default 3,00,000).
- "Run backtest" button → invokes `engine.debit_spread_backtest.run(config)`.

Output panel:
- Summary metrics block.
- Daily equity-curve line chart with drawdown overlay.
- Paginated trade table.
- Two download buttons: trades CSV + equity CSV.

`ui/strategy_form.py` — extend strategy-type enum to include `debit_spread` if such an enum exists.

---

## 10. Testing Plan

`tests/test_debit_spread.py` — synthetic 1-min bars where possible; one real-data integration check.

**Calendar:**
1. T−2 trading days, regular week (no holidays).
2. T−2 with a holiday between entry and expiry.
3. T−2 when expiry itself is shifted to Monday (Tuesday holiday).
4. `entry_date` itself a holiday → week skipped with `no_data_on_entry_day`.

**Strike resolution:**
5. ATM correctly identified from `moneyness=='ATM'` row.
6. All 6 offset legs picked correctly for CE and PE.
7. Missing one of 6 strikes → skip with correct `skip_reason`.

**Entry:**
8. 11:00 bar missing entirely → skip with `no_entry_bar`.
9. Net debit math (debit case) — manual P&L cross-check.
10. Net debit math (credit case) → `tp_target_inr == 0`.

**Exit:**
11. TP fires intra-day at 1-min granularity on entry day at, say, 11:23.
12. TP fires on day 2 between 09:15 and 15:25.
13. TP never fires → exit at 15:25 expiry close.
14. Credit-entry case: first strictly-positive MTM bar → immediate TP exit.
15. Data gap < 30 min mid-trade → carry-forward.
16. Data gap > 30 min mid-trade → `data_gap_force_exit`.
17. Expiry-day 15:25 bar missing → walk back.

**P&L & metrics:**
18. End-to-end P&L matches a hand-built ledger across all 6 legs.
19. Sharpe / Sortino / max drawdown / max consecutive losing weeks computed on a hand-crafted P&L sequence with known answers.
20. Skipped weeks contribute `r_w = 0` to weekly-return series for Sharpe/Sortino (verify, vs. excluding them — implementation choice locked here).

**Integration:**
21. One real-data week end-to-end (entry on a known Friday, expiry the following Tuesday) with golden-output trade row.
22. One-month real-data slice; summary metrics deterministic across two runs.

---

## 11. File Changes

**New files:**
- `engine/debit_spread_backtest.py` — engine (~500–600 lines).
- `saved_strategies/debit_spread.json` — config above.
- `tests/test_debit_spread.py` — full test suite.

**Modified files:**
- `ui/backtest_runner.py` — add Debit Spread strategy handler.
- `ui/strategy_form.py` — extend strategy-type enum if applicable.

**Untouched (reused):**
- `engine/backtest.py`, `engine/expiry_calendar.py`, `engine/data_loader.py`, `engine/reporter.py`.
- `config.py` — `NIFTY_WEEKLY_EXPIRY_DATES` and `LOT_SIZE['NIFTY']` already present.
- `data/options/nifty/NIFTY_OPTIONS_1m.parquet` — sole data source.

---

## 12. Out of Scope (v1)

- FinNifty, Midcap Nifty, SENSEX (extensible later by adding option parquets, lot sizes, and instrument-specific strike-offset logic since SENSEX has 100-pt strike spacing).
- Slippage, brokerage, STT, GST, exchange fees.
- Compounded position sizing (always 1 set per trade in v1).
- Margin-aware sizing (no SPAN+exposure model).
- Live / forward trading integration.
- Alternate entry triggers (signal-based, RSI, VWAP, etc.) — purely calendar-driven.
- Variable structures across weeks (1-3-2 hard-coded via JSON; engine would need extension to A/B-test multiple structures in one run).

---

## 13. Open Items / Assumptions Pinned

- **`expiry_code` semantics on expiry day itself:** Need to verify in implementation whether `expiry_code` rolls from 1 to 0 on expiry day. If so, the 15:25 squareoff lookup uses `expiry_code=0` on that date. Implementation will probe the data and document the rule it found.
- **Multiple ATM rows:** if more than one row tags `moneyness=='ATM'` at 11:00 (shouldn't happen but guard against), pick the row with min `|strike - spot|`.
- **Skipped weeks in Sharpe denominator:** weekly-return series fills `r_w = 0` for skipped weeks. This makes Sharpe slightly more conservative than excluding them, and keeps the time-series length stable across runs.
