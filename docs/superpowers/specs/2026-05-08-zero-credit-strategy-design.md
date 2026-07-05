# Zero Credit Strategy — 4-Leg Premium-Targeted NIFTY Backtest Design

**Date:** 2026-05-08
**Slug:** `zero_credit`
**Display name:** Zero Credit
**Instruments (v1):** NIFTY only
**Scope:** Daily entry, weekly expiry (`expiry_type='WEEK'`, `expiry_code=1`), no signal/indicator inputs

---

## 1. Overview

A daily, premium-targeted, four-leg NIFTY weekly-options strategy. Every trading day at 09:20:00 IST we build a "zero-credit" structure (net premium paid ≈ 0 by construction):

```
BUY  1 lot  CE @ strike whose 09:20 open ≈ ₹100
BUY  1 lot  PE @ strike whose 09:20 open ≈ ₹100
SELL 2 lots CE @ strike whose 09:20 open ≈ ₹50
SELL 2 lots PE @ strike whose 09:20 open ≈ ₹50
```

Net premium paid ≈ `1×100 + 1×100 − 2×50 − 2×50 = 0` (hence "zero credit"). Net contracts: short 1 CE + short 1 PE → net theta-positive, near-zero delta at entry.

Exit on whichever fires first: combined unrealized P&L ≥ `tp_target_inr` (default ₹800), combined unrealized P&L ≤ `−sl_target_inr` (default ₹2500; set to 0 or null to disable), or time exit at 15:20. One trade per day, no re-entry.

---

## 2. Trade Construction

### 2.1 Entry calendar

For every trading date in `[backtest_start, backtest_end]` for which option data exists. Trading days are derived from the distinct dates present in the option parquet (the same approach existing engines use). Holidays are naturally skipped because they have no data.

If the date is itself a holiday or has no option data, the day is skipped with `skip_reason = no_data_on_entry_day`.

### 2.2 Entry time

09:20:00 IST. We use the **open** of each leg's 09:20 1-min bar as that leg's entry price.

### 2.3 Expiry filter

`expiry_type='WEEK'` AND `expiry_code=1`. Required throughout (entry slice, holding-period TP scan, time-exit slice).

**Critical filter rule:** the parquet contains weekly and monthly contracts at the same `expiry_code==1` for any given minute, with very different premiums and IVs. Filtering only by `expiry_code` mixes legs across two different expiries and produces nonsensical fills. `expiry_type='WEEK'` must be enforced at every read.

**Expiry-day open item:** on the day the weekly contract expires, `expiry_code` may roll from 1 to 0. The implementation will probe the data and document the rule it found — same situation `debit_spread` already flagged. The user wants entries on expiry day too (current week), so the engine must handle both `expiry_code=1` (entry day before expiry) and the expiry-day rollover correctly.

### 2.4 Strike selection — premium-based, per leg, independent

For each of the 4 legs, at the 09:20 1-min slice for `underlying='NIFTY'`, `expiry_type='WEEK'`, `expiry_code=1`, filtered to the relevant `option_type` (CE or PE):

1. Compute `|leg_open − target_premium|` for every available strike on that side.
2. Pick the strike with smallest distance.
3. **Tiebreaker** (when two strikes are equidistant in premium): pick the one closer to ATM by strike distance (`|strike − atm_strike|`). If still tied, lower strike for CE / higher strike for PE.
4. **Tolerance gate:** if the picked strike's `|leg_open − target_premium| > premium_match_tolerance_inr` (default ₹20), skip the day with `skip_reason = no_strike_within_tolerance: <leg_code>`.

The 4 picks are independent — long-CE strike will not necessarily equal long-PE strike, and the same for shorts.

`atm_strike` for the day is the strike of the 09:20 row with `moneyness=='ATM'` for `underlying='NIFTY'`, `expiry_type='WEEK'`, `expiry_code=1`. If multiple rows tag ATM, take the one with smallest `|strike − spot|`.

### 2.5 Four legs

| # | Leg code | Side | Lots | option_type | Premium target | Notes |
|---|----------|------|------|-------------|----------------|-------|
| 1 | `ce_long`  | BUY  | 1 | CE | ₹100 | Closer to ATM |
| 2 | `pe_long`  | BUY  | 1 | PE | ₹100 | Closer to ATM |
| 3 | `ce_short` | SELL | 2 | CE | ₹50  | Further OTM |
| 4 | `pe_short` | SELL | 2 | PE | ₹50  | Further OTM |

Quantity per leg = `lots × LOT_SIZE['NIFTY']` = `lots × 65`. Per-leg quantities: `65, 65, 130, 130`. Total 6 lots = 390 contracts.

If any of the 4 legs cannot find a strike within tolerance at 09:20:00, the day is skipped with `skip_reason = no_strike_within_tolerance: <leg_code>`.

### 2.6 Net debit at entry

```
sign_i        = +1 if BUY else −1
net_debit_pts = Σ_i (sign_i × lots_i × leg_open_i)
                  # Positive = debit (cash out); negative = credit (cash in).
net_debit_inr = net_debit_pts × 65
```

Recorded for the trade row but **not** used to size TP — TP is the absolute ₹ target from config.

---

## 3. Exit Logic

### 3.1 Take profit (1-min intra-day check)

Between the entry bar (exclusive) and the time-exit bar (exclusive), at every 1-min bar of the entry day, mark the 4 locked strikes:

```
mtm_pts = Σ_i (sign_i × lots_i × leg_close_i_at_bar) − net_debit_pts
mtm_inr = mtm_pts × 65
```

If `mtm_inr ≥ tp_target_inr` (default ₹800), exit all 4 legs at that bar's close. `exit_reason = TP`.

### 3.2 Stop loss (1-min intra-day check)

If `sl_target_inr` is set and > 0, then on the same 1-min scan: if `mtm_inr ≤ −sl_target_inr` (default ₹2500), exit all 4 legs at that bar's close. `exit_reason = SL`. TP and SL are evaluated on every bar; whichever crosses first wins (they cannot fire on the same bar). Set `sl_target_inr` to 0 or null to disable.

### 3.3 Time exit

If neither TP nor SL fires, square off all 4 legs at the **15:20 1-min bar close on the entry day**. `exit_reason = TIME`.

### 3.4 Edge cases

| Case | Behavior |
|------|----------|
| Date has no option data at all | Skip day, `no_data_on_entry_day` |
| 09:20 bar missing entirely | Skip day, `no_entry_bar` |
| Any of 4 legs has no strike within tolerance | Skip day, `no_strike_within_tolerance: <leg_code>` |
| Single-bar gap during TP scan for any leg | Carry-forward last leg price within ≤ `data_gap_force_exit_minutes` (default 30) |
| Data gap > 30 min for any leg mid-trade | Force exit at last good MTM bar, `exit_reason = data_gap_force_exit` |
| 15:20 bar missing on entry day | Walk back to last available bar before 15:20 |
| Same-strike collision (e.g. picked `ce_long` strike == `ce_short` strike) | Allowed — they net out, but logged with `warning = strike_collision_<leg_pair>`. The ₹100 vs ₹50 premium gap and tolerance ₹20 make this rare. |

---

## 4. Mark-to-market & Equity Curve

Two layers of MTM:

- **Intra-day (1-min) MTM:** used only for TP trigger evaluation during a live trade. Not persisted.
- **Daily equity points:** at each trading day's exit bar (or last available bar of the day), append `(date, equity_inr, drawdown_inr, drawdown_pct, in_trade)` to the equity curve. Days skipped (no entry) contribute a flat point equal to running equity.

```
running_equity_inr = reference_capital + cumulative_realized_pnl_inr
                   = 200_000 + cumulative_realized_pnl_inr
```

Max drawdown is computed off this daily equity series.

---

## 5. P&L Calculation

```
exit_value_pts = Σ_i (sign_i × lots_i × leg_exit_price_i)   # signed sum at exit
pnl_pts        = exit_value_pts − net_debit_pts             # since net_debit_pts == signed sum at entry
pnl_inr        = pnl_pts × 65
return_pct     = pnl_inr / reference_capital                # default ₹2,00,000
```

Costs (slippage / brokerage / STT / GST) are **not** modeled in v1.

---

## 6. Sizing & Capital

- **Trades per day:** 1 (locked, no re-entry after TP).
- **Reference capital:** ₹2,00,000. Used as the denominator for return % and drawdown %, not as a sizing driver.
- **Lots:** `buy_lots=1, sell_lots=2` (configurable). Quantities per leg follow `lots × 65`.

---

## 7. Configuration

`saved_strategies/zero_credit.json`:

```json
{
  "name": "zero_credit",
  "strategy_type": "zero_credit",
  "instruments": ["NIFTY"],
  "entry": {
    "entry_time": "09:20"
  },
  "structure": {
    "buy_premium_target_inr":  100,
    "sell_premium_target_inr":  50,
    "buy_lots":  1,
    "sell_lots": 2,
    "premium_match_tolerance_inr": 20
  },
  "exit": {
    "tp_target_inr": 800,
    "sl_target_inr": 2500,
    "time_exit": "15:20",
    "data_gap_force_exit_minutes": 30
  },
  "sizing": {
    "reference_capital": 200000
  },
  "backtest_start": "2025-01-01",
  "backtest_end":   "2026-05-08"
}
```

The `structure` block is parameterized so other variants (e.g. "buy 80, sell 40", or 1-3 ratio) can be tested by editing the JSON without engine changes.

---

## 8. Output Artifacts

### 8.1 Trades CSV — `zero_credit_trades_<start>_<end>.csv`

One row per day attempt (including skips):

```
date, entry_time, atm_strike_at_entry, spot_at_entry,
ce_long_strike,  ce_long_open,  ce_long_exit,
pe_long_strike,  pe_long_open,  pe_long_exit,
ce_short_strike, ce_short_open, ce_short_exit,
pe_short_strike, pe_short_open, pe_short_exit,
net_debit_pts, net_debit_inr, tp_target_inr,
exit_time, exit_reason,
pnl_pts, pnl_inr, return_pct,
running_equity_inr,
skip_reason
```

### 8.2 Daily equity CSV — `zero_credit_equity_<start>_<end>.csv`

```
date, equity_inr, drawdown_inr, drawdown_pct, in_trade
```

### 8.3 Stdout summary block

```
Total days processed:           N
Trades placed:                  T
Trades skipped:                 N − T   (per-reason breakdown)
Wins / Losses:                  W / L   (% profitable days)
Total P&L (₹):                  ...
Mean / Median P&L (₹):          ...
Best day / Worst day:           ...
Max drawdown (₹ and %):         ...
Max consecutive losing days:    ...
Exit-reason breakdown:          TP=..%, TIME=..%, data_gap=..%
```

No Sharpe / Sortino / annualized return in v1 (per user choice — cleaner output).

---

## 9. UI Integration

In `ui/backtest_runner.py`, add a "Zero Credit" entry following the same pattern as `gamma_blast` / `debit_spread`:

- Date-range picker (defaults to data range).
- Editable fields:
  - `tp_target_inr` (default 1000)
  - `entry_time` (default 09:20)
  - `time_exit` (default 15:20)
  - `buy_premium_target_inr` (default 100)
  - `sell_premium_target_inr` (default 50)
  - `buy_lots` (default 1)
  - `sell_lots` (default 2)
  - `premium_match_tolerance_inr` (default 20)
- Reference capital (default 2,00,000).
- "Run backtest" button → invokes `engine.zero_credit_backtest.run(config)`.

Output panel:
- Summary metrics block.
- Daily equity-curve line chart with drawdown overlay.
- Paginated trade table.
- Two download buttons: trades CSV + equity CSV.

`ui/strategy_form.py` — extend strategy-type enum to include `zero_credit` if such an enum exists.

---

## 10. Testing Plan

`tests/test_zero_credit.py` — synthetic 1-min bars where possible; one real-data integration check.

**Strike picking:**
1. Among 5 candidate strikes with premiums `[40, 80, 95, 110, 140]`, target ₹100 → picks 95 (Δ=₹5, vs 110 at Δ=₹10).
2. Equidistant tiebreak: premiums `[90, 110]` for target ₹100 → both Δ=₹10, pick the one closer to ATM by strike distance.
3. No strike within tolerance: nearest premium is ₹130 for target ₹100, tolerance ₹20 → skip with `no_strike_within_tolerance: <leg_code>`.
4. CE and PE picked independently (verify long-CE and long-PE strikes can differ).

**Entry:**
5. 09:20 bar missing → skip with `no_entry_bar`.
6. Net debit math hand-verified (debit case AND credit case).

**Exit:**
7. TP fires intra-day at 1-min granularity at, say, 10:47 → exit reason TP, P&L ≥ 1000.
8. TP never fires → exit at 15:20 close, exit reason TIME.
9. Data gap < 30 min → carry-forward.
10. Data gap > 30 min → `data_gap_force_exit`.
11. 15:20 bar missing → walk back to last available bar before 15:20.

**Expiry filter:**
12. Both `expiry_type='WEEK'` and `expiry_type='MONTH'` rows present at 09:20 → only WEEK rows used (parallel to debit_spread's filter rule).
13. Expiry-day behavior: `expiry_code` semantics on expiry day documented after data probe.

**P&L:**
14. End-to-end P&L matches a hand-built ledger across all 4 legs.
15. Skipped days contribute flat equity (no change in `running_equity_inr`) and feed into max-drawdown computation.

**Integration:**
16. One real-data day end-to-end (a known date) with golden-output trade row.
17. One-month real-data slice; deterministic across two runs.

---

## 11. File Changes

**New files:**
- `engine/zero_credit_backtest.py` — engine (~400–500 lines).
- `saved_strategies/zero_credit.json` — config above.
- `tests/test_zero_credit.py` — full test suite.

**Modified files:**
- `ui/backtest_runner.py` — add Zero Credit strategy handler.
- `ui/strategy_form.py` — extend strategy-type enum if applicable.

**Untouched (reused):**
- `engine/expiry_calendar.py`, `engine/data_loader.py`, `engine/reporter.py`.
- `config.py` — `LOT_SIZE['NIFTY']` already present.
- `data/options/nifty/NIFTY_OPTIONS_1m.parquet` — sole data source.

---

## 12. Out of Scope (v1)

- Other instruments (SENSEX, FinNifty, Midcap Nifty).
- Slippage, brokerage, STT, GST, exchange fees.
- Re-entry after TP/SL — locked at one trade per day.
- Live / forward trading integration.
- Sharpe / Sortino / annualized-return metrics — kept summary minimal per user choice.
- Alternate entry triggers (signal-based, RSI, VWAP, etc.) — purely time-driven.

---

## 13. Open Items / Assumptions Pinned

- **`expiry_code` semantics on expiry day itself:** as flagged for `debit_spread`, on the day the weekly contract expires, `expiry_code` may roll from 1 to 0. Implementation will probe the data and document the rule it found. The user wants entries on expiry day (current week), so both states must be handled.
- **Multiple ATM rows:** if more than one row tags `moneyness=='ATM'` at 09:20 (shouldn't happen but guard against), pick the row with min `|strike − spot|`.
- **Skipped days in equity series:** skipped days contribute a flat point equal to running equity; they neither help nor hurt drawdown computation.
- **Premium tolerance default (₹20):** chosen to allow normal spread-board variance around ₹100/₹50 targets without swallowing the strategy on quiet days. Tunable via JSON.
- **Same-strike collision:** allowed and logged. The legs net out cleanly in P&L; the warning is informational. Premium gap of ₹100 vs ₹50 plus tolerance ₹20 makes this rare.
