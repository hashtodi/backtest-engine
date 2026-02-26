# SuperTrend - Booming Bulls Strategy

An options buying strategy that uses two SuperTrend indicators and one SMA to identify bullish momentum and enter at a dynamic support level.

---

## The Idea (Plain English)

Imagine you're watching an option contract's 1-minute chart.

1. **Three filters confirm the trend is bullish (ALERT):**
   - Price is above the 13-period SMA (short-term trend is up).
   - Price is above the slower SuperTrend (4, 11) line (medium-term trend is up).
   - Price is above the faster SuperTrend (3, 10) line (price hasn't broken the tighter support yet).

2. **Once all three filters are green, you start watching the contract.**
   - You keep watching **as long as** the faster SuperTrend (3, 10) direction is **bullish**.
   - While watching, you wait for price to dip back and touch the SuperTrend (3, 10) line — that's your entry level.

3. **If price touches SuperTrend (3, 10) while it's still bullish → ENTRY.**
   - You're buying the pullback at a dynamic support. Entry price = the SuperTrend (3, 10) value at that moment.

4. **If SuperTrend (3, 10) flips bearish before price touches it → CANCEL.**
   - The observation is invalidated. The support is gone.
   - Go back to step 1. Wait for a fresh alert signal.

5. **Exit is simple:**
   - Stop loss: 5% below entry price.
   - Target profit: 7.5% above entry price.
   - If neither hits by 3:15 PM, the trade is closed (end-of-day exit).

---

## The 3 Indicators

All indicators are calculated on the **option contract's 1-minute chart** (not the underlying spot index).

| # | Indicator | Settings | Role |
|---|-----------|----------|------|
| 1 | SuperTrend | Factor = 4, ATR Period = 11 | **Trend filter** (slower). Confirms the overall bullish trend. |
| 2 | SMA | Period = 13 | **Trend filter**. Price must stay above this moving average. |
| 3 | SuperTrend | Factor = 3, ATR Period = 10 | **Entry level** (faster/tighter). Acts as dynamic support where we buy. |

### Why two SuperTrends?

- **SuperTrend (4, 11)** is slower — it flips less often and gives a broader trend direction. Think of it as confirming "are we in an uptrend?"
- **SuperTrend (3, 10)** is faster — it hugs the price more closely and serves as a natural pullback level. Think of it as "where should I buy the dip?"

When the slow one says "bullish" and price pulls back to the fast one, that's a high-probability entry.

---

## How It Works: Step by Step

This strategy has 3 distinct phases. Each phase has clear rules.

### Phase 1: Alert (Scan for Bullish Setup)

Check these three conditions on every 1-minute candle:

| Condition | Plain English |
|-----------|--------------|
| Close price > SMA (13) | The option price is above its 13-period moving average. Short-term trend is up. |
| Close price > SuperTrend (4, 11) line | The option price is above the slower SuperTrend. Medium-term trend is also up. |
| Close price > SuperTrend (3, 10) line | The option price is above the faster SuperTrend. Price hasn't broken the tighter support. |

**All three must be true at the same time.**

If YES → move to Phase 2. The market is bullish. Start watching.
If NO → keep scanning. Do nothing.

---

### Phase 2: Observation (Watch for Entry)

Now you're watching the contract. But you don't buy immediately. You wait for a pullback.

Two things are checked continuously:

**1. Is SuperTrend (3, 10) still bullish?**

SuperTrend has a "direction" output. When it's bullish, the SuperTrend line sits **below** the price and acts as a **support** (a floor the price bounces off).

- If YES (still bullish) → keep watching. Move to check #2.
- If NO (flipped bearish) → the support line is broken. **Cancel observation. Go back to Phase 1.** When SuperTrend flips bearish, the line jumps above price and becomes resistance — buying at resistance makes no sense.

**2. Does the price touch the SuperTrend (3, 10) value?**

This is the key moment. The SuperTrend (3, 10) line is rising as a support floor. You're waiting for price to dip back and touch it.

- If YES → **BUY at that exact price.** Move to Phase 3.
- If NO → keep watching. The pullback hasn't happened yet.

---

### Phase 3: Trade Active (Manage the Position)

You bought at the SuperTrend (3, 10) level. Now forget about all the indicators. Only 3 exits matter:

| Exit Rule | What Happens |
|-----------|-------------|
| **Stop Loss (5%)** | Price drops 5% below your entry → exit immediately. Cut your loss. |
| **Target Profit (7.5%)** | Price rises 7.5% above your entry → exit immediately. Book your profit. |
| **End of Day (3:15 PM)** | Neither SL nor TP hit by 3:15 PM → close the position. No overnight risk. |

After the trade exits (by any of the 3 rules), go back to Phase 1 and scan for a new alert.

---

## Why This Works (The Logic)

The strategy is built on a simple principle: **buy the pullback in a confirmed uptrend.**

- All three indicators (SMA, slow SuperTrend, fast SuperTrend) must agree the trend is bullish — not just a random spike. Triple confirmation.
- SuperTrend (3, 10) then gives you a precise entry point — you're not chasing the move, you're waiting for price to come to you.
- The direction check on SuperTrend (3, 10) protects you — if the faster trend breaks down before you enter, you walk away instead of catching a falling knife.

The risk-reward is 5% risk for 7.5% reward (1:1.5 ratio), and the end-of-day exit ensures you never hold overnight.

---

## Visual Flow

```
┌─────────────────────────────────────────────────┐
│         PHASE 1: SCAN FOR BULLISH ALERT         │
│                                                 │
│  Is close price above SMA (13)?       ──YES──┐  │
│  Is close price above SuperTrend (4,11)? YES─┤  │
│  Is close price above SuperTrend (3,10)? YES─┘  │
│           All 3 YES → ALERT!                    │
│                                                 │
│  If any NO → keep scanning every minute.        │
└────────────────────┬────────────────────────────┘
                     │ Alert fired
                     ▼
┌─────────────────────────────────────────────────┐
│       PHASE 2: WATCH FOR PULLBACK ENTRY         │
│       (only while SuperTrend 3,10 is BULLISH)   │
│                                                 │
│  ┌─ Is SuperTrend (3,10) still bullish?         │
│  │    YES → Does price touch ST (3,10) line?    │
│  │           YES → BUY at that price! (PHASE 3) │
│  │           NO  → Keep watching...              │
│  │                                              │
│  │    NO (flipped bearish — support is broken)  │
│  │       → CANCEL. Walk away.                   │
│  └──────→ GO BACK TO PHASE 1.                   │
└────────────────────┬────────────────────────────┘
                     │ Price touched the line
                     ▼
┌─────────────────────────────────────────────────┐
│         PHASE 3: TRADE IS ACTIVE                │
│                                                 │
│  Entry price = SuperTrend (3,10) value          │
│                                                 │
│  Indicators no longer matter. Only exits:       │
│                                                 │
│  ├ Stop Loss:     -5% from entry                │
│  ├ Target Profit: +7.5% from entry              │
│  └ End of Day:    3:15 PM forced exit           │
│                                                 │
│  After exit → GO BACK TO PHASE 1.              │
└─────────────────────────────────────────────────┘
```

---

## Settings Summary

| Parameter | Value |
|-----------|-------|
| Timeframe | 1-minute chart |
| Instruments | NIFTY and SENSEX options |
| Option type | ATM (at-the-money) CE and PE of nearest weekly expiry |
| Trading hours | 9:30 AM — 3:15 PM |
| Max trades per day | Unlimited |
| Direction | Buy options |

| Indicator | Type | Settings |
|-----------|------|----------|
| Trend Filter 1 | SuperTrend | Factor = 4, ATR Period = 11 |
| Trend Filter 2 | SMA | Period = 13 |
| Entry Level | SuperTrend | Factor = 3, ATR Period = 10 |

| Risk | Value |
|------|-------|
| Stop Loss | 5% |
| Target Profit | 7.5% |
| End of Day Exit | 3:15 PM |
| Risk : Reward | 1 : 1.5 |

---

## Strategy Config (JSON)

This is the machine-readable config that our system uses to run the strategy. Here's what each part means:

```json
{
  // --- What is this strategy called ---
  "name": "supertrend - booming bulls",
  "description": "Buy options when price is bullish (above SMA 13, SuperTrend 4/11, and SuperTrend 3/10). Entry at the SuperTrend 3/10 support level.",

  // --- The 3 indicators applied on the option contract's 1-min chart ---
  "indicators": [
    // Indicator 1: Slower SuperTrend — used to confirm medium-term bullish trend
    { "type": "SUPERTREND", "factor": 4, "atr_period": 11 },

    // Indicator 2: 13-period Simple Moving Average — short-term trend filter
    { "type": "SMA", "period": 13 },

    // Indicator 3: Faster SuperTrend — used as dynamic support / entry level
    { "type": "SUPERTREND", "factor": 3, "atr_period": 10 }
  ],

  // --- Alert conditions (ALL must be true to trigger the alert) ---
  "signal_conditions": [
    // Condition 1: Close price is above the SMA (13)
    { "check": "close price > SMA (13)" },

    // Condition 2: Close price is above the slower SuperTrend (4, 11) line
    { "check": "close price > SuperTrend (4, 11) value" },

    // Condition 3: Close price is above the faster SuperTrend (3, 10) line
    { "check": "close price > SuperTrend (3, 10) value" }
  ],
  // All conditions joined by AND — every single one must be true
  "signal_logic": "AND",

  // --- We are BUYING options (not selling) ---
  "direction": "buy",

  // --- Entry method ---
  // "indicator_level" = don't buy at market price.
  // Instead, wait for a pullback and buy when price touches the SuperTrend (3, 10) line.
  // This is like a dynamic limit order that moves with the indicator.
  // Extra guard: only enter while SuperTrend (3, 10) direction is bullish.
  // If it flips bearish before price touches it — cancel and go back to scanning.
  "entry": {
    "type": "indicator_level",
    "indicator": "SuperTrend (3, 10) value",
    "valid_while": "SuperTrend (3, 10) direction is bullish"
  },

  // --- Risk management ---
  "stop_loss": "5%",        // Exit if price drops 5% below entry
  "target_profit": "7.5%",  // Exit if price rises 7.5% above entry
  // If neither SL nor TP hits → close position at end of day (3:15 PM)

  // --- Trading window ---
  "trading_hours": "9:30 AM to 3:15 PM",
  "max_trades_per_day": "unlimited",

  // --- Which index options to trade ---
  "instruments": ["NIFTY", "SENSEX"],
  // Watches ATM (at-the-money) CE and PE strikes of the nearest weekly expiry
}
```

> **Note:** The JSON above is a simplified, human-readable version. The actual system config uses internal indicator names and codes, but the logic is exactly the same.
