# SuperTrend - Booming Bulls Strategy

A options buying strategy that uses two SuperTrend indicators and one SMA to identify bullish momentum and enter at a dynamic support level.

---

## The Idea (Plain English)

Imagine you're watching an option contract's 1-minute chart.

1. **Two filters confirm the trend is bullish (ALERT):**
   - Price is above the 13-period SMA (short-term trend is up).
   - Price is above the slower SuperTrend (4, 11) line (medium-term trend is up).

2. **Once both filters are green, you start watching the contract.**
   - You keep watching **as long as** the faster SuperTrend (3, 10) direction is **bullish** (direction = -1).
   - While watching, you wait for price to dip back and touch the ST(3,10) value — that's your entry level.

3. **If price touches ST(3,10) while it's still bullish → ENTRY.**
   - You're buying the pullback at a dynamic support. Entry price = the ST(3,10) value at that moment.

4. **If ST(3,10) flips bearish before price touches it → CANCEL.**
   - The observation is invalidated. The support is gone.
   - Go back to step 1. Wait for a fresh alert signal on the ATM strike.

5. **Exit is simple:**
   - Stop loss: 5% below entry price.
   - Target profit: 7.5% above entry price.
   - If neither hits by 3:15 PM, the trade is closed (end-of-day exit).

---

## The 3 Indicators

All indicators are calculated on the **option contract's price** (not the underlying spot index).

| # | Indicator | Settings | Role |
|---|-----------|----------|------|
| 1 | SuperTrend | Factor=4, ATR Period=11 | **Trend filter** (slower). Confirms bullish trend. |
| 2 | SMA | Period=13 | **Trend filter**. Price must stay above this. |
| 3 | SuperTrend | Factor=3, ATR Period=10 | **Entry level** (faster/tighter). Acts as dynamic support. |

### Why two SuperTrends?

- **SuperTrend (4, 11)** is slower — it flips less often and gives a broader trend direction.
- **SuperTrend (3, 10)** is faster — it hugs the price more closely and serves as a natural pullback level.

When the slow one says "bullish" and price pulls back to the fast one, that's a high-probability entry.

---

## Signal vs Entry (Important Distinction)

This strategy separates the **signal** (alert) from the **entry** (execution).

### Signal (Alert)
Both conditions must be true at the same time (AND logic):

| Condition | What it checks |
|-----------|---------------|
| `close > SMA(13)` | Price is above the moving average |
| `close > SuperTrend(4,11)` | Price is above the slower SuperTrend line |

When both are true, the system says: *"Market is bullish. Start watching for an entry."*

### Observation Window
After the alert fires, the system enters an **observation window**. This window stays open only while SuperTrend (3, 10) direction is **bullish** (direction = -1).

| What happens | Result |
|-------------|--------|
| ST(3,10) is bullish AND price touches ST(3,10) value | **ENTRY** — buy at the indicator level |
| ST(3,10) flips bearish (direction = +1) before price touches it | **CANCEL** — observation is invalidated, go back to watching for a new alert |

Why? When ST(3,10) is bullish, the line is below price and acts as **support** (a floor). If it flips bearish, the line jumps above price and becomes **resistance** (a ceiling). Buying at resistance makes no sense — so we cancel and wait for a fresh setup.

### Entry (Execution)
Once price touches the ST(3,10) value during a bullish observation window:

- In backtesting: checks if the 1-min candle's low-to-high range includes the indicator value.
- In forward testing: checks tick-by-tick if the price crosses through the indicator level.
- Entry price = the SuperTrend (3, 10) value at that moment (not the candle close).

### Once the trade is active:
- The original signal conditions (SMA and slow SuperTrend) **no longer matter**.
- The ST(3,10) direction check also **no longer matters**.
- They were just for the alert and observation. Now only SL, TP, and EOD exit apply.

---

## Risk Management

| Parameter | Value | Meaning |
|-----------|-------|---------|
| Stop Loss | 5% | Exit if price drops 5% below entry |
| Target Profit | 7.5% | Exit if price rises 7.5% above entry |
| EOD Exit | 3:15 PM | Close any open position at end of day |
| Max Trades/Day | Unlimited | No cap on daily trades |

---

## Trading Session

| Parameter | Value |
|-----------|-------|
| Start Time | 9:30 AM |
| End Time | 3:15 PM |
| Instruments | NIFTY, SENSEX |
| Direction | Buy (CE and PE options) |

The system observes **ATM (at-the-money) strikes** of the **nearest weekly expiry**.

- It watches 2 contracts at a time: 1 CE + 1 PE (independent).
- Once a trade is taken on one side (say CE), it stops looking for new CE signals until that trade exits.

---

## Visual Flow

```
┌─────────────────────────────────────────────────┐
│             STEP 1: WAIT FOR ALERT              │
│                                                 │
│  Price above SMA(13)?  ──YES──┐                 │
│                                ├──> ALERT!      │
│  Price above ST(4,11)? ──YES──┘                 │
│                                                 │
│  If NO → keep scanning every minute.            │
└────────────────────┬────────────────────────────┘
                     │ Alert fired
                     ▼
┌─────────────────────────────────────────────────┐
│         STEP 2: OBSERVATION WINDOW              │
│         (only while ST(3,10) is BULLISH)        │
│                                                 │
│  Every minute / tick, check:                    │
│                                                 │
│  ┌─ Is ST(3,10) still bullish (dir = -1)?       │
│  │    YES → Does price touch ST(3,10) value?    │
│  │           YES → GO TO STEP 3 (ENTRY)         │
│  │           NO  → Keep watching...              │
│  │                                              │
│  │    NO (dir flipped to +1, bearish)           │
│  │       → CANCEL observation.                  │
│  └──────→ GO BACK TO STEP 1.                    │
└────────────────────┬────────────────────────────┘
                     │ Price touched ST(3,10)
                     ▼
┌─────────────────────────────────────────────────┐
│             STEP 3: TRADE ACTIVE                │
│                                                 │
│  Entry price = ST(3,10) value                   │
│                                                 │
│  Signal & observation no longer matter.         │
│  Only these exits apply:                        │
│                                                 │
│  ├ SL:  -5% from entry                         │
│  ├ TP:  +7.5% from entry                       │
│  └ EOD: 3:15 PM forced exit                    │
│                                                 │
│  After exit → GO BACK TO STEP 1.               │
└─────────────────────────────────────────────────┘
```

---

## Backtest Configuration

| Parameter | Value |
|-----------|-------|
| Period | Jan 1, 2025 — Feb 25, 2026 |
| Initial Capital | ₹2,00,000 |
| Instruments | NIFTY + SENSEX |

---

## JSON Config Reference

The strategy is stored as a JSON file. Here's the structure:

```json
{
  "name": "supertrend - booming bulls",
  "description": "...",

  "indicators": [
    {"type": "SUPERTREND", "factor": 4, "atr_period": 11},
    {"type": "SMA", "period": 13},
    {"type": "SUPERTREND", "factor": 3, "atr_period": 10}
  ],

  "signal_conditions": [
    {"indicator": "opt_sma_13", "compare": "price_above"},
    {"indicator": "opt_st_4_11_value", "compare": "price_above"}
  ],
  "signal_logic": "AND",

  "direction": "buy",

  "entry": {
    "type": "indicator_level",
    "indicator": "opt_st_3_10_value",
    "valid_while": [
      {"indicator": "opt_st_3_10_direction", "compare": "equals", "value": -1}
    ]
  },

  "stop_loss_pct": 5.0,
  "target_pct": 7.5,
  "trading_start": "09:30",
  "trading_end": "15:15"
}
```

### Entry Types Available

| Type | Description |
|------|-------------|
| `direct` | Enter immediately at market price when signal fires |
| `staggered` | Enter in parts at different % levels below/above base price |
| `indicator_level` | Enter when price touches a specific indicator's value (dynamic limit order) |

This strategy uses **indicator_level** — the most precise entry method.

> **Note:** The `entry.valid_while` field (observation window guard — cancel if condition becomes false) is a planned feature — not yet implemented in the codebase. Currently only `indicator` and `type` are active.
