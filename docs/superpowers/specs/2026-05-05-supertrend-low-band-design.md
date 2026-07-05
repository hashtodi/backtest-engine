# SuperTrend Low-Band — Custom Backtest Engine Design

**Date:** 2026-05-05 (rev. 2026-05-05)
**Instrument:** NIFTY (v1)
**Scope:** Daily intraday strategy on nearest weekly expiry's ATM CE / PE

> **Revision notes (chronological):**
> 1. SuperTrend direction is computed on the option's **5-min** chart (forward-filled to 1-min), and the band check compares the **option's 1-min close** (not the ST value) to the morning low.
> 2. Band is asymmetric: trigger fires when `option_close ≤ morning_low × (1 + band_pct/100)`. Anything below the morning low qualifies (no lower bound).
> 3. **Strike-lock per 5-min block** for the entry scanner: at each 5-min boundary, lock the ATM strike using the ATM observed at the close of the previous 5-min bar; that strike is used for all 5 1-min entry checks in the next block. Open positions still lock to entry strike until exit.
> 4. **DTE-based TP/SL.** Replaces top-level `sl_pct`/`tp_pct` with a per-DTE table. DTE = trading days from the trade date (exclusive) to the nearest NIFTY weekly expiry (inclusive), using `config.get_nearest_weekly_expiry`. DTE values higher than the largest table key clamp at the largest key.

---

## Overview

Every trading day, mark each ATM contract's "morning low" — the lowest 1-min `low` printed during the 9:15–9:19 candles. From 09:20 onwards, scan the nearest-weekly ATM CE and ATM PE every minute. Buy the contract at the next minute's open when **both**:
1. The contract's continuous **5-min** SuperTrend(3,10) direction is bullish (forward-filled to the current 1-min bar), and
2. The option's **1-min close** sits within ±5% of that contract's morning low.

Hold until +10% (TP), −7.5% (SL), or 14:45 force-exit. CE and PE run as fully independent state machines; both can be simultaneously open. After exit, the same side resumes scanning immediately — same-day re-entry is unbounded.

The strategy bets on a counter-trend mean-revert near the day's early support, filtered by SuperTrend's bullish regime (on a slower timeframe) so we don't catch falling knives.

---

## Plain-language description (for verification)

**Setup at 9:20:**
- For both CE and PE of the nearest weekly expiry's ATM strike, record the **morning low** = min(`low`) across 1-min bars timestamped 9:15, 9:16, 9:17, 9:18, 9:19.
- Each contract has its own morning low, even if it wasn't ATM during 9:15–9:19.

**Watching from 9:20 onwards (every minute, CE and PE separately):**
1. Look at current spot → identify today's ATM CE and ATM PE.
2. For each side, look at:
   - That contract's **5-min** SuperTrend(3,10) direction (continuous across the contract's lifetime; the current 5-min bar's value forward-fills to all 1-min bars within that 5-min window).
   - The contract's current 1-min close.
3. Buy at next 1-min bar's open if **both**:
   - 5-min SuperTrend direction is bullish (price above ST line)
   - 1-min close is within ±5% of the contract's morning low

**Once bought (only one position per side at a time):**
- TP: option price ≥ entry × 1.10
- SL: option price ≤ entry × 0.925
- Force exit at 14:45 close

**Independence:** CE and PE run in totally separate state machines. Both can be open simultaneously. While a side is OPEN, that side's entry scanner is paused. After SL/TP/EOD exit, the scanner resumes immediately on the next bar — re-entry allowed and unbounded same-day.

**Strike lock:** A position locks the entry strike until exit. Even if spot moves and a different strike becomes ATM, the open position stays on its original strike. The other side's entry scanner can simultaneously evaluate a different ATM strike.

---

## Data Sources

| Source | Path | Usage |
|--------|------|-------|
| NIFTY options 1-min | `data/options/nifty/` (parquet) | Per-contract OHLC, ATM lookup, spot column embedded |
| Expiry calendar | `config.NIFTY_WEEKLY_EXPIRY_DATES` | Identify nearest weekly expiry per day |

Option rows include: `ts, datetime, underlying, option_type, expiry_type, expiry_code, atm_strike, strike_offset, moneyness, strike, spot, open, high, low, close, volume, oi, iv`.

Bar timestamp convention: `09:15:00` is the bar's OPEN time; the bar represents 09:15 → 09:16 OHLC. **Verified against parquet sample.** The "9:15 5-min candle" therefore comprises 1-min bars timestamped `[09:15, 09:16, 09:17, 09:18, 09:19]`, all closed by 09:20:00. An integration test will assert this convention and fail loud if the data is ever close-stamped.

ATM determination uses the existing `moneyness == 'ATM'` flag (point-in-time tag based on spot at that bar — no lookahead).

---

## Configuration (`saved_strategies/supertrend_low_band.json`)

```json
{
  "name": "supertrend_low_band",
  "instrument": "NIFTY",
  "supertrend": { "factor": 3, "atr_period": 10 },
  "first_5min_window": { "start": "09:15", "end": "09:20" },
  "band_pct": 5.0,
  "sl_pct": 7.5,
  "tp_pct": 10.0,
  "trading": {
    "scan_start": "09:20",
    "force_exit": "14:45"
  },
  "lot_size": 1,
  "backtest_start": "2025-01-01",
  "backtest_end":   "2026-04-30"
}
```

**Configurable from JSON / UI form:**
- SuperTrend `factor` and `atr_period`
- `band_pct` (the ±% band around morning low)
- `sl_pct`, `tp_pct`
- `first_5min_window.start` / `end` (the morning-low window)
- `trading.scan_start`, `trading.force_exit`
- `lot_size` (multiplier; engine multiplies by `config.LOT_SIZE['NIFTY']`)
- `backtest_start`, `backtest_end`

**Hardcoded in v1 (not configurable):**
- Instrument = NIFTY
- Bullish ST filter is always on
- Both CE and PE always run
- Same-day re-entry always allowed, no cooldown, no per-day caps

---

## Per-Day, Per-Side State Machine

Two state machines per day, one per option type. Each has two states: `IDLE` and `OPEN`.

### IDLE (scanner active for this side)

For each minute close `T` in `[scan_start, force_exit)`:

1. Look up the ATM-flagged row for this side at minute `T`:
   - `atm_row = day_df[(datetime == T) & (option_type == side) & (moneyness == 'ATM') & (expiry_code == 1)]`
   - If no row → skip this minute.
2. `atm_strike = atm_row.strike`
3. Get this contract's `morning_low` from the precomputed `(date, strike, side) → low` table.
   - If `morning_low` is NaN (contract had no bars in 9:15–9:19) → skip this side this minute.
4. Get this contract's ST values at minute `T`:
   - `st_value, st_dir = st_df[(strike, side, expiry_code, T)]`
5. Entry condition (all three must hold):
   - `st_dir == bullish` (5-min direction forward-filled to 1-min; direction == −1 in this codebase's convention)
   - `morning_low * (1 - band_pct/100) ≤ option_close[T] ≤ morning_low * (1 + band_pct/100)` where `option_close[T]` is the contract's 1-min close at bar `T`
   - Next bar `T+1m` for this strike exists and has a non-null `open`
6. If condition met:
   - Look up `next_open` from the same strike's row at `T+1m`.
   - Transition to OPEN with:
     - `entry_strike = atm_strike` (LOCKED)
     - `entry_price = next_open`
     - `entry_time = T + 1 minute`
     - `sl = next_open * (1 - sl_pct/100)`
     - `tp = next_open * (1 + tp_pct/100)`
     - record `entry_st_value`, `entry_trigger_close`, `morning_low`, band edges, `spot_at_entry`

### OPEN (locked strike)

For each minute `T` after entry, read the row for `entry_strike` at `T`:

1. **EOD check first**: if `T == force_exit` (14:45):
   - Exit at this bar's `close`, reason `"EOD"`. Transition to IDLE — but no further re-entry today since the scanner won't fire after force_exit.
2. Otherwise check intra-bar wick:
   - If `bar.low ≤ sl` → exit at exactly `sl`, reason `"SL"`.
   - Else if `bar.high ≥ tp` → exit at exactly `tp`, reason `"TP"`.
   - Same-bar SL+TP: SL wins (pessimistic tie-break).
3. After exit transition to IDLE — scanner resumes on the **next** bar. Re-entry can fire at `T+1`'s close as soon as conditions are met.

### Per-minute ordering at bar `T`

Within a single minute the engine processes in this exact order:
1. Read bar `T`'s row(s) for the locked strike (if OPEN) and the ATM strike (always).
2. **OPEN path first**: evaluate exit. May transition OPEN → IDLE.
3. **IDLE path second** (including just-flipped from step 2): evaluate entry on bar `T`'s close-time data.
4. If entry triggers, the fill happens at bar `T+1`'s `open` — which is consumed when the next iteration of the loop processes `T+1`. We do NOT peek at `T+1`'s close/high/low.

### Force-exit safety net

Any position still OPEN at the very last bar of the day is force-closed at that bar's `close` with reason `"EOD"`. (Belt-and-braces, since the explicit 14:45 check should already handle it.)

---

## CE / PE Independence

The two state machines maintain completely separate state. Both can be `OPEN` simultaneously (effectively a long straddle when the conditions align). The CE machine reads only CE rows; the PE machine reads only PE rows. The other side's open position has no effect on this side's entry scanner.

---

## Pre-Computation Pipeline

Performed once at the start of the run for the whole `[backtest_start, backtest_end]` range:

1. **Load options data**: `engine.data_loader.load_data(path, start, end, "weekly")` for NIFTY, filter to `expiry_code == 1`.
2. **Continuous 5-min SuperTrend per contract** (forward-filled to 1-min): group by `(strike, option_type, expiry_type, expiry_code)`, sort by `datetime` ascending. For each contract: filter to market hours (`between_time("09:15","15:29")`), resample 1-min OHLC → 5-min (open=first, high=max, low=min, close=last, drop empty), compute `SuperTrend(factor=3, atr_period=10)` continuously across the 5-min series (no daily reset — matches TradingView). Shift the 5-min indicator's index by +5 minutes so each 5-min bar's ST is keyed at the END of the bar (i.e., next bar's open time), then forward-fill onto the 1-min datetimes. Adds `st_value` and `st_dir` columns. With atr_period=10, the first valid 5-min ST is at the 11th 5-min bar; after the +5min shift, this becomes available roughly 55 minutes into the contract's lifetime.
3. **Per-(date, contract) morning low**: filter rows where `time_str` ∈ `["09:15", "09:16", "09:17", "09:18", "09:19"]`, group by `(date, strike, option_type, expiry_type, expiry_code)`, compute `min(low)`. Materialize into a dict keyed by `(date, strike, option_type)` for O(1) lookup.
4. **ATM index per minute**: filter rows where `moneyness == 'ATM'`, build a dict keyed by `(datetime, option_type) → strike`.
5. **Per-strike-and-minute index**: for fast row lookup during state-machine iteration, set a multi-index `(strike, option_type, datetime)` on the day's filtered DataFrame.

Then iterate trading day by trading day, running two machines per day on the precomputed structures.

---

## P&L Calculation

Long-only:

```
pnl_points = exit_price - entry_price
pnl_inr    = pnl_points × LOT_SIZE['NIFTY'] × json.lot_size
```

`LOT_SIZE['NIFTY']` comes from `config.py`. JSON `lot_size` is a multiplier (default 1).

---

## Trade Output Schema

Per-trade row written to `supertrend_low_band_trades_<start>_<end>.csv`:

| Column | Type | Description |
|---|---|---|
| `date` | string | YYYY-MM-DD |
| `instrument` | string | "NIFTY" |
| `expiry_date` | string | Nearest weekly expiry on the trade date |
| `option_type` | string | "CE" / "PE" |
| `strike` | int | Locked entry strike |
| `morning_low` | float | Contract's 9:15-9:19 min(low) |
| `band_low` | float | `morning_low × (1 − band_pct/100)` |
| `band_high` | float | `morning_low × (1 + band_pct/100)` |
| `spot_at_entry` | float | Spot column on the entry-bar option row |
| `entry_time` | string | HH:MM (T+1) |
| `entry_price` | float | Next-minute open of locked strike |
| `entry_st_value` | float | ST value at trigger bar T close |
| `entry_trigger_close` | float | Option close at trigger bar T |
| `spot_at_exit` | float | Spot column on the exit-bar row |
| `exit_time` | string | HH:MM |
| `exit_price` | float | Exact SL/TP level OR 14:45 close |
| `exit_reason` | string | "SL" / "TP" / "EOD" |
| `pnl_points` | float | `exit_price − entry_price` |
| `pnl_inr` | float | `pnl_points × LOT_SIZE × json.lot_size` |
| `lot_size` | int | `LOT_SIZE × json.lot_size` |

Stdout summary: total trades, wins, losses, win rate, total points, total ₹, by-side breakdown.

---

## No-Lookahead / Bias Audit

| Decision point | Data read | Available at decision time? |
|---|---|---|
| Compute morning low | min(`low`) of bars 9:15–9:19 | Yes — all 5 bars closed by 09:20 |
| First scan at 9:20 close | bar 9:20 close, ST(3,10) at 9:20 close, spot at 9:20, ATM-flagged row | Yes — bar 9:20 fully closed at decision |
| ST(3,10) at bar `T` | `close[0..T]`, `high[0..T]`, `low[0..T]` of contract | Yes — only past+current bars used (Wilder's ATR with `close[T-1]` for TR) |
| ATM strike at bar `T` | `spot[T]` from option row | Yes — point-in-time lookup |
| Entry fill price | bar `T+1`'s `open` only | Observed when bar `T+1` arrives. We do NOT use `T+1`'s close/high/low to make the entry decision. |
| SL / TP intra-bar | bar `T`'s `low` / `high` / `close` | Yes — all known at bar `T` close |
| Same-bar SL+TP | tie-break "SL wins" | Pessimistic, not biased |
| Force exit at 14:45 | bar 14:45's `close` | Yes — known at 14:45:00 end |
| Re-entry after exit | scanner runs on already-closed bars | Yes |

**Survivorship:** None — weekly nearest-expiry contracts always exist; ATM is a point-in-time tag.

**Selection:** None — all weekly expiry days in the range are processed. No cherry-picking.

### Realism caveats (NOT lookaheads — disclosed assumptions)

- **SL/TP fill at exact level when wick touches.** Realistic for liquid ATM weeklies; can slip on fast moves. Same convention as Gamma Blast.
- **No transaction costs / slippage / spreads** are modeled. Standard backtest assumption.
- **No volume / liquidity filtering.** If a strike becomes ATM hours later but had near-zero volume at 9:15-9:19, its morning low may be a stale tick. The `skip-side when morning low is NaN` rule covers the worst case but not low-volume-but-non-NaN cases.

---

## Edge Cases

| Case | Behavior |
|---|---|
| Contract has no bars in 9:15-9:19 | morning_low = NaN → skip side this minute (resume when ATM shifts to a contract with valid morning low) |
| Bar `T+1` row is missing for the locked strike | Skip entry trigger this minute (rare data gap) |
| Bar `T` row is missing for the locked strike during OPEN | Skip exit check this minute; re-evaluate next minute. Force-exit safety net catches anything still open at EOD. |
| ATM strike is NaN at minute `T` (shouldn't happen but guard) | Skip side this minute |
| ST value is NaN at minute `T` (early in contract life — first ~11 bars of a freshly listed contract) | Skip side this minute |
| Position opens at 14:45 | Cannot — scanner is gated by `T < force_exit`. Last possible entry is the bar that fills at 14:45 open, i.e., trigger bar = 14:44 close. |
| Position opens at 14:44, doesn't hit SL/TP by 14:45 | Force-exit at 14:45 close |
| Same-bar SL and TP both touched | SL wins (pessimistic) |

---

## UI Integration

Add a new tab to the Streamlit app in `app.py` named `tab_st_low_band` calling `render_supertrend_low_band_backtest()`. The runner module `ui/supertrend_low_band_backtest_runner.py` mirrors `ui/gamma_blast_backtest_runner.py`:

- Form fields:
  - SuperTrend factor (number)
  - SuperTrend atr_period (number)
  - Band % (number, default 5.0)
  - SL % (number, default 7.5)
  - TP % (number, default 10.0)
  - First-5min window start / end (text HH:MM, defaults 09:15 / 09:20)
  - Scan start (text, default 09:20)
  - Force exit (text, default 14:45)
  - Lot size multiplier (int)
  - Date range
  - Run button
- Results pane:
  - Summary metrics: trades, win rate, total points, total ₹
  - Equity curve via `st.line_chart(cumulative_pnl_by_trade)`
  - Trades dataframe with filter widgets (option_type, exit_reason, date)
  - CSV download button

---

## Testing Plan

`tests/test_supertrend_low_band.py` using **synthetic** 1-min bars (no parquet dependency). Each test runs in milliseconds.

**Pure-function unit tests:**
1. `compute_first_5min_low(df, window)` — returns min of `low` across bars in window; NaN when no bars
2. `is_in_band(value, low, band_pct)` — boundary tests at exactly `low * 0.95` and `low * 1.05`; both ends inclusive
3. `evaluate_entry(st_val, st_dir, low, band_pct, bullish_required=True)` — full truth table: bullish/not × in-band/not
4. `evaluate_exit(bar_high, bar_low, bar_close, sl, tp, is_force_exit_bar)` — SL / TP / EOD / same-bar SL+TP precedence

**State-machine tests** (one side, one day, synthetic bars):

5. Entry fires correctly: ST drops into band on bar T close → entry at T+1 open
6. No entry when ST is in band but bearish
7. No entry when ST is bullish but outside band
8. No entry when one of bar T+1 fields is missing (data gap)
9. Boundary: ST exactly at `low * 0.95` → entry fires (inclusive)
10. Boundary: ST exactly at `low * 1.05` → entry fires (inclusive)
11. SL exit fires intra-bar on wick (bar.low ≤ sl)
12. TP exit fires intra-bar on wick (bar.high ≥ tp)
13. Same-bar SL+TP → SL wins
14. Force exit at 14:45 closes any open position at that bar's close
15. Re-entry: after exit, next bar's ST in-band + bullish → new entry at the bar after that
16. ATM-shift mid-day: spot moves at T, ATM strike changes; OPEN position holds the original strike; entry scanner sees the new strike's morning low
17. CE and PE independence: both can be OPEN simultaneously; one side's exit doesn't affect the other
18. Skip side when contract has no 9:15-9:19 data (morning_low is NaN)
19. Skip side when ST value is NaN (early in contract life)
20. End-of-day safety net: position open at last bar gets force-closed at that bar's close

**Integration tests:**

21. Bar-timestamp convention assertion: load one real day, verify rows exist at `09:15:00` (open-stamp) and not at `09:16:00`-only (close-stamp); fail loud if the convention has changed.
22. One real NIFTY trading day from parquet, golden-output trade list comparison.

---

## File Changes

**New:**
- `engine/supertrend_low_band_backtest.py` (~400 lines)
- `saved_strategies/supertrend_low_band.json`
- `ui/supertrend_low_band_backtest_runner.py`
- `tests/test_supertrend_low_band.py`

**Modified:**
- `app.py` — add `tab_st_low_band` to the tab list, wire to `render_supertrend_low_band_backtest()`

**Untouched:**
- `engine/data_loader.py`, `engine/backtest.py`, `indicators/supertrend.py`, `config.py`

---

## Open Items / Future Work (out of v1)

- SENSEX support (parametrize `instrument`, swap expiry calendar and lot size).
- Optional bearish ST mode (sell or buy on bearish band touch — currently not in scope).
- Per-side caps and daily loss circuit-breaker (deliberately deferred — user wants clean v1).
- Volume / liquidity filtering on the morning low computation.
- Slippage modeling on SL/TP fills.
- Per-day drill-down chart (option price + ST line + band shading + entry/exit markers) — equity curve only in v1.
- Live trading integration — out of scope; backtest only for v1.
