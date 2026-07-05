# BB Reversal PE Buy Strategy

## Overview

An intraday options buying strategy on NIFTY that profits when the market overextends upward and reverses down. When spot price breaks above the Bollinger Band upper band, the strategy waits for confirmation of weakness and then buys an ATM Put. Stop-loss and target are placed on the **underlying spot price** (not on the option price) using a fixed 1:2 risk-reward framework.

---

## Instruments & Timeframe

- **Instrument:** NIFTY
- **Chart Timeframe:** 1-minute candles for both spot and options
- **Signal Basis:** Spot price with 20-period, 2-std-dev Bollinger Bands
- **Trade Basis:** ATM Put option, nearest weekly expiry
- **Trading Hours:** 09:18 – 15:19 (new entries), force exit at 15:20

---

## Market Thesis

When spot breaks above the Bollinger upper band, it is statistically overextended. A subsequent bearish candle followed by a break below that candle's low signals that the breakout has failed and mean reversion is likely. We buy a Put to profit from the expected pullback.

---

## Signal Logic — 3-Stage State Machine

The strategy progresses through three sequential states on the spot chart:

| Stage | What Must Happen | Next State |
|---|---|---|
| **IDLE** | Spot candle closes **above** BB upper band (breakout) | WATCHING |
| **WATCHING** | A **red candle** appears (close < open) | RED_FOUND |
| **RED_FOUND** | A candle **closes below** the red candle's low | **SIGNAL FIRES** |

Throughout every stage after the breakout, the strategy tracks the **highest high** reached by spot. This peak becomes the stop-loss reference at entry.

### Setup Invalidation

If, during WATCHING or RED_FOUND, a new candle closes **above** BB upper again, the setup partially resets:
- Return to WATCHING
- Forget the red candle's low
- Continue tracking the highest high (it is preserved across re-breakouts)

This ensures the stop-loss always reflects the true swing peak from the original breakout, not a fresher (lower) high.

---

## Entry Rules

- **Trigger:** Signal fires on the close of candle N.
- **Strike Selection:** ATM Put, based on spot close at signal, rounded to the nearest 50.
- **Fill:** Entry at **next candle (N+1) PE open price** — no same-bar fills.
- **Validity Check:** If `highest_high ≤ spot open at entry`, the setup is invalid (no meaningful risk distance) and skipped.
- **Position Size:** 1 lot (65 qty).

---

## Exit Framework

Stop-loss and target are measured on **spot price**, not on the option price.

### Levels

| Level | Formula | Meaning |
|---|---|---|
| **Spot SL** | `highest_high` | If spot climbs back to the pre-entry peak, the reversal thesis is invalidated. |
| **Spot TP** | `spot_at_entry − 2 × (highest_high − spot_at_entry)` | Fixed 1:2 reward-to-risk on spot. |

### Exit Triggers

| Trigger | Condition | Fill |
|---|---|---|
| **SL Hit** | Spot high ≥ Spot SL | Exit PE at **next candle's open** |
| **TP Hit** | Spot low ≤ Spot TP | Exit PE at **next candle's open** |
| **EOD** | Time ≥ 15:20 | Exit PE at **current candle's close** |

### Same-Candle Conflict

If both SL and TP are breached in the same 1-minute bar, **SL wins** (conservative assumption, since intra-bar sequencing is unknown).

---

## Worked Example

Suppose NIFTY spot action:

- **09:45** — Breakout candle closes at 23,615 (BB upper = 23,600), high = 23,620. State → WATCHING. highest_high = 23,620.
- **09:50** — Spot pushes further to 23,645. highest_high updated to 23,645.
- **09:52** — Red candle forms: O=23,640, C=23,630, L=23,625. State → RED_FOUND. red_low = 23,625.
- **09:55** — Candle closes at 23,620 (below 23,625). **Signal fires.**
- **09:56** — Entry. Spot opens at 23,618. Buy ATM PE (strike 23,600) at its 09:56 open.
  - Risk = 23,645 − 23,618 = 27 points
  - Spot SL = 23,645
  - Spot TP = 23,618 − (2 × 27) = 23,564

From here:
- If spot high touches 23,645 → SL triggered → exit PE at next bar's open.
- If spot low touches 23,564 → TP triggered → exit PE at next bar's open.
- If neither happens by 15:20 → force-exit PE at close of 15:20 bar.

---

## Key Parameters

| Parameter | Value |
|---|---|
| Timeframe | 1-minute (spot + options) |
| Bollinger Period | 20 |
| Bollinger Std Dev | 2.0 |
| Risk-Reward Ratio | 1:2 (on spot) |
| Entry Window | 09:18 – 15:19 |
| Force Exit | 15:20 |
| Strike Selection | ATM Put, rounded to 50 |
| Lot Size | 1 lot (65 qty) |
| Same-Bar SL+TP | SL wins (conservative) |

---

## Summary

A disciplined mean-reversion Put-buying strategy with risk anchored to the **underlying spot** rather than the option itself. The stop-loss sits at the actual swing peak of the breakout, and the target is a strict 2× that risk distance below entry. One trade setup requires three confirmations — breakout, weakness (red candle), and break-down close — making each signal high-conviction.
