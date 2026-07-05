# OI Wall — First-Principles Analysis (Expanded Window)

Diagnostic backtests over **2020-08-03 → 2026-05-22** (1,436 trading days, ~5.7 years). Defaults as shipped: NIFTY weekly, signal 10:15, entry T+1 OPEN, sell ±2 / buy ±6, 4 lots, Rs 2L capital, TP Rs 1,200/lot, SL Rs 2,000/lot, breach OFF.

**Note:** earlier version of this doc covered only Jan 2025 → May 2026 (339 days). The options data is now 4× larger. Conclusions update meaningfully.

## Question

Is the strategy's edge real and stable across regimes, or was it a lucky window?

## What I tested

| Test | Question | Variant |
|---|---|---|
| 1 | Do the conditions filter? | min = 0 / 1 / 2 |
| 2 | Does wall side matter? | wall-selected vs force-CE vs force-PE |
| 3 | Walk-forward stability? | H1 (2020-08 → 2023-07) vs H2 (2023-07 → 2026-05) |
| 4 | Which condition does the work? | C1-only vs C2-only vs both vs neither |

## Headline results (5.7 years)

| Variant | Trades | WR | Mean P&L | Total P&L | Max DD | Worst day |
|---|---|---|---|---|---|---|
| **Strategy as configured (min=2)** | **697** | **66.6%** | **+Rs 310** | **+Rs 216,112** | **Rs 114,803** | Rs −14,534 |
| Same, min=1 (either condition) | 1,221 | 62.9% | +Rs 25 | +Rs 30,173 | Rs 219,349 | Rs −17,186 |
| Same, min=0 (no condition filter) | 1,410 | 62.9% | +Rs 47 | +Rs 65,923 | Rs 229,970 | Rs −17,186 |
| Force CE-only | 671 | 64.8% | +Rs 201 | +Rs 134,745 | Rs 114,049 | Rs −14,534 |
| **Force PE-only** | **684** | **67.3%** | **+Rs 226** | **+Rs 154,765** | **Rs 91,754** | Rs −14,014 |
| H1 (2020-08 → 2023-07) | 357 | 68.1% | +Rs 313 | +Rs 111,839 | Rs 60,788 | Rs −10,556 |
| H2 (2023-07 → 2026-05) | 340 | 65.0% | +Rs 307 | +Rs 104,273 | Rs 85,384 | Rs −14,534 |

### Per-condition breakdown (all 1,410 entries when min=0)

| Signal at 10:15 | n | WR | Mean | Total |
|---|---|---|---|---|
| **Both C1 AND C2 true** | **697** | **66.6%** | **+Rs 310** | **+Rs 216,112** |
| C1 only (price ok, OI not) | 121 | 59.5% | −Rs 431 | −Rs 52,091 |
| C2 only (OI ok, price not) | 403 | 57.6% | −Rs 332 | −Rs 133,848 |
| Neither true | 189 | 63.0% | +Rs 189 | +Rs 35,750 |

---

## Findings

### Edge 1 — The two-condition AND gate is the dominant filter

Same pattern as the short window, now confirmed at 4× the sample size. The "both required" rule produces the only meaningfully profitable bucket; C1-only days and C2-only days are big losers (−Rs 52K and −Rs 134K respectively).

**Going from min=2 to min=0 destroys the strategy: +Rs 216K → +Rs 66K total, and max DD blows out from Rs 115K to Rs 230K.** Relaxing the gate at all is catastrophic.

The two conditions remain anti-correlated as individual signals and complementary as a pair. This is the strategy's biggest edge.

### Edge 2 — Wall direction selection: smaller advantage than the short window suggested

| variant | total P&L |
|---|---|
| Force CE-only | +Rs 135K |
| Force PE-only | +Rs 155K |
| Wall-selected | +Rs 216K |

Wall-selection beats the better-individual-side (force-PE) by ~Rs 61K. Still a real edge, but only ~28% of total P&L — not the ~25% from a coin-flip baseline as estimated on the short window.

### Edge 3 — Structural baseline persists

"Neither true" days net **+Rs 36K** (mean +Rs 189). The structural credit-spread + theta + tight-stop continues to be mildly positive in any regime — small but real.

### Walk-forward holds across very different periods

H1 (2020-08 → 2023-07, post-COVID rally + 2022 chop) and H2 (2023-07 → 2026-05, recent regime) deliver **almost identical mean P&L per trade (Rs 313 vs Rs 307)** and similar win rates (68.1% vs 65.0%). Different regimes, very different vol environments — same edge intact. This is the strongest signal in the data.

---

## What changed vs the 16-month analysis

| metric | 16-month window | **5.7-year window** | what it means |
|---|---|---|---|
| Mean P&L / trade | +Rs 800 | **+Rs 310** | 2.6× lower — the 2025 window was unusually rich |
| Max DD | Rs 19K | **Rs 115K** | **6× worse** — earlier years had real losing streaks |
| Max DD % of capital | ~10% | **~57%** | risk of ruin actually present |
| Worst day | Rs −14K | Rs −14.5K | unchanged — tail-day magnitude similar |
| PE side total | **−Rs 51K (loss)** | **+Rs 155K (gain)** | **reversal** — "PE is fragile" was a regime artifact |
| Walk-forward consistency | mean R 813 vs R 783 | mean R 313 vs R 307 | both halves remain very close |

**Three things flipped:**

1. **Tail risk is real.** The short-window max DD of Rs 19K was a snapshot of a calm period. Over 5.7 years the engine shows ~57% drawdowns. The Rs 8,000 SL does not stop a clustered bad sequence.
2. **PE is not fragile.** Over the longer window PE actually outperforms CE on a force-side basis. The earlier diagnosis was wrong — that was a 2025-specific bias.
3. **Mean P&L per trade is much lower long-run.** Rs 310 vs Rs 800. The reported "67% return per year" from the short window is not sustainable; the realistic long-run pace is ~Rs 310 × ~120 trades/year = Rs 37K/year = ~18.5% on Rs 200K. Still positive but a third of the headline.

---

## What's intact

- The strategy **is profitable in every multi-year sub-period tested**.
- Both **the two-condition gate** and **the wall direction selection** continue to add measurable, independently verifiable value.
- Walk-forward is **rock solid** — per-trade mean is virtually identical in H1 (mostly 2020-2023) and H2 (mostly 2023-2026), spanning very different vol regimes.

## What's now alarming

- **Drawdowns are 6× larger** than the short window suggested. Rs 115K on Rs 200K capital means **at one point in the 5.7-year window, you were down 57% from a recent peak**. Sizing for the realistic DD means trading at maybe 1.5 lots instead of 4 lots to keep DD manageable.
- The **SL doesn't reliably cap losses** — Rs −14.5K on a Rs 8K stop = 80% overshoot from T+1 fill slippage. This is a structural feature, not a bug: you cannot avoid signal-bar-close-to-fill-bar-open gaps with minute data.
- **8-day losing streaks** occurred (in min=0/min=1; min=2 sees up to 7-day streaks). These compound quickly with size.

---

## Verdict (revised)

The strategy is **not a fluke** — both edges (conditions + wall direction) replicate cleanly across 5.7 years, and walk-forward consistency is strong. The edges are real.

But the **risk profile is much worse than the short window suggested**. Realistic expectations:

- **Per-trade expectancy:** ~Rs 310 (not Rs 800).
- **Annual return at 4 lots:** ~Rs 37K = **~18.5% on Rs 2L capital** (not 67%).
- **Likely drawdown to expect:** 30–60% of capital is plausible based on history. Plan for it.
- **Realistic deployment size:** scale lots down so max-DD is in a tolerable range. E.g., **2 lots cuts both expected P&L and DD in half** — annual ~Rs 18K with ~30% DD.

The "Rs 134K profit" from the 16-month run was the upper envelope of a calm regime, not the steady-state. The "Rs 216K over 5.7 years" is closer to truth, but with very different volatility along the way.

## Next steps before live deployment

1. **Lock current params and paper-trade forward** for 3 months minimum. Look for mean ~Rs 310/trade and confirm DD behavior.
2. **Size down to 1–2 lots** until live behavior matches backtest.
3. **Investigate the PE side reversal.** Why did 2025 alone make PE a loser? Was it directional drift, OI-quality issues, or expiry-day changes? Understanding this protects against the next regime flip.
4. **Stress-test specific periods:** isolate the worst drawdown sub-window in 2020-2026 and read what was happening in NIFTY then. Are there warning signs we could filter on?
