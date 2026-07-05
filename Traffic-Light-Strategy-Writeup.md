# Traffic Light Strategy — As-Built Writeup

> **Purpose of this doc:** Lay out every decision the engine makes, every place where the original spec had ambiguity, and how it was resolved. Read top to bottom with the strategy designer — anything that doesn't match his intent is a 1-line change.

**Instrument:** NIFTY (1-min spot for pattern + filter; 1-min nearest-weekly options for fills)
**Trading window:** 09:15 → 14:45 IST
**Direction:** Long calls (CE) or long puts (PE)
**Concurrency:** One trade at a time

---

## 1. The signal in plain English

1. **Watch NIFTY spot.** Every minute, look at the last two 1-min candles.
2. **If they're opposite colors** (green-then-red, or red-then-green) → this is a *traffic light pair*. Mark:
   - `pair_high` = max(high of bar 1, high of bar 2)
   - `pair_low` = min(low of bar 1, low of bar 2)
3. **Check the TAIL filter** at the close of bar 2 (more on this below). If both CE and PE are blocked, throw away the pair and keep scanning. Otherwise lock the levels in memory.
4. **Wait for a breakout.** From here on we ignore any new opposite-color pairs forming. We only react to:
   - Spot 1-min close **strictly above** `pair_high` → CE breakout (long calls)
   - Spot 1-min close **strictly below** `pair_low` → PE breakdown (long puts)
5. **Enter** at the *next* 1-min bar's option open, at the ATM strike (with an OTM walk if too expensive — see §5).
6. **Exit** when spot wicks the SL or TP (next-bar option open fill), or force-exit at 14:45 option close.
7. **Repeat.** Once the trade resolves, resume scanning for the next opposite-color pair.

---

## 2. Pair detection (spot 1-min)

| Rule | Decision |
|---|---|
| What defines green / red? | `close > open` = green; `close < open` = red; `close == open` = **doji** |
| Doji participation | **Doji bars never form a pair.** A doji followed by red, or green followed by doji, etc. → no pair. |
| Lookback | Exactly the **previous bar** and the **current bar**. Rolling 2-bar window every minute. |
| When the pair is "formed" | At the **close** of the second bar. |
| Pair levels | `pair_high = max(high₁, high₂)`, `pair_low = min(low₁, low₂)` — over the two pair bars only. |
| Lifecycle | **Lock-first.** Once a pair is armed (or a trade is open), all new opposite-color pairs are *ignored* until the current pair resolves. The original levels stay in force. |
| Validity window | Pair stays armed until breakout / both-blocked at formation / 14:45 force-exit. No timed expiry; spot wicking through pair_high or pair_low without closing across does **not** invalidate it. |

**Subtle point — bar 2 can't be its own breakout.** Because pair_high includes bar 2's high (and pair_low its low), and bar 2's close ≤ its own high, the close can't be strictly above pair_high at the formation bar. So the earliest a breakout can fire is bar 2's *successor*.

---

## 3. TAIL filter (RSI + EMA, evaluated only at pair formation)

Computed on the **same spot 1-min series** used for pair detection. Both indicators are continuous across day boundaries (matches TradingView default — they don't reset at 09:15).

- **RSI(14)** using Wilder's smoothing
- **EMA(15)** using `ewm(span=15, adjust=False)`

At the close of pair-formation bar (bar 2), the engine evaluates both filters once. **The decision is fixed for the entire life of that pair** — never re-evaluated at breakout time.

### CE side (long calls) — blocked iff:
- RSI(14) strictly above the overbought threshold (default 70) on **both** pair bars **AND**
- Spot close on bar 2 ≤ EMA(15) on bar 2

### PE side (long puts) — blocked iff:
- RSI(14) strictly below the oversold threshold (default 30) on **both** pair bars **AND**
- Spot close on bar 2 ≥ EMA(15) on bar 2

### Combination outcomes
- **Both blocked** → discard pair entirely, keep scanning
- **CE blocked, PE not** → arm pair with only PE side active (any CE-direction breakout is ignored; only PE breakdown triggers a trade)
- **PE blocked, CE not** → arm pair with only CE side active
- **Neither blocked** → arm pair with both sides active; whichever fires first wins, the other is automatically cancelled

### Exact comparison semantics (strict vs ≤)
| Condition | Strictness |
|---|---|
| RSI > 70 (overbought) | **Strict** `>` — RSI exactly 70 does NOT count as overbought |
| RSI < 30 (oversold) | **Strict** `<` — RSI exactly 30 does NOT count as oversold |
| Close vs EMA (CE block) | **Non-strict** `close ≤ EMA` — equality blocks |
| Close vs EMA (PE block) | **Non-strict** `close ≥ EMA` — equality blocks |
| RSI overbought "for 2 consecutive bars" | Means RSI on **bar 1 AND bar 2** of the pair, both > threshold. NOT a rolling 2-bar window outside the pair. |

### Warmup
RSI(14) and EMA(15) are undefined until ~15 bars of history. Treatment: **NaN on either indicator → both filters block (pair undecidable)**. In practice this is a non-issue: indicators are computed continuously across day boundaries, so by the 09:25 scan_start (default) they are already well-warmed from prior days.

---

## 4. Breakout trigger

| Rule | Decision |
|---|---|
| What counts as a break? | Spot 1-min **close** strictly beyond the level. `close > pair_high` (CE) or `close < pair_low` (PE). Wicks alone do **not** trigger. |
| Equality (close exactly at level) | Does **not** trigger. Strict `>` and `<` only. |
| Where in time? | At the **close** of any 1-min bar after the pair is armed, while bar's time ≤ `entry_deadline` (14:44). |
| Both sides break same bar? | Impossible — a single bar's close can't be both above pair_high and below pair_low. |
| One armed side breaks the other's direction? | E.g. only PE is armed (CE was filtered), and spot closes above pair_high. **Ignored.** The pair stays armed waiting for PE breakdown. |

---

## 5. Strike selection at breakout

Strike selection happens entirely at the **entry bar** — the bar *after* the breakout signal. ATM, premium budget check, and fill price are **all anchored to the same observable bar**. The flow:

1. **Trigger bar N close** — engine detects breakout. Locks in: side (CE/PE), pair levels, SL/TP. **No strike chosen yet.**
2. **Entry bar N+1 open** — engine computes:
   - `ATM = round(spot_at_entry_bar_open / 50) * 50`
   - This is the engine's source of truth (not the options data's `moneyness=ATM` tag — avoids any half-strike rounding disagreement between Python's banker's rounding and the data feed's rounding).
3. **Walks OTM strikes** in the user-configured range:
   - For **CE**: try strikes ATM, ATM+50, ATM+100, … up to ATM + 50*max_otm_offset
   - For **PE**: try strikes ATM, ATM−50, ATM−100, … down to ATM − 50*max_otm_offset
4. **Premium budget check** at each candidate, using the **entry bar's option open** for that strike (= the actual price you'd pay):
   ```
   option_open_at_entry_bar × lot_size < premium_budget_inr   (strict <)
   ```
   With defaults: `option_open × 65 < ₹10,000` → first strike whose entry-bar open premium < ₹153.85 wins.
5. **Fill price = the same value** that just passed the budget check — entry-bar option open at the chosen strike.
6. **If no strike fits** within `[min_otm_offset, max_otm_offset]` → skip the trade entirely. State returns to IDLE, scanning resumes (see §8).

For reference, the trade record also stores `trigger_option_close` (option close at trigger bar N for the chosen strike) — informational only, useful for analyzing how much the premium moved between trigger and entry.

### Why entry-bar consistent?

The strike you actually buy is the strike that's ATM *at the moment you place the order*. The budget cap (₹10,000) should be enforced against the *price you actually pay* (entry-bar open), not against an earlier reference price that may have shifted between trigger and entry. This is the most internally consistent design: ATM, budget check, and fill all reference the same bar.

Edge case where this matters: if spot crosses a strike boundary between bar N close (23524.50) and bar N+1 open (23525.75), the ATM flips from 23500 to 23550. The whole strike walk reorients. This matches the natural mental model of "ATM is what's near spot right now."

### Configurable knobs
- `min_otm_offset` (default 0): skip ATM and start at this offset. E.g., 2 means "always at least ATM±2 OTM."
- `max_otm_offset` (default 4): stop searching here.
- `premium_budget_inr` (default 10000): the INR cap.

### ⚠ Nuance to confirm with designer
- **Lot size in budget check.** Engine uses *one* lot size (`65` for NIFTY) regardless of the JSON `lot_size` multiplier. So if you set `lot_size: 2` (2 lots), the budget check is still `premium × 65 < 10,000`, not `premium × 130 < 10,000`. This matches the literal "per-lot ₹10,000" reading. If the designer meant "total INR spend across all lots ≤ ₹10,000" we'd need to multiply.

---

## 6. Entry mechanics

| Step | Time | Used for |
|---|---|---|
| Breakout signal | Bar N's close | `spot.close > pair_high` (CE) or `< pair_low` (PE); engine locks side + SL/TP |
| ATM derivation | Bar **N+1**'s open | `ATM = round(spot_at_entry_open / 50) * 50` |
| Premium budget walk | Bar **N+1**'s option open | `option_open × 65 < ₹10,000` |
| Entry fill | Bar N+1's open | Same option_open value that just passed the budget check |
| Reported `spot_at_entry` | Bar N+1's spot open | — |

The 1-minute delay between signal and fill is intentional and prevents any look-ahead bias — at bar N's close the engine cannot see bar N+1's open, so it can't pre-pick the strike that will be ATM at N+1.

---

## 7. Exit mechanics

### SL and TP levels (set at entry, on spot)

| Side | SL (spot) | TP (spot) |
|---|---|---|
| CE | `pair_low − sl_buffer` | `pair_high + range × rr_ratio` |
| PE | `pair_high + sl_buffer` | `pair_low − range × rr_ratio` |

where `range = pair_high − pair_low`, `sl_buffer` defaults to 0, `rr_ratio` defaults to 1.2.

So with defaults: SL distance ≈ range, TP distance = range × 1.2. **The 1.2 RR is on spot points, not on option-premium INR P&L.** Premium moves are non-linear (delta, gamma, theta) so the realised INR RR will differ — this is a deliberate strategy property, not a bug.

### Exit detection (wick-based, on spot)

At every bar after entry, the engine checks the **spot** bar's high/low:

| Side | SL triggers when… | TP triggers when… |
|---|---|---|
| CE | `spot.low ≤ sl_spot` (any wick touches SL level) | `spot.high ≥ tp_spot` |
| PE | `spot.high ≥ sl_spot` | `spot.low ≤ tp_spot` |

**Equality counts** (non-strict `≤` / `≥`). If both SL and TP wicks fire in the same bar, **SL wins** (pessimistic tie-break — standard backtest convention since intra-bar order is unknown).

### Exit fill

| Trigger | Fill bar | Fill price |
|---|---|---|
| SL or TP wicks at bar M | Bar **M+1**'s open | Option open at locked strike |
| Force exit at 14:45 (no earlier SL/TP) | Bar **14:45** itself | Option **close** at 14:45 |

So all SL/TP exits have the same 1-min delay as entries. EOD is a same-bar fill at close.

### ⚠ Important realism nuance (already showing up in backtests)

Because the spot wick fires the exit but the fill is at the *next* bar's option open, a trade can be labeled `TP` and still post a *negative* INR P&L. Example from a live backtest: spot wicked the TP level at 13:31; at 13:32 the option opened below entry due to theta / vega / spread, so the "TP" trade lost ₹16. **This is correct engine behavior reflecting real-world slippage** — but the designer should be aware that `exit_reason` is the *spot* event, not the option's profitability.

---

## 8. Trading window & scan resume

| Event | Time | Behaviour |
|---|---|---|
| Scan start | 09:25 | **Both pair bars** must be at or after this time. Earliest candidate pair is (09:25, 09:26), evaluated at 09:26's close. |
| Entry deadline | 14:44 (inclusive) | Last bar at which a breakout can trigger entry. A breakout at 14:44 → entry fills at 14:45 open. |
| Pair expiry | 14:44 → 14:45 boundary | If a pair is still armed at 14:45 (past entry deadline), it silently expires. |
| Force exit | 14:45 | Any open position is force-exited at 14:45's option close, reason = `EOD`. |

**Edge case — 14:44 breakout.** A breakout at 14:44 fills at 14:45 option open, then immediately force-exits at 14:45 option close. The trade has effectively zero duration but is recorded.

### Multiple trades per day

Yes — only one trade is active at a time, but after a trade resolves (or a pair is filtered out, or premium budget skip), the engine resumes scanning. Empirically on busy days this can produce 20+ trades.

### Pair scan resume after resolution — STRICT NO-OVERLAP

The engine enforces that **the next pair_bar1 must be strictly AFTER the prior trade's exit_time** (or budget-skip/cancel time). Bars during the prior trade's life — including the wick-trigger bar and the fill bar — can NEVER participate in the next pair.

| Resolution event | Resolution bar | Earliest new pair_bar1 |
|---|---|---|
| Trade exit fill at bar M+1 (SL/TP wicked at M, fill at M+1) | M+1 | bar M+2 → first pair is (M+2, M+3), evaluated at M+3 |
| Premium-budget-skip at entry bar M+1 (Option C) | M+1 | bar M+2 |
| Entry data-gap cancel at bar M+1 | M+1 | bar M+2 |
| Both filters blocked at pair formation (bar N) | — (no trade attempted) | bar N+1 (rolling, normal) |
| 14:45 force exit | — | no more scanning this day |

So between every two consecutive trades on the same day, you'll always see a gap of at least one bar between `exit_time` of trade N and `pair_bar1_time` of trade N+1.

---

## 9. Trade record (CSV columns)

Each completed trade is recorded with all decision-relevant context:

```
date, instrument, expiry_date, option_type, strike, strike_offset,
pair_high, pair_low, pair_bar1_time, pair_bar2_time, range_size,
sl_spot, tp_spot,
entry_time, spot_at_entry, entry_price,
exit_time, spot_at_exit, exit_price, exit_reason,
pnl_points, pnl_inr, lot_size
```

- `pnl_points` = `exit_price − entry_price` (option premium points)
- `pnl_inr` = `pnl_points × NIFTY_lot_size (65) × lot_multiplier`
- `strike_offset` = signed offset from ATM (where ATM is computed from spot at the entry bar's open). 0 = ATM, +N = N strikes higher, −N = N strikes lower. For CE, OTM = positive; for PE, OTM = negative.
- `exit_reason` ∈ `{SL, TP, EOD}` (or in rare data-gap cases, a recorded trade with a synthetic exit using the trigger price — logged as a warning)

---

## 10. First-principles audit summary

The engine was reviewed for biases. **No look-ahead bias found.** Every decision uses only data known at decision time; all fills happen at the *next* bar's open (or current bar's close for EOD), naturally simulating realistic execution.

Specifically:
- Pair detection at bar N's close uses bars N-1 and N (both observed).
- TAIL filter at bar N's close uses RSI[N-1], RSI[N], and EMA[N] (causal indicators).
- Breakout at bar N's close uses bar N's spot close (observed).
- ATM strike chosen at bar N's close from spot[N].close.
- Strike walk uses option closes at bar N (observed).
- Entry fill at bar N+1's option open (cannot peek before iterating to N+1).
- SL/TP detection uses spot bar's high/low (intra-bar wick — observed at end of bar).
- Exit fill at next bar's option open. EOD at force-exit bar's option close.

### Other realism notes (deliberate, not bugs):
1. **Premium slippage** between trigger close and fill open is realistically modeled — the engine uses the actual next-bar open, which may differ.
2. **SL/TP tie-break** favors SL (pessimistic).
3. **Indicator continuity** across overnight gaps matches TradingView.
4. **Spot wick triggers, option fills** — "TP" trades can have negative INR P&L due to option-premium decay between trigger and fill.

### One half-strike edge case fixed during audit
Earlier the engine looked up options by the data feed's `strike_offset` column. At exact half-strike spots (e.g., spot = 22525.00) Python's banker's rounding could disagree with the data feed's rounding. Fixed: the engine now looks up options by **actual strike value** computed from its own ATM rule.

---

## 11. Configuration reference (`saved_strategies/traffic_light.json`)

```json
{
  "name": "traffic_light",
  "instrument": "NIFTY",
  "params": {
    "rsi_period": 14,
    "ema_period": 15,
    "rsi_overbought": 70,
    "rsi_oversold": 30,
    "sl_buffer": 0.0,
    "rr_ratio": 1.2,
    "premium_budget_inr": 10000,
    "min_otm_offset": 0,
    "max_otm_offset": 4
  },
  "timing": {
    "scan_start": "09:25",
    "entry_deadline": "14:44",
    "force_exit": "14:45"
  },
  "lot_size": 1,
  "backtest_start": "2025-01-01",
  "backtest_end": "2026-04-13"
}
```

All knobs are exposed in the Streamlit UI (🚦 Traffic Light tab).

---

## 12. Open items to confirm with the designer

1. **Premium budget with multiple lots.** Today `premium × 65 < ₹10,000` regardless of `lot_size` multiplier. Should it scale to `premium × 65 × num_lots < ₹10,000`?
2. **"TP" with negative INR.** Acceptable that exit_reason refers to the spot event, not option profitability? (Already showing up in backtests.)
3. **Indicator reset at 09:15.** Currently RSI/EMA carry over across day boundaries (TradingView default). Designer might want intraday reset; this is a one-flag change.
4. **Strike rounding rule.** Engine uses Python's banker's rounding (`round(spot/50)*50`). At exact half-strikes this rounds to even. If designer prefers "always round to nearest, halfway away from zero" — one-line change.
5. **SL buffer direction.** Engine WIDENS SL by `sl_buffer` (CE SL = pair_low − buffer; PE SL = pair_high + buffer). Default 0. Confirm direction is correct (buffer makes SL harder to hit, gives the trade more room).
6. **Force-exit price at 14:45.** Currently uses **option close** at 14:45. Alternative: fill at 14:46 open. Designer picked "close at 14:45" in the original interview.

---

## 13. Numbers from one real-data smoke (sanity only)

NIFTY 2026-04-01 → 2026-04-13 with defaults:

| Metric | Value |
|---|---|
| Total trades | 83 |
| Wins / Losses | 45 / 38 |
| Win rate | 54.2% |
| Total P&L | +₹2,025 |
| By reason | TP 42 / SL 36 / EOD 5 |
| Strike distribution | Mostly ATM (37); walked to ±1/±2/±3/±4 on expensive days |

**Not a recommendation.** Just confirming the engine runs cleanly end-to-end and produces plausible numbers.
