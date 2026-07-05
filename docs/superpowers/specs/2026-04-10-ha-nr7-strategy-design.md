# HA-NR7 Strategy — Design Spec

**Date:** 2026-04-10
**Engine type:** Specialized (custom state machine, shared utilities)
**Files:** `engine/ha_nr7_backtest.py`, `ui/ha_nr7_backtest_runner.py`, `indicators/heikin_ashi.py`

---

## Overview

Buy ITM options based on a two-stage signal: a Heikin-Ashi neutral candle on the spot chart (alert), followed by an NR7 (Narrow Range 7) pattern on the option chart (entry). Supports pyramiding up to 3 lots on same-side NR7, reversal on opposite-side NR7, and DTE-based TP/SL with EMA-driven target adjustments.

---

## Timeframe

All signals and indicators operate on **3-minute candles** resampled from 1-minute data. SL/TP is checked on **1-minute bars** for precise fills.

**No-lookahead rule:** A 3-min candle at time T (covers T to T+2:59) is known at T+3. Entry is at the **open of the T+3 1-min bar**.

---

## Alert — Spot 3-min Heikin-Ashi

A neutral HA candle on the resampled spot chart triggers an alert.

**Conditions (both must be true):**
1. HA Body < 2.5 points: `abs(HA_Close - HA_Open) < 2.5`
2. Regular Range > 20 points: `regular_High - regular_Low > 20`

**HA Formulas (matching TradingView PineScript):**
```
HA_Close = (Open + High + Low + Close) / 4
HA_Open  = first candle ? (Open + Close) / 2 : (prev_HA_Open + prev_HA_Close) / 2
HA_High  = max(High, HA_Open, HA_Close)
HA_Low   = min(Low, HA_Open, HA_Close)
```

Body uses HA values (HA_Open, HA_Close). Range uses **regular** candle High and Low.

**Alert behavior:**
- New HA alert **replaces** any active (non-entered) alert — restarts the 5-candle NR7 window and re-determines ITM strikes.
- HA alerts are **ignored** while a position is open. New alerts only accepted once all positions are closed (back to IDLE).

---

## Entry — Option 3-min NR7

**NR7 definition (matching LuxAlgo PineScript):**
```
rng = high - low
lowest_7 = min(rng over last 7 candles including current)
NR7 = (rng == lowest_7)
```
Current candle + 6 previous = 7 total. **Ties count** (`==` not `<`). Calculated on **regular** option OHLC (not HA-transformed). Calculated **per contract** — each (strike, option_type, expiry) has independent NR7 history.

**Strike selection (fixed at alert time):**
- Spot price = spot close of the HA alert candle.
- ITM CE: `floor(spot / 100) * 100`
- ITM PE: `ceil(spot / 100) * 100`
- Example: spot = 23,904 → CE = 23,900 (4pts ITM), PE = 24,000 (96pts ITM).
- Rounded to 100 for NIFTY. Configurable rounding for other instruments.
- Strikes are fixed for the entire trade lifecycle (including pyramiding and reversal).

**NR7 scan window:**
- Starts from the **alert candle itself** (inclusive) — 5 consecutive 3-min candles.
- At each candle, check NR7 on both ITM CE and ITM PE option charts:
  - **Both NR7** on same candle → skip, continue scanning.
  - **One side NR7** → BUY that option at the **open of the next 3-min candle**.
  - **Neither NR7** → continue scanning.
- If no valid NR7 in 5 candles → alert expires → IDLE.

**Entry fill:** At the open price of the next 3-min candle (equivalently, the 1-min bar open at T+3 where T is the NR7 candle time).

**Timing example:**
```
10:21 candle (covers 10:21-10:23, known at 10:24):
  - HA alert fires on spot
  - NR7 found on CE option chart
  → BUY CE at 10:24 open
  → Compare entry price (10:24 open) vs EMA values from 10:21 candle
```

---

## EMA Target Adjustment

EMA(10) and EMA(21) calculated on the **option 3-min close**, **per contract** — each (strike, option_type, expiry) has independent EMA history.

At first entry, compare entry price against EMA values from the **last closed candle** (the candle that triggered entry):

| Condition | TP Adjustment |
|---|---|
| Entry price **above both** EMA10 and EMA21 AND base TP >= 7.5% | Reduce TP to **5%** |
| Entry price **below both** EMA10 and EMA21 AND base TP <= 7.5% | Increase TP to **10%** |
| Otherwise (between EMAs, or conditions don't match) | Keep base DTE TP |

**SL is never adjusted** — always uses the DTE-based value.

Pyramiding entries (2nd, 3rd lots) use the **same TP/SL %** from the first entry. No re-evaluation of EMA position.

---

## DTE-Based TP/SL

DTE = **trading days** to expiry (not calendar days). Weekly expiry, always nearest.

| Trading DTE | Base TP | Base SL |
|---|---|---|
| 4+ | 5% | 7.5% |
| 3 | 7.5% | 7.5% |
| 2 | 10% | 10% |
| 1 | 12.5% | 12.5% |
| 0 (expiry day) | 15% | 15% |

**After EMA adjustment, effective TP table:**

| Trading DTE | Above Both EMAs | Below Both EMAs | Between EMAs |
|---|---|---|---|
| 4+ | 5% (no change) | 10% | 5% |
| 3 | 5% | 10% | 7.5% |
| 2 | 5% | 10% (no change) | 10% |
| 1 | 5% | 12.5% (no change) | 12.5% |
| 0 | 5% | 15% (no change) | 15% |

---

## Pyramiding

While a position is open, subsequent NR7 on the **same option type** adds 1 lot.

- **Max 3 lots total** (1 per entry, fixed lot size).
- Consecutive NR7 candles count (no gap required between NR7s).
- Same TP/SL **percentage** as first entry (no re-evaluation).
- SL/TP **levels** recalculated on weighted average entry price after each addition:
  - `avg_entry = sum(entry_prices) / num_lots`
  - `TP_level = avg_entry * (1 + TP%)`
  - `SL_level = avg_entry * (1 - SL%)`
- All lots exit together (SL, TP, reversal, or EOD).
- No new HA alert needed — pyramiding is within the same alert lifecycle.
- Entry at next 3-min candle open (same no-lookahead rule).

---

## Reversal

If NR7 appears on the **opposite option type** while a position is open:

1. **Close** all lots at the NR7 candle's **close price** (the 3-min candle close).
2. **Enter** 1 lot on the opposite side at the **next 3-min candle open**.
3. The reversed position starts fresh: 1 lot, can pyramid up to 3.
4. TP/SL % determined fresh (DTE + EMA of the reversal trigger candle).
5. ITM strikes remain the same (fixed at original alert time).

**Day-stop rules (either triggers stop trading for the rest of the day):**
1. Reversed position hits **SL** → stop trading.
2. **2nd reversal trigger** — already reversed once, opposite NR7 appears again → close position, do NOT enter new side, stop trading.

Regular SL (no reversal involved) has **no penalty** — return to IDLE and wait for next HA alert.

---

## Exit Logic

**On every 1-min bar (while position is open, including the entry bar):**
1. **SL check** (immediate): option_low <= SL_level → exit all lots at SL_level.
2. **TP check** (immediate): option_high >= TP_level → exit all lots at TP_level.
3. If **both SL and TP hit on same 1-min bar** → **SL wins** (conservative).

SL/TP CAN trigger on the entry 1-min bar itself (entry is at open, high/low happen after).

**On every 3-min boundary (after no-lookahead shift):**
4. **Opposite NR7** → reversal (close at candle close, enter new side at next open).
5. **Same side NR7** → pyramid (add lot at next open, if lots < 3 and before 14:45).

**Priority:** SL/TP (1-min, immediate) > NR7 signals (3-min boundary). If SL/TP fills on a 1-min bar before the 3-min boundary is reached, NR7 is never checked.

**EOD:** Force exit at **close of the 14:55 1-min bar**.

---

## Session Timing

| Parameter | Value |
|---|---|
| Trading start | 09:30 |
| Last entry (any type: new, pyramid, reversal) | 14:45 |
| Force exit (EOD) | 14:55 (1-min close) |

- After 14:45: no new entries, no pyramiding, no reversals. Only SL/TP/EOD exits.
- Unlimited alerts per day (unless day-stopped by reversal SL or 2nd reversal trigger).

**Last valid signal candle:** The candle whose entry fill time <= 14:45. E.g., 14:42 candle (known at 14:45) → entry at 14:45 open = allowed. 14:45 candle (known at 14:48) → entry at 14:48 = not allowed.

---

## State Machine

```
IDLE ──[HA alert]──► ALERT_ACTIVE ──[NR7 found]──► POSITION_OPEN
  ▲                       │    ▲                      │  │  │
  │                       │    │ [new HA alert,       │  │  │
  │                       │    │  restarts window]    │  │  │
  │               [5 candles,  │                      │  │  │
  │                no NR7]─────┘                      │  │  │
  │                                                   │  │  │
  │◄──────[TP / regular SL / EOD]─────────────────────┘  │  │
  │                                                      │  │
  │    ┌──[opposite NR7, 1st reversal: close + enter]────┘  │
  │    │  (stays POSITION_OPEN, reversal_count = 1)         │
  │    │                                                    │
  │◄───┤──[reversed position TP / EOD]                      │
  │    │                                                    │
  │    DAY_STOPPED ◄──[reversed position SL]────────────────┤
  │         ▲                                               │
  │         └──────[2nd reversal trigger: close, no entry]──┘
  │
  [next day] → IDLE
```

**States:**
- **IDLE**: Scanning for HA alerts on 3-min spot candles.
- **ALERT_ACTIVE**: HA alert fired, scanning for NR7 on option candles (5-candle window). New HA alert restarts window.
- **POSITION_OPEN**: Holding lots. Checking SL/TP on 1-min, NR7 on 3-min for pyramid/reversal.
- **DAY_STOPPED**: No more trading. Reached via reversal position SL or 2nd reversal trigger.

---

## Position Tracking

**Per-lot tracking:**

| Field | Description |
|---|---|
| lot_number | 1, 2, or 3 |
| entry_price | Option open price at entry candle |
| entry_time | Datetime of entry |

**Position-level tracking:**

| Field | Description |
|---|---|
| option_type | CE or PE |
| strike | ITM strike (fixed at alert time) |
| num_lots | Current lot count (1-3) |
| avg_entry | Simple average of all entry prices |
| tp_pct / sl_pct | Locked at first entry (DTE + EMA adjusted TP) |
| tp_level / sl_level | Recalculated on avg_entry after each lot added |
| is_reversal | True if this position was entered via reversal |
| reversal_count | 0 or 1 (2nd reversal trigger = day stop, no new position) |

---

## Trade Dataclass

Each completed trade (entry to exit) is one record:

| Field | Type | Description |
|---|---|---|
| entry_date | date | Trading date |
| alert_candle_time | datetime | Time of the HA alert candle |
| entry_time | datetime | Time of first lot entry |
| exit_time | datetime | Time of exit |
| option_type | str | CE or PE |
| strike | int | ITM strike traded |
| entry_prices | list[float] | Entry price per lot |
| avg_entry | float | Simple average entry |
| num_lots | int | Number of lots at exit (1-3) |
| exit_price | float | Price at exit |
| exit_reason | str | TP / SL / EOD / REVERSAL / REVERSAL_STOP |
| tp_pct | float | TP % used |
| sl_pct | float | SL % used |
| dte | int | Trading DTE on entry date |
| ema_adjusted | bool | Whether EMA adjustment was applied to TP |
| is_reversal | bool | Whether this trade was a reversal entry |
| pnl_points | float | exit_price - avg_entry |
| pnl_inr | float | pnl_points * num_lots * lot_size |

---

## Data Pipeline

1. **Spot 1-min parquet** → resample to 3-min OHLCV → compute HA candles (keep both HA and regular OHLC).
2. **Option 1-min parquet** → filter ITM CE and PE contracts (by strike, option_type, expiry) → resample to 3-min per contract.
3. **Per option contract (3-min):**
   - NR7 flag: `range == min(range over last 7 candles including current)`.
   - EMA(10) and EMA(21) on close — per contract, independent history.
4. **Forward-fill to 1-min timeline** with +3 min shift (no-lookahead) for 3-min indicator values.
5. **1-min option OHLC** retained for SL/TP checking.
6. **Expiry calendar:** Compute trading DTE for each date using existing `expiry_calendar.py`.

**Warmup considerations:**
- NR7 needs 7 candles → valid from 7th candle of the day (~9:33 at earliest).
- EMA(21) needs ~21 candles for stability (~10:18). Early EMAs are unreliable but functional (pandas ewm computes from first value).
- HA needs 1 previous candle → valid from 2nd candle (9:18).

---

## File Structure

| File | Purpose |
|---|---|
| `engine/ha_nr7_backtest.py` | Engine class, state machine, trade logic, day loop |
| `ui/ha_nr7_backtest_runner.py` | Streamlit UI: parameters, run button, results display |
| `indicators/heikin_ashi.py` | HA candle transformer (new indicator) |
| `app.py` | Register new tab |

**Reused:** `data_loader.py`, `expiry_calendar.py`, `reporter.py`, `detailed_logger.py`, `indicators/ema.py`, `config.py`.

---

## UI Parameters

| Parameter | Default | Configurable |
|---|---|---|
| Instrument | NIFTY | Yes |
| Strike rounding | 100 | Yes |
| HA body threshold | 2.5 | Yes |
| HA range threshold | 20 | Yes |
| NR7 lookback | 7 | Yes |
| NR7 scan window | 5 candles | Yes |
| Max pyramid lots | 3 | No (fixed) |
| Lot size per entry | 1 | No (fixed) |
| DTE TP/SL table | As specified | Yes (editable) |
| EMA periods | 10, 21 | Yes |
| EMA TP thresholds | 7.5%, 5%, 10% | Yes |
| Trading start | 09:30 | Yes |
| Last entry | 14:45 | Yes |
| Force exit | 14:55 | Yes |
| Backtest date range | — | Yes |

---

## Processing Order (per 1-min bar)

While in POSITION_OPEN:
1. Check if day-stopped → skip all.
2. **SL check**: option_low <= SL_level → exit at SL_level. If reversal position → day stop.
3. **TP check**: option_high >= TP_level → exit at TP_level → IDLE.

At 3-min boundaries (candle known), while in POSITION_OPEN and before 14:45:
4. **Opposite NR7**: If reversal_count == 0 → close at candle close, enter opposite at next open (reversal_count = 1). If reversal_count == 1 → close at candle close, don't enter, day stop.
5. **Same side NR7**: Add lot at next open (if num_lots < 3).

At 3-min boundaries, while ALERT_ACTIVE:
6. **NR7 scan**: Check CE/PE for NR7. Both → skip. One → entry. Decrement countdown.
7. **New HA alert**: Restart window, re-determine strikes.

At 3-min boundaries, while IDLE:
8. **HA alert check**: Neutral HA candle → ALERT_ACTIVE.

At 14:55:
9. **EOD**: Force exit at 1-min close → IDLE (or IDLE if no position).

---

## Worked Example

**Date: Wednesday (DTE-1), Spot ~23,450. ITM CE = 23,400. ITM PE = 23,500.**

| Candle | Covers | Known At | Event |
|---|---|---|---|
| 9:30 | 9:30-9:32 | 9:33 | HA alert fires (body=1.8, range=25). NR7 on 9:30 option candle: CE: No, PE: No |
| 9:33 | 9:33-9:35 | 9:36 | NR7: CE: **Yes**, PE: No → entry signal |
| — | — | 9:36 open | **BUY 1 lot 23400 CE at ₹180**. EMA10(9:33 candle)=175, EMA21=172. Entry > both → TP=5%, SL=12.5%. TP=189, SL=157.5 |
| — | — | 10:15 (1m) | CE high=190 >= 189 → **TP hit, exit at 189**. PnL: (189-180)×65 = +₹585. → IDLE |

**Later same day, new alert. Spot ~23,520. ITM CE = 23,500. ITM PE = 23,600.**

| Candle | Covers | Known At | Event |
|---|---|---|---|
| 10:30 | 10:30-10:32 | 10:33 | HA alert fires. NR7: CE: No, PE: **Yes** → entry signal |
| — | — | 10:33 open | **BUY 1 lot 23600 PE at ₹150**. EMA10=155, EMA21=158. Entry < both, but TP 12.5% > 7.5% → no adj. TP=12.5%, SL=12.5%. TP=168.75, SL=131.25 |
| 10:33 | 10:33-10:35 | 10:36 | NR7: PE: **Yes** (same side) → **pyramid** |
| — | — | 10:36 open | **Add lot 2 PE at ₹148**. Avg=149. TP=149×1.125=167.63, SL=149×0.875=130.38 |
| 10:36 | 10:36-10:38 | 10:39 | NR7: CE: No, PE: No |
| 10:39 | 10:39-10:41 | 10:42 | NR7: CE: **Yes** (opposite) → **REVERSAL** (reversal_count=1) |
| — | — | 10:41 close | Close 2 lots PE at 10:39 candle close = ₹145. PnL: (145-149)×2×65 = -₹520 |
| — | — | 10:42 open | **Enter 1 lot 23500 CE at ₹200**. EMA10(10:39)=195, EMA21=190. Entry > both → TP=5%. TP=210, SL=175 |
| — | — | 11:05 (1m) | CE low=174 <= 175 → **SL hit at 175**. PnL: (175-200)×65 = -₹1,625. Reversed position SL → **DAY_STOPPED** |
