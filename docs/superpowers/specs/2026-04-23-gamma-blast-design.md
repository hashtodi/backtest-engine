# Gamma Blast — Custom Backtest Engine Design

**Date:** 2026-04-23
**Instruments:** NIFTY, SENSEX (configurable, either / both)
**Scope:** Weekly expiry day only

---

## Overview

On expiry day, ATM option premiums get crushed by theta in the morning. When spot moves sharply in one direction, the contract that was getting crushed can explode in value because gamma peaks on expiry day. Gamma Blast buys an ATM CE or PE **after** it has been beaten down (cheap = bounded downside) **and** after it has shown signs of reversal (price above `entry_price`, not still dying).

The engine runs two independent state machines per instrument (one for CE, one for PE), all tied to a fixed-absolute-price entry/SL/TP schema.

---

## Data Sources

| Source | Path | Usage |
|--------|------|-------|
| NIFTY options 1-min | `data/options/nifty/NIFTY_OPTIONS_1m.parquet` | ATM premium, strike lookup, per-bar OHLC for entry/exit |
| SENSEX options 1-min | `data/options/sensex/SENSEX_OPTIONS_1m.parquet` | same as above |
| NIFTY spot 1-min | `data/spot/nifty/NIFTY_1m.parquet` | Reference only (spot column already embedded in option rows) |
| SENSEX spot 1-min | `data/spot/sensex/SENSEX_1m.parquet` | Reference only |
| Expiry calendar | `config.NIFTY_WEEKLY_EXPIRY_DATES`, `config.SENSEX_WEEKLY_EXPIRY_DATES` | Filter to expiry days only |

Option rows have: `ts, datetime, underlying, option_type, expiry_type, expiry_code, atm_strike, strike_offset, moneyness, strike, spot, open, high, low, close, volume, oi, iv`.

**ATM assumption:** we trust the `moneyness == 'ATM'` tag in the data. No fresh ATM computation from spot.

---

## Configuration (saved_strategies/gamma_blast.json)

```json
{
  "name": "gamma_blast",
  "strategy_type": "gamma_blast",
  "instruments": ["NIFTY", "SENSEX"],
  "params": {
    "NIFTY":  { "alert_price": null, "entry_price": null, "sl": null, "tp": null },
    "SENSEX": { "alert_price": 20,   "entry_price": 40,   "sl": 15,   "tp": 80   }
  },
  "timing": {
    "arm_start":      "10:00",
    "arm_deadline":   "15:00",
    "entry_deadline": "15:05",
    "force_exit":     "15:15"
  },
  "lot_size": 1,
  "backtest_start": "2025-01-01",
  "backtest_end":   "2026-04-13"
}
```

**Rules:**
- An instrument is only processed if it is in `instruments` **and** all four of its `params` values are non-null.
- All four price levels must be numeric (no percentage). They are absolute option premium values.
- Per-instrument levels allow NIFTY and SENSEX to use different absolute thresholds (they have different typical premium ranges).

---

## Expiry Day Filter

For each instrument, the engine iterates only dates in `SENSEX_WEEKLY_EXPIRY_DATES` / `NIFTY_WEEKLY_EXPIRY_DATES` that fall within `[backtest_start, backtest_end]` AND for which option data exists in the parquet. Non-expiry days are skipped entirely. Holiday-shifted dates are already baked into those lists.

**Data coverage:** NIFTY option data runs to 2026-04-13, SENSEX to 2026-03-09. If `backtest_end` is later than a given instrument's data coverage, the engine simply processes every available expiry day up to that limit and reports how many expiry days ran.

NIFTY weekly expiries are Tuesdays (or Mon/Fri if shifted). SENSEX weekly expiries are Fridays pre-2025-01-03, Tuesdays 2025-01-07 through 2025-08-26, and Thursdays from 2025-09-04 onward. They do not overlap in the backtest window (cross-instrument independence is preserved naturally).

---

## Per-Day State Machine

Four independent state machines run in parallel when both instruments are configured:

```
NIFTY-CE, NIFTY-PE, SENSEX-CE, SENSEX-PE
```

Each has three states: **IDLE → ARMED → OPEN → IDLE**.

### Initial state

All machines start **IDLE** at market open. Nothing happens before 10:00.

### IDLE → ARMED

At the **close** of each 1-min candle whose timestamp is in `[arm_start, arm_deadline]` inclusive (10:00 to 15:00):

- Look up the **current ATM** row for that instrument/option_type/`expiry_code == 1` at that minute (by `moneyness == 'ATM'` filter).
- If that row's `close` **strictly less than** `alert_price` (matching the user's "less than 20" rule), transition to **ARMED**, **locking** the row's `strike` for the rest of this trade attempt.

The armed strike is remembered by value. Subsequent tracking uses that strike specifically — not "whatever is ATM now" — until exit.

### ARMED → OPEN (standard path)

While ARMED, track the **locked strike's** candle stream (filter by `strike == locked_strike`, `option_type`, `expiry_code == 1`).

Entries require that the fill bar's open time is ≤ `entry_deadline` (15:05). Because entry fills at the next bar's open, the trigger bar close must be at time T where T + 1 minute ≤ 15:05, i.e. **trigger bar close at or before 15:04**.

On each such bar close:

- If `close > entry_price`:
  - Look at the **next** 1-min bar's `open` price.
  - If `next_open > tp` → **skip** entry. Release the armed strike → back to **IDLE** (same-day re-arm via the fresh-scan path still allowed).
  - If `next_open < sl` → **skip** entry (symmetric gap-down protection). Release → **IDLE**.
  - Otherwise → transition to **OPEN** with `entry_price = next_open`, `entry_time = next_bar.datetime`.
- If `close ≤ entry_price`: stay ARMED and keep scanning subsequent bars.

Both gap-protection skips are expected to be extremely rare at 1-min granularity; they exist to keep backtest results from degenerating in pathological cases.

### ARMED → OPEN (same-bar whip)

A single bar may satisfy both `low ≤ alert_price` AND `close > entry_price`:

- If the bar is **green** (`close > open`): treat as instant arm + trigger. Entry = next bar's open (subject to same gap-protection above).
- If the bar is **red** (`close < open`) or doji (`close == open`): **skip entirely** — neither arm nor trigger. Stay IDLE.

### ARMED → IDLE (deadline)

- If we reach the last allowable trigger bar (15:04 close) without triggering, release the armed strike → IDLE. No trade for that arm.
- The machine can re-arm later the same day if it scans a new bar with ATM close `<` `alert_price` and the scan is still within `[arm_start, arm_deadline]`. In practice this tail is narrow (15:00 arm cutoff vs 15:04 trigger cutoff) but it exists.

### OPEN → IDLE (exit)

While OPEN, watch the **locked strike's** bars. For each bar:

- If `low ≤ sl`:
  - Exit at exactly `sl`. Reason = `"SL"`.
- Else if `high ≥ tp`:
  - Exit at exactly `tp`. Reason = `"TP"`.
- **If both conditions in the same bar**: take SL (pessimistic tie-break).
- If we reach the `force_exit` bar (15:15):
  - Exit at that bar's `close`. Reason = `"EOD"`.

Record the trade. Return to IDLE.

### IDLE → ARMED (re-arm after exit)

Immediately after recording an exit:
- Re-check the **current ATM** row (at the exit bar's minute) for that instrument/option_type.
- If its `close` **strictly less than** `alert_price`, arm instantly using the current ATM's strike (same-bar re-arm).
- Otherwise stay IDLE and resume the normal IDLE → ARMED scanning on subsequent bars.

The just-exited strike is **not blocklisted** — it can be re-armed if it happens to still be the ATM strike and still below `alert_price`.

**Arm condition is always strict `<` `alert_price`**, whether first-arm or re-arm. Trigger condition is always strict `>` `entry_price`.

---

## Entry / Exit Price Mechanics

| Event | Price used | Source |
|-------|-----------|--------|
| Arm observation | bar `close` | locked strike's bar |
| Entry trigger signal | bar `close` | locked strike's bar |
| Entry fill | next bar `open` | locked strike's bar |
| SL fill | exactly `sl` (absolute price) | assumes fill at the level if wick touches |
| TP fill | exactly `tp` (absolute price) | assumes fill at the level if wick touches |
| EOD fill | 15:15 bar `close` | locked strike's bar |

---

## Daily Limits

No daily max-loss / max-SL limits in v1. NIFTY and SENSEX are fully independent — no shared cap, no shared trade counter.

---

## P&L Calculation

Long-only (we always BUY options):

```
pnl_points = exit_price - entry_price
pnl_inr    = pnl_points * LOT_SIZE[instrument] * lot_size
```

`LOT_SIZE` comes from `config.py` (NIFTY = 65, SENSEX = 20 per the current values). `lot_size` in JSON is a multiplier (1 by default).

---

## Trade Output Schema

One CSV per run at `gamma_blast_trades_<instruments>_<start>_<end>.csv`:

```
date                 YYYY-MM-DD
instrument           "NIFTY" | "SENSEX"
expiry_date          YYYY-MM-DD
option_type          "CE" | "PE"
strike               int
spot_at_arm          float   (spot column from option row at arm minute)
arm_time             HH:MM
arm_premium          float   (option close at arm bar)
spot_at_entry        float
entry_time           HH:MM
entry_price          float   (next-bar open)
entry_trigger_close  float   (close of the bar that triggered entry)
spot_at_exit         float
exit_time            HH:MM
exit_price           float   (sl / tp / 15:15 close)
exit_reason          "SL" | "TP" | "EOD"
pnl_points           float
pnl_inr              float
lot_size             int     (LOT_SIZE * json lot_size)
```

Stdout summary per run: total trades, wins, losses, win rate, total points, total INR, max winning / losing trade, per-instrument breakdown.

---

## UI Integration

Add Gamma Blast to the Streamlit strategy dropdown, following the pattern of other custom-engine strategies in `ui/backtest_runner.py`.

Form fields:
- Instrument toggle(s): checkbox for NIFTY, checkbox for SENSEX
- Per-instrument inputs (only shown when that instrument is toggled on): `alert_price`, `entry_price`, `sl`, `tp`
- Date range picker
- Lot size (default 1)
- Run button → invokes `engine.gamma_blast_backtest.run(config)` → shows trade table + summary stats + download button for CSV

---

## Testing Plan

`tests/test_gamma_blast.py` using **synthetic** 1-min bars (no parquet dependency) — each test runs in ms.

State-machine unit tests:
1. Arm on first bar with `close` one tick **below** `alert_price` (should arm, strict `<`).
2. Arm NOT triggered when `close == alert_price` (boundary; strict `<`, not `≤`).
3. Arm ignored when bar is before `arm_start`.
4. Arm ignored when bar is after `arm_deadline`.
5. Entry triggered on `close > entry_price`; entry price = next bar open; verify trade recorded.
6. Entry at boundary: `close == entry_price` should NOT trigger (strictly greater).
7. Same-bar green whip: one bar has `low ≤ alert_price`, `close > entry_price`, `close > open` → entry next bar.
8. Same-bar red whip: same conditions but `close < open` → no arm, no entry.
9. Gap-beyond-TP at entry open → skip, no trade.
10. Gap-below-SL at entry open → skip, no trade.
11. SL exit: candle `low < sl` on a later bar → exit at `sl`.
12. TP exit: candle `high > tp` → exit at `tp`.
13. Same-bar SL + TP → SL wins.
14. Armed but deadline expires → no trade.
15. Force-exit at 15:15 close.
16. Re-arm instantly after exit when current ATM already ≤ alert.
17. Re-arm waits when current ATM > alert.
18. CE and PE independence: both can be OPEN at the same minute.
19. Non-expiry day: no trades triggered even if data satisfies all conditions.
20. Strike lock: armed strike is tracked even after it ceases to be ATM.

Integration test:
- One SENSEX expiry day with real parquet data, golden-output comparison.

---

## File Changes

**New:**
- `engine/gamma_blast_backtest.py` — dedicated engine (~400-500 lines)
- `saved_strategies/gamma_blast.json` — config
- `tests/test_gamma_blast.py` — unit + integration tests

**Modified:**
- `config.py` — add SENSEX to `SPOT_DATA_PATH` (one line; cheap future-proofing)
- `ui/backtest_runner.py` — add Gamma Blast tab / form handler
- `ui/strategy_form.py` — extend strategy type enum if needed

**Untouched:**
- `engine/backtest.py` (generic engine)
- `engine/expiry_calendar.py` (reused as-is)
- `engine/data_loader.py` (reused as-is)

---

## Open Items / Future Work (not in v1)

- Slippage modeling on SL/TP fills (currently assumes exact fill at level).
- Daily max-loss cap if cross-instrument P&L needs linking.
- NIFTY level parameters to be filled in once user provides them.
- Live trading integration — out of scope; backtest only for v1.
