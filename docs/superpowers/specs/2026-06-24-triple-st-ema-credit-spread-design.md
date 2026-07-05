# Triple-SuperTrend + EMA9/21 Credit-Spread Backtest Engine

**Date:** 2026-06-24
**Status:** Design approved, pending spec review
**Author:** Harsh (design assisted by Claude)

## 1. Summary

A NIFTY intraday, defined-risk **credit-spread** engine driven by a triple-SuperTrend
multi-timeframe regime filter plus a 1-minute EMA9/EMA21 crossover trigger
(translated from the user's TradingView Pine "Triple Supertrend + EMA9/21 Cross"
strategy).

- **LONG signal** (all 3 SuperTrends green/up + EMA9 crosses **above** EMA21)
  → **bull-put credit spread**: SELL PE 2-strikes-OTM, BUY PE 6-strikes-OTM.
- **SHORT signal** (all 3 SuperTrends red/down + EMA9 crosses **below** EMA21)
  → **bear-call credit spread**: SELL CE 2-strikes-OTM, BUY CE 6-strikes-OTM.

Exits are absolute-INR per-lot TP/SL on the live spread mark-to-market, plus an
end-of-day square-off. The engine follows the functional, config-dict style of
`engine/oi_wall_backtest.py` and `engine/bb_pivot_spread_backtest.py`.

## 2. Goals / Non-goals

**Goals**
- Faithfully reproduce the Pine signal logic (3m/5m/10m SuperTrend alignment +
  1m EMA cross) with **zero look-ahead**.
- Map the directional signal onto the user's credit-spread structure.
- Honor the user's execution rules: enter on the signal minute's close; INR
  per-lot TP (₹800) / SL (₹650); nearest weekly expiry with expiry-day roll to
  the next weekly.
- Ship as a drop-in peer of the existing spread engines: engine module + pytest
  suite + Streamlit UI tab.

**Non-goals**
- Live trading / order routing (backtest only).
- Re-implementing SuperTrend / EMA (reuse `indicators/`).
- Tuning / optimizing the parameters — defaults mirror the Pine script.

## 3. Inputs & Data

| Concern | Source | Notes |
|---|---|---|
| Signal series (SuperTrend, EMA, ATM) | `data/spot/nifty/NIFTY_1m.parquet` | true 1-min OHLCV, start-stamped |
| Option premiums | `data/options/nifty/NIFTY_OPTIONS_1m.parquet` | per-contract OHLC; has `strike`, `strike_offset`, `atm_strike`, `expiry_type`, `expiry_code`, `spot` |
| Expiry calendar | `config.get_nearest_weekly_expiry` | NIFTY weekly expiries 2025–2026 |
| Lot size | `config.LOT_SIZE['NIFTY']` = 65 | |
| Strike grid | `config.STRIKE_ROUNDING['NIFTY']` = 50 | 1 strike = 50 pts |

**Bar-timestamp convention (critical):** rows are **start-stamped**. A row
labeled `T` covers clock `[T, T+1min)` and its close is known at clock `T+1min`.
Verified: a trading day runs `09:15 … 15:29`.

## 4. Signal logic (continuous across days, no look-ahead)

All indicators are computed over the **continuous** series (no daily reset),
exactly like `bb_pivot_spread`. Warm-up trading days are loaded before
`backtest_start` so SuperTrend/EMA are seeded from day one.

### 4.1 Multi-timeframe SuperTrend regime

Two modes, selected by `signal.htf_mode` (default **`rolling`**):

- **`rolling` (trailing window, per-minute):** at each 1-min bar `T` the
  tf-minute bar is the sliding window `[T-tf+1 … T]` (high = trailing max, low =
  trailing min, close = `close[T]`); SuperTrend runs on that minute-by-minute
  series, so the direction **refreshes every minute**. The window only uses data
  `≤ T`, so it is known at `T`'s close — **no look-ahead, no availability shift**
  (direction at `T` is attached directly to bar `T`). Because the windows
  overlap, the True-Range/ATR is a rolling-range volatility: a faster, smoother
  variant, **not identical** to a real tf-chart SuperTrend. Rolling is by ROW
  over the continuous series; the first few minutes of each day blend the prior
  session's tail (well before the entry window). Impl:
  `_attach_htf_dir_rolling`.
- **`anchored` (steps at boundaries):** the original non-overlapping tf-min bars
  described below.

The remainder of this section describes the **`anchored`** mode.

1. Resample the 1-min spot into **3m / 5m / 10m** OHLC bars, anchored at the
   session open **09:15** (`resample(origin="start_day", offset="9h15min")`;
   1440 min is divisible by 3/5/10 so the grid re-anchors to 09:15 every day).
2. Run `indicators.SuperTrend(factor=3.0, atr_period=12)` on each timeframe's
   continuous close/high/low. `direction == -1` ⇒ uptrend (green);
   `direction == +1` ⇒ downtrend (red). (Matches the codebase + the Pine
   `dir < 0 = uptrend` convention.)
3. Attach the **last completed** HTF direction onto each 1-min bar, matching
   TradingView's `request.security(..., lookahead_off)` semantics: an HTF bar's
   direction only becomes visible on the 1-min candle that **opens at/after**
   the HTF bar has closed.

   > HTF bar with start label `s` is usable at 1-min bar `T`
   > **iff `s + tf ≤ T`** (the HTF bar must have fully closed by the time the
   > 1-min bar `T` opens). Implemented as `avail.index = htf_start + tf`,
   > `decision = onemin_start`, `reindex(decision, method="ffill")`. (This is one
   > minute more conservative than `dema_mtf_vwap`'s rule, because our "current
   > bar" is a 1-min bar evaluated at its own close, not a 5-min bar.)

   **Worked example — the EMA cross on the `12:35` candle** (parquet row `12:35`,
   covering 12:35:00–12:35:59; the cross is confirmed at its close and we enter
   at that close). At row `12:35` the freshly-completed HTF candles available are:
   - 5m candle `12:30–12:34` (rows 12:30…12:34, closed at 12:35:00 = row 12:35's
     open) → `12:30+5 = 12:35 ≤ 12:35`. ✅
   - 10m candle `12:25–12:34` (closed 12:35:00) → `12:25+10 = 12:35 ≤ 12:35`. ✅
   - 3m freshest is `12:30–12:32` (closed 12:33); the next 3m candle `12:33–12:35`
     only closes at 12:36, so it is **not** used at row `12:35`. ✅
   - The 5m/10m candles that *start* at row `12:35` are still forming → **not** used.

   `regime = LONG` when dir3m == dir5m == dir10m == -1;
   `regime = SHORT` when all == +1; otherwise `NONE`.

### 4.2 EMA9/EMA21 trigger
- `EMA(9)` and `EMA(21)` on the continuous 1-min close.
- `long_cross[T]`  = `ema9[T] > ema21[T]` **and** `ema9[T-1] <= ema21[T-1]`.
- `short_cross[T]` = `ema9[T] < ema21[T]` **and** `ema9[T-1] >= ema21[T-1]`.

### 4.3 Combined entry signal (evaluated at each 1-min bar `T`'s close)
- LONG signal  = `regime == LONG`  and `long_cross[T]`.
- SHORT signal = `regime == SHORT` and `short_cross[T]`.
- Gated to the **entry window** (default `09:30`–`14:45`, inclusive on the
  bar's close), one position at a time (see §6).

## 5. Strike selection & spread structure

At the signal bar `T`:
1. `atm = round(spot_close[T] / 50) * 50` — from the **spot** feed (per the
   user's instruction to use the spot parquet for spot/ATM/strike).
2. Derive absolute strikes (1 strike = 50 pts; OTM 2 = 100 pts, OTM 6 = 300 pts):

   | Signal | Spread | Sell leg | Buy leg |
   |---|---|---|---|
   | LONG | bull-put | SELL PE `atm − 100` | BUY PE `atm − 300` |
   | SHORT | bear-call | SELL CE `atm + 100` | BUY CE `atm + 300` |

3. Look up both legs by **absolute `strike`** in the chosen-expiry options slice
   at bar `T`. Entry premiums = those rows' **close** at `T`.
4. **Skip** (logged, counted) the signal if either leg row is missing or the net
   credit `sell_entry − buy_entry ≤ 0`.

`sell_offset_abs` (2), `buy_offset_abs` (6) are configurable.

## 6. Execution & exits (no look-ahead)

- **Entry fill:** the signal bar `T`'s **CLOSE** premiums (no next-minute delay) —
  the user's explicit rule, and identical to `bb_pivot_spread`'s entry semantics.
- **Position cap:** **one open spread at a time; unlimited re-entries per day.**
  A new signal while in a trade is ignored; after an exit, a later signal the
  same day can open a fresh spread. (`max_trades_per_day = 0` ⇒ unlimited;
  configurable.)
- **TP / SL (absolute INR, per lot, scaled by lots):** on every 1-min option
  close **after** entry, compute the live spread P&L:

  ```
  live_inr = ((sell_entry + buy_now) - (sell_now + buy_entry)) * lot_size * lots
  TP hit when live_inr >=  tp_inr * lots     (default tp_inr = 800)
  SL hit when live_inr <= -sl_inr * lots     (default sl_inr = 650)
  ```

  Detection is at bar `t`'s **close**; the exit **fills at the next 1-min bar's
  OPEN** (`t+1`), matching `oi_wall`/`bb_pivot`. SL is checked before TP (they
  cannot both fire on one spread). `tp_inr`/`sl_inr = 0` disables that leg.
- **EOD square-off:** force-close any open spread at **15:15** (configurable) at
  that minute's CLOSE, reason `EOD`. If data ends before the deadline, close on
  the last available minute.

**P&L (per spread):**
```
pnl_pts = (sell_entry + buy_exit) - (sell_exit + buy_entry)
pnl_inr = pnl_pts * lot_size * lots
```

## 7. Expiry handling

- Load weekly options **codes 1 and 2**.
- Per trading day: if the day is a weekly-expiry day
  (`get_nearest_weekly_expiry(day) == day`) → trade `expiry_code 2` (next
  weekly, keeps time value); otherwise `expiry_code 1` (nearest weekly).
- Mirrors `bb_pivot_spread._expiry_code_for` and the lessons.md guidance.

## 8. Configuration schema (saved_strategies / UI)

```jsonc
{
  "backtest_start": "YYYY-MM-DD",
  "backtest_end":   "YYYY-MM-DD",
  "signal": {
    "st_factor": 3.0, "st_atr_period": 12,
    "htf_mode": "rolling",            // "rolling" (default) | "anchored"
    "tf1_min": 3, "tf2_min": 5, "tf3_min": 10,
    "ema_fast": 9, "ema_slow": 21,
    "warmup_days": 10
  },
  "entry": { "window_start": "09:30", "window_end": "14:45" },
  "exit":  { "tp_inr": 800.0, "sl_inr": 650.0, "square_off_time": "15:15" },
  "structure": { "lots": 1, "sell_offset_abs": 2, "buy_offset_abs": 6,
                 "max_trades_per_day": 0 },
  "expiry": { "expiry_type": "WEEK", "expiry_roll": true },
  "sizing": { "reference_capital": 200000 }
}
```

## 9. Module structure

`engine/triple_st_ema_spread_backtest.py`, mirroring the reference engines:

- `LegFill`, `TripleStEmaTrade`, `TripleStEmaDayContext` dataclasses.
- `build_htf_supertrend(spot_1m, tf, factor, atr_period)` → per-tf direction frame.
- `build_signals(spot_1m, params)` → per-1-min-bar frame with the three attached
  HTF directions, `ema9`/`ema21`, `regime`, `long_cross`/`short_cross`,
  `long_sig`/`short_sig`, `in_window`, keyed by `(date, time)`.
- `lookup_by_strike`, `atm_from_spot` helpers.
- `run_one_day(day_options, day_signals, ctx)` → `(trades, skipped)` — the
  one-open/unlimited-re-entry scan with the entry-at-close + next-min-open-exit
  fill rules.
- `load_filtered_options(...)` (codes 1+2, warm-up, session window),
  `load_spot_1m(...)`.
- `parse_config`, `run_backtest`, `summarize_metrics`, `build_equity_curve`,
  `trades_to_dataframe`, `write_trades_csv`, `write_equity_csv`,
  `print_summary`, `run(config, options_path, spot_path, output_dir)`.

## 10. Testing (`tests/test_triple_st_ema_spread.py`)

1. **HTF attachment / no look-ahead:** the 12:35 case — assert that at the
   signal bar (parquet row `12:35`) the 5m candle `12:30–12:34` and 10m candle
   `12:25–12:34` are attached, the 3m used is `12:30–12:32`, and the HTF candles
   starting at row `12:35` (still forming) are excluded.
2. **Regime gating:** all-3-aligned required; a single mismatched timeframe
   blocks the signal.
3. **Cross detection:** long/short cross fires only on a fresh crossover, gated
   by regime and the entry window.
4. **Structure:** LONG → SELL PE atm−100 / BUY PE atm−300; SHORT → SELL CE
   atm+100 / BUY CE atm+300; non-positive credit skipped.
5. **Exits:** INR TP (₹800/lot) and SL (₹650/lot) detect-at-close /
   fill-next-open; EOD square-off at 15:15 close; `tp_inr=0` disables TP.
6. **Re-entry:** a second signal after the first exits opens a second spread the
   same day; a signal while in-trade is ignored.
7. **Expiry roll:** expiry-day uses code 2, other days code 1.

## 11. Deliverables

1. `engine/triple_st_ema_spread_backtest.py`
2. `tests/test_triple_st_ema_spread.py`
3. `ui/triple_st_ema_spread_backtest_runner.py`
4. `app.py` — import + register a `Triple ST + EMA Spread` tab.

## 12. Risks / open points

- **Spot-derived ATM vs options `atm_strike`:** computing ATM from spot and
  looking up absolute strikes can, in rare rounding-boundary cases, differ by
  one 50-pt strike from the options parquet's own `atm_strike`. Strikes are
  dense around ATM so misses are rare; a missing leg → skip (logged). Honors the
  user's "use spot for ATM/strike" instruction.
- **3m alignment to 09:15:** TradingView anchors NSE intraday HTF bars to the
  session; we replicate with the 09:15 origin. 12:35 (and other non-multiples of
  3 from 09:15) are intentionally *not* 3m boundaries.
