# RSI Calculation Guide for Options

Complete reference for implementing RSI on options data.
Designed to be language-agnostic — use this to reimplement in any language.

---

## 1. Overview

RSI (Relative Strength Index) measures momentum by comparing average gains vs average losses.

- **Range:** 0 to 100
- **Above 70:** overbought
- **Below 30:** oversold
- **Smoothing method:** Wilder's EMA (matches TradingView and most charting platforms)

---

## 2. Input Data

RSI is calculated on **option close prices** (1-minute candles).

Each bar in the data has these fields:

| Field         | Example                    | Description                        |
|---------------|----------------------------|------------------------------------|
| `datetime`    | 2025-02-19 10:15:00+05:30 | Timestamp of the 1-min candle      |
| `strike`      | 25550                      | Strike price                       |
| `option_type` | PE                         | CE (Call) or PE (Put)              |
| `expiry_type` | WEEK                       | Weekly or monthly                  |
| `expiry_code` | 1                          | 1 = nearest weekly expiry          |
| `close`       | 142.50                     | Option close price for this candle |
| `spot`        | 25532.10                   | Underlying/spot price              |

---

## 3. What is a "Contract"

A contract is a unique combination of 4 fields:

```
contract = (strike, option_type, expiry_type, expiry_code)
```

**Example:** `(25550, PE, WEEK, 1)` = "25550 PE nearest weekly expiry (e.g. Feb 24th)"

Each contract has its own independent price history and its own independent RSI.

---

## 4. RSI is Calculated Per Contract, Across All Available Days

### Key rules

1. **Each contract gets its own RSI.** Two different strikes (e.g. 25550 PE and 25600 PE) never share RSI data.
2. **RSI does NOT reset daily.** All bars for a contract across multiple trading days are treated as one continuous series.
3. **RSI resets only when a new expiry starts.** A new weekly contract = a new group = RSI starts from scratch.

### Example timeline

```
Contract: 25550 PE, WEEK, expiry_code=1 (expires Feb 24th)
Listed since Feb 18th. Has ~375 bars per trading day.

Day        Time     Close    RSI input
────────   ─────    ─────    ──────────────────────────────────
Feb 18     09:15    120.00   Bar 1   → RSI = NaN (warmup)
Feb 18     09:16    122.00   Bar 2   → RSI = NaN
...
Feb 18     15:29    135.00   Bar 375 → RSI = 62.3
────────   ─────    ─────    ──────────────────────────────────
Feb 19     09:15    138.00   Bar 376 → RSI = 63.1  (continues!)
Feb 19     09:16    136.00   Bar 377 → RSI = 61.8
...
Feb 24     15:29    45.00    Bar 1875 → RSI = 22.1 (expiry day)
```

The RSI at bar 376 (Feb 19, 09:15) uses the avg_gain and avg_loss
carried over from bar 375 (Feb 18, 15:29). There is no daily reset.

---

## 5. How ATM Strikes and RSI Interact

The ATM (At The Money) strike changes as the spot price moves.
When ATM changes, the system switches to the **new contract's own RSI** —
it does NOT carry over the old contract's RSI values.

### Example

```
Time     Spot      ATM Strike   RSI used
─────    ────────  ──────────   ─────────────────────────────────────
10:14    25532     25550 PE     RSI of 25550 PE (built from ALL its bars
                                since Feb 18th, not just today)

10:15    25578     25600 PE     ATM changed! Now uses RSI of 25600 PE.
                                25600 PE has its OWN bars since Feb 18th.
                                Its RSI is calculated on ITS OWN history.
                                No data from 25550 PE is used.

10:16    25581     25600 PE     Still using 25600 PE's RSI.
```

**Why:** Different strikes trade at different price levels. Mixing data from
25550 PE and 25600 PE into one RSI series would create artificial jumps
every time the ATM shifts. Keeping each contract's RSI separate ensures
the momentum measurement reflects that specific contract's price action.

---

## 6. RSI Algorithm (Wilder's Smoothing)

### Input

- `closes[]` — array of close prices, sorted chronologically
- `period` — RSI period (default: 14)

### Step 1: Price changes

```
delta[0] = NaN                              // no previous bar
delta[i] = closes[i] - closes[i-1]          // for i = 1 to n-1
```

### Step 2: Separate gains and losses

```
gain[i] = max(delta[i], 0)                  // positive change only
loss[i] = max(-delta[i], 0)                 // absolute value of negative change
```

### Step 3: Seed averages (simple mean of first `period` changes)

```
avg_gain = mean(gain[1], gain[2], ..., gain[period])
avg_loss = mean(loss[1], loss[2], ..., loss[period])
```

Compute RSI at index `period` (the first valid RSI value):

```
if avg_loss == 0:
    rsi[period] = 100.0
else:
    RS = avg_gain / avg_loss
    rsi[period] = 100 - (100 / (1 + RS))
```

All indices before `period` are NaN (not enough data).

### Step 4: Wilder's recursive smoothing (remaining bars)

For each bar `i` from `period + 1` to `n - 1`:

```
avg_gain = (avg_gain * (period - 1) + gain[i]) / period
avg_loss = (avg_loss * (period - 1) + loss[i]) / period

if avg_loss == 0:
    rsi[i] = 100.0
else:
    RS = avg_gain / avg_loss
    rsi[i] = 100 - (100 / (1 + RS))
```

This is equivalent to an exponential moving average with alpha = 1/period.

### Output

Array of RSI values (0–100). First `period` entries are NaN.

---

## 7. Language-Agnostic Pseudocode

```
function calculate_rsi(closes[], period):
    n = length(closes)
    rsi = array of NaN, size n

    // Need at least period+1 bars to compute one RSI value
    if n < period + 1:
        return rsi

    // 1. Compute deltas
    delta = array of size n
    delta[0] = NaN
    for i = 1 to n-1:
        delta[i] = closes[i] - closes[i-1]

    // 2. Separate gains and losses
    gain = array of size n
    loss = array of size n
    for i = 1 to n-1:
        gain[i] = max(delta[i], 0)
        loss[i] = max(-delta[i], 0)

    // 3. Seed: simple mean of first `period` changes
    avg_gain = sum(gain[1..period]) / period
    avg_loss = sum(loss[1..period]) / period

    // 4. First RSI value at index = period
    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        RS = avg_gain / avg_loss
        rsi[period] = 100 - (100 / (1 + RS))

    // 5. Wilder's smoothing for all remaining bars
    for i = period+1 to n-1:
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period

        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            RS = avg_gain / avg_loss
            rsi[i] = 100 - (100 / (1 + RS))

    return rsi
```

---

## 8. Full Flow Pseudocode (RSI on Options)

```
// ─── STEP 1: Group all bars by contract ───
// A contract = (strike, option_type, expiry_type, expiry_code)
// Include ALL bars across ALL days (no daily reset)

contracts = group_all_bars_by(strike, option_type, expiry_type, expiry_code)

// Example groups:
//   (25550, PE, WEEK, 1) → bars from Feb 18 09:15 to Feb 24 15:29
//   (25600, PE, WEEK, 1) → bars from Feb 18 09:15 to Feb 24 15:29
//   (25550, CE, WEEK, 1) → bars from Feb 18 09:15 to Feb 24 15:29
//   ... one group per unique contract


// ─── STEP 2: Calculate RSI for each contract independently ───

for each contract in contracts:
    sorted_bars = contract.bars.sort_by(datetime)
    // This includes ALL days. e.g. ~1875 bars for 5 trading days.
    contract.rsi = calculate_rsi(sorted_bars.close_prices, period=14)


// ─── STEP 3: At signal time, look up current ATM contract's RSI ───

for each minute in trading_session:
    spot_price = get_spot_price(this_minute)
    atm_strike = round_to_nearest_strike(spot_price)

    // Look up the specific contract
    atm_contract = lookup(atm_strike, option_type, "WEEK", 1)

    // Use THAT contract's pre-computed RSI
    current_rsi = atm_contract.rsi[this_minute]

    // Evaluate signal conditions using current_rsi
    if current_rsi crosses above 70:
        trigger_sell_signal()
```

---

## 9. Quick Reference

| Detail                 | Value                                                      |
|------------------------|------------------------------------------------------------|
| Smoothing method       | Wilder's EMA (alpha = 1/period)                            |
| Seed                   | Simple mean (SMA) of first `period` deltas                 |
| Subsequent bars        | `avg = (prev_avg × (period-1) + current_value) / period`  |
| Edge case              | If avg_loss = 0 → RSI = 100 (all gains, no losses)        |
| NaN count              | First `period` values are NaN (indices 0 to period-1)      |
| First valid RSI        | At index `period` (the period+1th bar)                     |
| Input                  | Close prices of that specific contract, sorted by datetime |
| Per-contract           | Each (strike, type, expiry_type, expiry_code) is separate  |
| Daily reset?           | **NO** — RSI continues across days for the same contract   |
| Resets on new expiry?  | **YES** — new weekly contract = new group = fresh RSI      |
| ATM changes?           | Switch to the new strike's own RSI. No data mixing.        |

---

## 10. Worked Numeric Example

Given: `period = 3`, close prices = `[100, 102, 101, 104, 103, 106]`

```
Index   Close   Delta    Gain    Loss
0       100     NaN      -       -
1       102     +2       2       0
2       101     -1       0       1
3       104     +3       3       0
4       103     -1       0       1
5       106     +3       3       0

Seed (index 3, period=3):
  avg_gain = mean(2, 0, 3) = 5/3 = 1.6667
  avg_loss = mean(0, 1, 0) = 1/3 = 0.3333
  RS = 1.6667 / 0.3333 = 5.0
  rsi[3] = 100 - (100 / (1 + 5.0)) = 100 - 16.667 = 83.33

Index 4:
  avg_gain = (1.6667 * 2 + 0) / 3 = 3.3333 / 3 = 1.1111
  avg_loss = (0.3333 * 2 + 1) / 3 = 1.6667 / 3 = 0.5556
  RS = 1.1111 / 0.5556 = 2.0
  rsi[4] = 100 - (100 / (1 + 2.0)) = 100 - 33.333 = 66.67

Index 5:
  avg_gain = (1.1111 * 2 + 3) / 3 = 5.2222 / 3 = 1.7407
  avg_loss = (0.5556 * 2 + 0) / 3 = 1.1111 / 3 = 0.3704
  RS = 1.7407 / 0.3704 = 4.7
  rsi[5] = 100 - (100 / (1 + 4.7)) = 100 - 17.544 = 82.46

Final RSI = [NaN, NaN, NaN, 83.33, 66.67, 82.46]
```

Use this example to validate your implementation in any language.

---

## 11. Notes for Live Market Implementation

For calculating any indicator (RSI, EMA, etc.) on options in the live market:

**Warmup ±20 strikes around ATM for the nearest weekly contract.**

At startup, before the trading session begins, fetch all available historical
1-minute bars for ±20 strikes above and below the current ATM strike, for the
nearest weekly expiry. This means:

- If ATM = 25550 and step = 50 → warm up strikes from 24550 to 26550 (CE + PE)
- If ATM = 25500 and step = 100 → warm up strikes from 23500 to 27500 (CE + PE)

Fetch all available data for each contract (multi-day, from when the contract
started trading until now). This ensures that even if ATM shifts during the day,
the new contract already has full indicator history ready.

During the session, append each new live bar and recalculate indicators from
the full series.