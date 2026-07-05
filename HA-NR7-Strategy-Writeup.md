# HA-NR7 Options Trading Strategy

## Overview

A two-stage intraday options buying strategy on NIFTY. Stage 1 identifies indecision in the market using a Heikin-Ashi neutral candle on the spot chart (the "alert"). Stage 2 waits for a narrow-range breakout (NR7) on the ITM option chart to confirm momentum direction and times the entry. The strategy supports position scaling (pyramiding), reversal on opposing signals, and dynamically adjusts risk based on days-to-expiry and the option's position relative to its moving averages.

---

## Instruments & Timeframe

- **Instrument:** NIFTY (configurable for other indices)
- **Chart Timeframe:** 3-minute candles for both spot and options
- **SL/TP Monitoring:** 1-minute bars for precise fill execution
- **Trading Hours:** 09:30 -- 14:55
  - Last new entry (including pyramids and reversals): 14:45
  - Force exit all positions: 14:55 (1-min bar close)

---

## Stage 1: Alert -- Heikin-Ashi Neutral Candle on Spot

A "neutral" Heikin-Ashi candle on the 3-minute spot chart signals market indecision with high volatility -- a potential breakout setup.

**Conditions (both must be true):**

| Condition | Threshold | Computed On |
|---|---|---|
| HA Body (indecision) | abs(HA_Close - HA_Open) < 2.5 points | Heikin-Ashi values |
| Regular Range (volatility) | High - Low > 20 points | Regular candle OHLC |

**Heikin-Ashi Formula (matches TradingView):**
- HA_Close = (Open + High + Low + Close) / 4
- HA_Open = previous bar: (prev_HA_Open + prev_HA_Close) / 2; first bar: (Open + Close) / 2
- HA_High = max(High, HA_Open, HA_Close)
- HA_Low = min(Low, HA_Open, HA_Close)

HA is computed **continuously** across days (no daily reset) to match TradingView's behavior.

**Alert Behavior:**
- A new alert **replaces** any active (non-entered) alert, restarting the NR7 scan window.
- Alerts are **ignored** while a position is open. A new alert is only accepted after all positions are closed.

---

## Stage 2: Entry -- NR7 Breakout on Option Chart

Once an alert fires, the strategy scans the ITM option charts for a Narrow Range 7 (NR7) pattern and enters on the breakout.

### Strike Selection (fixed at alert time)

- **ITM CE:** floor(spot_close / 100) x 100
- **ITM PE:** ceil(spot_close / 100) x 100
- Spot close is taken from the alert candle. Strikes are fixed for the entire trade lifecycle.
- Example: spot = 23,660 -> CE = 23,600, PE = 23,700

### NR7 Pattern Detection

NR7 = the candle with the smallest range (High - Low) in the last 7 candles (inclusive). Ties count.

Computed on **regular** option OHLC (not HA-transformed), independently per contract -- each (strike, option_type, expiry) has its own NR7 history. The NR7 candle's high is stored as the **breakout level** and persists until the next NR7 is detected on that contract.

### Breakout Entry Signal

The entry trigger is a **breakout above the NR7 candle's high** (matching LuxAlgo's NR4/NR7 with Breakouts indicator):

- **Condition:** Option close crosses above the persisted NR7 high
  - Current candle close > NR7 high
  - Previous candle close <= NR7 high (crossover, not just being above)
- **Applies to both CE and PE** -- we are buying the option, so upward price breakout = momentum confirmation

### Scan Window

- 5 consecutive 3-min candles starting from the alert candle (inclusive)
- Both CE and PE are scanned simultaneously at each candle
- **Both breakout simultaneously** -> skip (ambiguous signal)
- **One side breaks out** -> BUY that option at the open of the next 3-min candle
- **No breakout in 5 candles** -> alert expires, return to scanning for new alerts

### No-Lookahead Timing

A 3-min candle at time T (covers T to T+2:59) is known at T+3. Entry fills at T+3 open.

Example:
- 10:09 candle: NR7 detected on PE 23700 (high = 102.00, stored as breakout level)
- 10:12 candle: HA alert fires on spot. PE 23700 close = 103.05, crosses above 102.00
- 10:15: Entry at PE 23700 open price

---

## Position Sizing & Pyramiding

- **Fixed lot size:** 1 lot per entry
- **Maximum:** 3 lots total (via pyramiding)

### Pyramiding Rules

While a position is open, if a new NR7 breakout fires on the **same option type**:
- Add 1 lot at the next 3-min candle's open
- Consecutive breakouts count (no gap required)
- Same TP/SL **percentage** as the first entry (no re-evaluation)
- SL/TP **levels** recalculated on the new weighted average entry price:
  - avg_entry = sum(entry_prices) / num_lots
  - TP_level = avg_entry x (1 + TP%)
  - SL_level = avg_entry x (1 - SL%)
- All lots exit together

---

## Reversal

If an NR7 breakout fires on the **opposite option type** while a position is open:

1. **Close** all lots at the NR7 breakout candle's close price
2. **Enter** 1 lot on the opposite side at the next 3-min candle's open
3. Fresh position: 1 lot, can pyramid up to 3, TP/SL re-evaluated with DTE + EMA rules

### Reversal Limits

- **1st reversal:** Allowed. Close and enter opposite side.
- **2nd reversal trigger:** Close position, do NOT enter new side. Stop trading for the day.
- **Reversed position hits SL:** Stop trading for the day.

Regular SL (no reversal involved) has no penalty -- resume scanning for new alerts.

---

## TP/SL Framework

### Base TP/SL by DTE (Trading Days to Expiry)

DTE is counted in **trading days** (market-open days), not calendar days. Uses nearest weekly expiry.

| Trading DTE | Base TP | Base SL |
|---|---|---|
| 0 (expiry day) | 15% | 15% |
| 1 | 12.5% | 12.5% |
| 2 | 10% | 10% |
| 3 | 7.5% | 7.5% |
| 4+ | 5% | 7.5% |

Closer to expiry = wider TP/SL (options have more gamma, larger % moves expected).

### EMA Adjustment (applied at first entry only)

EMA(10) and EMA(21) are calculated on the option's 3-min close, per contract. At entry, the option's entry price is compared against the EMA values from the last closed candle:

| Position vs EMAs | TP Adjustment | SL Adjustment |
|---|---|---|
| **Above both** EMA(10) and EMA(21), base TP >= 7.5% | TP reduced to **5%** | SL fixed to **7.5%** |
| **Below both** EMA(10) and EMA(21), base TP <= 7.5% | TP increased to **10%** | SL capped at **10%** (min of base SL, 10%) |
| Otherwise | No change | No change |

**Rationale:**
- Above both EMAs = option is extended/expensive -> take a smaller, quicker profit with tighter SL
- Below both EMAs = option is compressed/cheap -> expect a larger move, allow more room

Pyramiding entries (2nd, 3rd lots) use the **same TP/SL percentages** locked at the first entry. No re-evaluation.

### Effective TP/SL Table After EMA Adjustment

| DTE | Above Both EMAs | Below Both EMAs | Between EMAs |
|---|---|---|---|
| 0 | TP=5%, SL=7.5% | TP=15%, SL=10% | TP=15%, SL=15% |
| 1 | TP=5%, SL=7.5% | TP=12.5%, SL=10% | TP=12.5%, SL=12.5% |
| 2 | TP=5%, SL=7.5% | TP=10%, SL=10% | TP=10%, SL=10% |
| 3 | TP=5%, SL=7.5% | TP=10%, SL=7.5% | TP=7.5%, SL=7.5% |
| 4+ | TP=5%, SL=7.5% | TP=10%, SL=7.5% | TP=5%, SL=7.5% |

---

## Exit Logic

### Priority Order

SL/TP are checked on every **1-minute bar** (including the entry bar). Indicator signals (reversal, pyramid) are checked at **3-minute boundaries** only.

1. **SL** (immediate, 1-min): option low <= SL level -> exit all lots at SL level
2. **TP** (immediate, 1-min): option high >= TP level -> exit all lots at TP level
3. **Same-bar conflict:** If both SL and TP are hit on the same 1-min bar, **SL wins** (conservative)
4. **Reversal** (3-min boundary): opposite NR7 breakout -> close at candle close, enter new side
5. **EOD** (14:55): force exit at 1-min bar close

SL/TP take priority over indicator signals. If SL/TP fills before a 3-min boundary, NR7 signals are never checked for that candle.

---

## Day-Stop Rules

Trading halts for the rest of the day if either occurs:

1. A **reversed position** hits SL (the reversal was wrong)
2. A **2nd reversal trigger** fires (market is choppy, no clear direction)

Regular SL (on a non-reversed position) does NOT stop trading.

---


## Summary of Key Parameters

| Parameter | Value |
|---|---|
| Timeframe | 3-min (signals), 1-min (SL/TP) |
| HA Body Threshold | < 2.5 points |
| HA Range Threshold | > 20 points |
| NR7 Lookback | 7 candles |
| Scan Window | 5 candles from alert |
| Strike Selection | ITM, rounded to 100 |
| Lot Size | 1 per entry (fixed) |
| Max Lots | 3 (via pyramiding) |
| Max Reversals | 1 per alert lifecycle |
| Trading Start | 09:30 |
| Last Entry | 14:45 |
| Force Exit | 14:55 |
| EMA Periods | 10, 21 (on option close) |
