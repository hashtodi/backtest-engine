# RSI EMA Cross Strategy — Manual Trading Guide (NIFTY)

---

## What this strategy does (in simple words)

You watch the **1-minute chart of the ATM NIFTY option contract** (not the NIFTY spot chart).

When the option's momentum picks up (RSI > 50) **AND** the short-term trend crosses above the long-term trend (EMA 9 crosses above EMA 20), you prepare to buy that option.

You don't buy immediately. You wait for a small dip and enter in **3 parts**.

---

## Setup (do this once at the start of the day)

1. Find the current NIFTY spot price. Round it to the nearest 50 to get the **ATM strike**.
   - Example: NIFTY at 25,623 → ATM strike = **25,600**

2. Open **two** TradingView charts, both on the **1-minute** timeframe:
   - **Chart 1:** NIFTY 25600 CE (nearest weekly expiry)
   - **Chart 2:** NIFTY 25600 PE (nearest weekly expiry)

3. Add these indicators to **both** charts:
   - RSI with period **14**
   - EMA with period **9**
   - EMA with period **20**

4. **Important:** These indicators must be on the **option price chart**, not the NIFTY spot chart.

5. If NIFTY moves enough to change the ATM strike (e.g., 25600 → 25650), switch to the new ATM strike contracts and start watching fresh charts. The old indicators no longer apply.

---

## Signal — When to get ready to buy

**Both** of these must be true at the **same time** on the 1-minute candle:

| Condition | What to look for |
|-----------|-----------------|
| RSI 14 is above 50 | RSI value is currently above 50 (not crossing, just above) |
| EMA 9 crosses above EMA 20 | Previous candle: EMA 9 was below EMA 20. Current candle: EMA 9 is now above EMA 20 |

- CE and PE are **independent**. A signal on CE does not affect PE, and vice versa.
- You can have both active at the same time.

When the signal fires, note down the **current option price**. This is your **base price**.

> Example: CE signal fires. CE option price is ₹634.90. That's your base price.

---

## Entry — Staggered in 3 parts

You don't buy all at once. Split your capital into 3 equal parts and buy at 3 lower levels:

| Level | Price to buy at | Capital to use |
|-------|----------------|----------------|
| L1 | Base price × 0.95 (−5%) | 33% |
| L2 | Base price × 0.90 (−10%) | 33% |
| L3 | Base price × 0.85 (−15%) | 34% |

> **Example** (base price = ₹634.90):
>
> - L1 = 634.90 × 0.95 = **₹603.15** → buy with 33% capital
> - L2 = 634.90 × 0.90 = **₹571.41** → buy with 33% capital
> - L3 = 634.90 × 0.85 = **₹539.66** → buy with 34% capital

### Two ways to enter

- **Option A (limit orders):** Place limit buy orders at all 3 prices right after the signal. They fill automatically if price drops.
- **Option B (manual):** Watch the price and buy manually when it reaches each level.

Not all levels may fill. If price only drops to L1 and bounces, you'll only be in with 33% capital. That's fine.

---

## Exit — When to sell

Calculate your **average entry price** from whichever levels filled.

| Exit reason | When to exit |
|-------------|-------------|
| **Take Profit (TP)** | Option price rises **10% above** your average entry price |
| **Stop Loss (SL)** | Option price drops **15% below** your average entry price |
| **End of Day** | **3:30 PM IST** — close everything, no overnight holding |

> **Example:** You entered at L1 (₹603.15) and L2 (₹571.41). Average entry = ₹587.28.
>
> - Take profit at: 587.28 × 1.10 = **₹646.01**
> - Stop loss at: 587.28 × 0.85 = **₹499.19**

---

## Important rules

1. **Trading hours:** Only trade between **9:30 AM** and **3:30 PM** IST.

2. **One trade per type at a time.** Only one CE trade and one PE trade can be active. If a CE trade is already open, ignore new CE signals until it exits.

3. **Indicators are on the option contract chart.** Not the NIFTY index chart. This is the most common mistake.

4. **ATM strike changes:** If NIFTY spot moves and the ATM strike changes, indicators on the new ATM contract start from scratch. Any already-open trade stays on its **original strike** until exit.

5. **Weekly expiry reset:** After weekly expiry, the contract changes. Start fresh with the new nearest weekly expiry contract.

6. **Entry levels are BELOW the signal price.** You're buying a dip after the signal. If the option price keeps going up without dipping, you don't enter. That signal is considered missed.

---

## Quick checklist (print this)

- [ ] Open ATM CE and ATM PE 1-min charts on TradingView
- [ ] Add RSI(14), EMA(9), EMA(20) to both charts
- [ ] Watch for: **RSI > 50** AND **EMA 9 crosses above EMA 20**
- [ ] Signal fires → note the **base price**
- [ ] Calculate L1 (−5%), L2 (−10%), L3 (−15%)
- [ ] Place limit orders or watch manually
- [ ] Set TP at **+10%** from average entry
- [ ] Set SL at **−15%** from average entry
- [ ] Close everything by **3:30 PM**

---

## Strategy summary

| Parameter | Value |
|-----------|-------|
| Instrument | NIFTY options (ATM strike, nearest weekly expiry) |
| Timeframe | 1-minute |
| Indicators | RSI(14), EMA(9), EMA(20) — all on option price |
| Signal | RSI > 50 AND EMA 9 crosses above EMA 20 |
| Direction | Buy options |
| Entry | Staggered: −5%, −10%, −15% from base |
| Capital split | 33% / 33% / 34% |
| Stop Loss | 15% below average entry |
| Take Profit | 10% above average entry |
| Hours | 9:30 AM – 3:30 PM IST |
| Max trades | Unlimited (but one CE + one PE at a time) |
