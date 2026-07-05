# SENSEX Dual Short-Premium Backtest — Design Spec

**Date:** 2026-07-01
**Status:** Approved for implementation
**Source:** `sensex_strategy_handover (2).pdf` (Sensex Options Strategy — Developer Handover Specification)
**Owner:** Harsh

---

## 1. Overview

Backtest a two-part, short-premium SENSEX weekly-options strategy. Two independent
sub-strategies run on the same trading day, sharing one ₹5,00,000 / 1-lot capital block.
Every leg is **sold** (short premium) and managed independently under one common rule set.

- **Part 1 — 09:45 Short Strangle:** at 09:45 sell the 0.25Δ CE and 0.25Δ PE.
- **Part 2 — Range Breakout:** freeze the 09:45→11:45 *spot* High/Low; after 11:45, sell the
  09:45-recorded ATM strike on a breakout (PUT on a High break, CALL on a Low break).

The two parts may **stack on the same side** (e.g. a short 0.25Δ PE from Part 1 and a short
ATM PE from Part 2). This is intended; no part nets or caps the other. Each leg is P&L'd
independently.

### Delta → strike mapping (owner-provided, no greeks feed)
The options data has **no delta column**, so delta is approximated by strike offset from ATM
(SENSEX strike step = 100):

| Delta | Meaning | `strike_offset` |
|-------|---------|-----------------|
| 0.5Δ  | ATM | `0` |
| 0.25Δ (CE) | ATM + 600 | `+6` |
| 0.25Δ (PE) | ATM − 600 | `−6` |

---

## 2. Scope

**In scope**
- Engine that backtests both parts + shared leg management on 1-minute data.
- Weekly-DTE gating (trade only DTE 0–3), full data range 2023-05-15 → 2026-04-23.
- CLI runner producing a per-leg trades CSV + a summary and skip ledger.

**Out of scope (non-goals)**
- Streamlit UI tab and `saved_strategies/*.json` wiring — **deferred** to a follow-up step
  after the numbers are verified.
- Scaling beyond 1 lot per leg (A3).
- Instruments other than SENSEX; monthly expiry; expiries other than the nearest weekly.
- Slippage / IOC buffer modelling (explicitly removed — see §9, A6).

---

## 3. Data

| Feed | Path | Key columns |
|------|------|-------------|
| Spot (range building) | `data/spot/sensex/SENSEX_1m.parquet` | `datetime, open, high, low, close` |
| Options (per-contract premium) | `data/options/sensex/SENSEX_OPTIONS_1m.parquet` | `datetime, option_type, expiry_type, expiry_code, atm_strike, strike_offset, moneyness, strike, spot, open, high, low, close` |

- **Granularity:** 1-minute OHLC. No tick data, no greeks.
- **Timezone:** `datetime` is ISO-8601 with `+05:30`. Parse the **tz-less prefix**
  (`pd.to_datetime(s.str.slice(0,19))`) to keep IST wall-clock — do **not** use tz-aware
  `.values`/`.to_numpy()`, which silently shifts to UTC and corrupts time-of-day filtering.
- **Expiry filter:** trade the nearest weekly — `expiry_type == "WEEK"` and `expiry_code == 1`.
  On the expiry day itself, `code=1` is the contract expiring that day; we sell it (no roll,
  because we are a premium *seller* and want maximum theta).
- **Backtest window:** full available range, **2023-05-15 → 2026-04-23** (spans three
  weekly-expiry regimes: Fri, then Tue, then Thu).

---

## 4. Components

| File | Change | Purpose |
|------|--------|---------|
| `engine/sensex_dual_short_backtest.py` | **new** | The engine: day loop, both parts, shared leg manager. |
| `engine/expiry_calendar.py` | **+2 helpers** | `get_weekly_expiry(instrument, trading_date)` and `days_to_weekly_expiry(instrument, trading_date)`, reading the existing `get_weekly_expiries("sensex")` calendar. (The module's existing `days_to_expiry`/`get_expiry_code` are **monthly**-only.) |
| `run_sensex_dual_short.py` | **new** (repo root) | CLI runner → writes `sensex_dual_short_trades.csv` + prints summary & skip ledger. |
| `tests/test_sensex_dual_short.py` | **new** | Unit tests (see §10). |

---

## 5. Per-day pipeline

For each trading day in the window:

1. **DTE gate.** `dte = days_to_weekly_expiry("sensex", day)` (calendar days to the nearest
   weekly expiry). Trade only if `dte ∈ {0,1,2,3}`; otherwise **skip the day** (recorded as a
   non-trading day, *not* as a skipped leg).
2. **Load the day's contracts** (`expiry_code=1`, `WEEK`) and the day's spot bars.
3. **09:45 close — selection & Part-1 entry:**
   - Determine ATM at the 09:45 bar (`atm_strike` column).
   - **Lock four absolute strikes** for the whole day:
     - P1 CE = row `(CE, strike_offset=+6)` → ATM+600
     - P1 PE = row `(PE, strike_offset=−6)` → ATM−600
     - P2 CE (recorded) = row `(CE, strike_offset=0)` → ATM
     - P2 PE (recorded) = row `(PE, strike_offset=0)` → ATM
   - **Enter P1 CE and P1 PE now**, at their 09:45 **close**. If a P1 offset row is absent at
     09:45, **skip that leg** (log reason).
   - If the ATM (offset 0) row is absent at 09:45, the P2 strikes cannot be recorded →
     **disable both P2 legs for the day** (log reason).
4. **09:45 → 11:45 inclusive — range building.** `range_high = max(spot.high)`,
   `range_low = min(spot.low)` over these bars. Freeze at 11:45.
5. **11:46 → 15:28 — breakout monitoring** (per spot bar, each side once):
   - `spot.high > range_high` and P2 PUT not yet entered → **enter P2 PUT** (sell recorded
     ATM PE) at that bar's option **close**.
   - `spot.low < range_low` and P2 CALL not yet entered → **enter P2 CALL** (sell recorded
     ATM CE) at that bar's option **close**.
   - A single **engulfing bar** (both conditions) fires **both** legs.
   - If the recorded strike's row is absent at the breakout bar, **skip that P2 leg** (log reason).
6. **Throughout** — run the leg manager (§7) on every open leg.
7. **15:28 — square-off.** Close all open legs at the 15:28 close. No new entries or
   re-entries after 15:28.

---

## 6. Strike selection & locking (hard requirement)

Each leg records at entry: `option_type`, **locked absolute `strike`**, `expiry_code=1`,
`entry_time`, `entry_cost`.

- All subsequent monitoring (SL / target / re-entry / EOD) uses **only that exact
  `(strike, option_type)` contract's own 1-minute OHLC** for the rest of the day.
- ATM drift after entry is **irrelevant** — we never re-pick the strike. `strike_offset` is
  used only at the selection instant (09:45); locking is by **absolute strike**.
- Part-2's `entry_cost` is the recorded strike's premium **at the breakout bar** (the actual
  Part-2 entry), **not** the 0.5Δ premium observed at 09:45.

This is the correctness-critical invariant: **SL/target come from, and are monitored on, the
contract we actually entered.**

---

## 7. Leg manager — per-leg state machine

All legs are short. Levels are locked at entry off the **original** entry cost `E` (A2):

- **Stop Loss:** `SL = 1.25 × E`. Fires when the contract's **1-min high ≥ SL**.
- **Target:** `TGT = 0.10 × E`. Fires when the contract's **1-min low ≤ TGT**.
- **Same-bar SL + Target → SL wins** (conservative tie-break).
- **Fills (no slippage):** entries at bar close; SL exit at exactly `SL`; target exit at
  exactly `TGT`; re-entry at exactly `E`; EOD at the 15:28 close.
- **P&L (points, short):** `entry − exit`. `pnl_inr = pnl_points × lot_size(20)`.
  - SL ≈ `E − 1.25E = −0.25E`; Target ≈ `E − 0.10E = +0.90E`; EOD = `E − close`.

### Re-entry logic (max 1 per leg per day)
- **Arms only after an SL exit** (never after a target hit or EOD).
- After the SL, keep watching the **same locked contract**. Because the premium rose to `1.25E`
  to trigger the SL, "return to cost" means the premium falls back **down** to `E`:
  - **Trigger:** the contract's **1-min low ≤ E**, evaluated from the **bar after** the SL exit.
  - **Action:** re-sell at `E`. Same `SL = 1.25E`, `TGT = 0.10E`.
- If the re-entered position stops out again → leg is **done** (no third entry). If it hits the
  target → done. If still open at 15:28 → EOD square-off.
- Re-entry may fire any time up to 15:28 (A1); no earlier cutoff.
- Since we add no slippage and re-sell exactly at `E`, the "original-cost vs re-entry-fill"
  distinction (A2) collapses — both bases give identical SL/target.
- Re-entry counts independently for each leg (A7): up to 1 for each of the up-to-2 Part-1 legs
  and up-to-2 Part-2 legs.

### Detection basis
Intrabar **touch** on the contract's own 1-min high/low (LTP proxy on 1-min data), consistent
with the breakout detection in §5. Re-entry is checked from the bar after the SL exit to avoid
intrabar sequencing guesswork.

---

## 8. Edge cases & skips (surfaced in the output)

- **Missing target strike at the needed minute → skip that leg**, counted in a **skip ledger**
  by reason:
  - `P1_CE_UNAVAILABLE_0945` (ATM+600 CE row absent at 09:45)
  - `P1_PE_UNAVAILABLE_0945` (ATM−600 PE row absent at 09:45)
  - `P2_ATM_NOT_RECORDABLE_0945` (ATM offset-0 row absent at 09:45 → both P2 legs disabled)
  - `P2_CALL_UNAVAILABLE_BREAKOUT` / `P2_PUT_UNAVAILABLE_BREAKOUT` (recorded strike row absent
    at the breakout bar)
- **Locked-contract data gap mid-day:** hold through minutes with no row for that contract;
  resume when rows reappear. If the contract never reappears before 15:28, square off at the
  **last available close** (logged with exit reason `EOD_LAST_AVAILABLE`).
- **Day-level skips (not counted as skipped legs):** `dte ∉ {0,1,2,3}`, no spot data, or no
  `code=1 WEEK` options for the day.
- **Missing 15:28 bar:** use the last available bar at or before 15:28.
- **Breakout comparison:** strict `>` / `<` (a true break beyond the frozen level).

---

## 9. Assumptions (mapped to handover A1–A7)

| # | Handover assumption | Decision in this build |
|---|---------------------|------------------------|
| A1 | Last entry vs square-off | Entries and re-entries allowed up to 15:28; hard square-off at 15:28. No earlier cutoff. |
| A2 | SL/target reference after re-entry | Original entry cost. With no slippage this equals the re-entry fill. |
| A3 | Lot count | 1 lot on every leg; SENSEX lot size = 20. |
| A4 | Breakout definition | **Spot** 1-min high/low **touch** beyond the frozen 09:45–11:45 range, monitored from 11:46. |
| A5 | Strike feed on multi-DTE days | Offsets measured on the traded nearest weekly (`code=1`). |
| A6 | Slippage buffer | **Removed** — owner decision. Fills are exact (no 1% buffer). |
| A7 | Re-entry count | 1 re-entry per leg, counted independently per leg. |

---

## 10. Testing & verification

**Unit tests (`tests/test_sensex_dual_short.py`):**
- Delta→strike mapping: 0.5Δ→offset 0, 0.25Δ CE→+6, 0.25Δ PE→−6.
- DTE gate correctness across all three expiry regimes (Fri/Tue/Thu), including the lone DTE≥4
  day per week being skipped and DTE 0 trading the expiring contract.
- Leg-manager math: SL = 1.25E, target = 0.10E, EOD P&L, points→₹.
- Same-bar SL+target → SL-first.
- Re-entry: arms only after SL; triggers on 1-min low ≤ E from the next bar; max 1; second SL
  ends the leg; no re-entry after a target hit.
- Engulfing bar fires both P2 legs.
- Skip ledger increments on missing strikes.

**Hand-verification (correctness gate):**
Pick 2–3 real trading days; reconstruct one Part-1 leg and one Part-2 breakout leg
minute-by-minute independently (strike, entry_cost, SL/target levels, exit bar, P&L) and assert
the engine matches exactly. This directly proves the §6 invariant (SL/target from the entered
contract only).

---

## 11. Outputs

**Trades CSV (`sensex_dual_short_trades.csv`)** — **one row per round-trip** (entry fill →
exit fill). A leg that re-enters produces **two rows** sharing the same `leg_id`, distinguished
by `entry_kind`:

`date, dte, leg_id, part (P1/P2), side (CE/PE), strike, entry_kind (INITIAL/REENTRY),
entry_time, entry_cost, sl_level, target_level, exit_time, exit_price,
exit_reason (SL/TARGET/EOD/EOD_LAST_AVAILABLE), pnl_points, pnl_inr`

- `sl_level`/`target_level` are identical across a leg's INITIAL and REENTRY rows (both off the
  original `E`). The leg's net P&L = sum of `pnl_points` across its rows.

**Summary printout:**
- Total ₹ P&L, split **Part-1 vs Part-2**.
- Win rate, average win / average loss, total legs traded, re-entry count.
- **Skip ledger** by reason (§8) and count of non-trading days.

---

## 12. Deliverables & sequencing (Approach A)

1. Add weekly-DTE helpers to `engine/expiry_calendar.py`.
2. Build `engine/sensex_dual_short_backtest.py`.
3. Build `run_sensex_dual_short.py` CLI runner.
4. Write and pass `tests/test_sensex_dual_short.py`.
5. Run the full backtest; produce CSV + summary.
6. Hand-verify 2–3 days.
7. **(Deferred)** Wire a Streamlit tab + `saved_strategies/sensex_dual_short.json` once numbers
   are trusted.
