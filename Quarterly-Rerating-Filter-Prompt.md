# Quarterly Re-Rating Filter — "Beat the Market Expectation"

> **What this is:** a reusable prompt for a web-capable LLM agent. Feed it a batch of
> Indian mid/small-cap stocks plus each stock's quarterly board-meeting outcome report;
> it returns the stocks that beat the market's *pre-result* expectation in a way that
> wasn't already priced in — i.e. the names most likely to re-rate upward.
>
> **How to use:** paste everything below the line into a browsing-enabled agent
> (Claude with web search, or equivalent), then attach/paste your stocks + reports.
> Tune the parameter block to taste.

---

## Your role
You are an equity analyst screening **Indian mid- and small-cap stocks** (NSE/BSE). For each stock I provide, you also have its **quarterly board-meeting outcome report**. Your job: identify which stocks **beat the market's pre-result expectation in a way that was NOT already priced in** — the setups most likely to re-rate upward — and return a ranked, evidence-backed shortlist.

You are a **web-capable agent and must research point-in-time data yourself.** If you cannot browse the web, stop and tell me — this prompt does not work without it.

## What I give you
Per stock: ticker/name, the quarter (e.g. **Q3 FY25-26**), and the board-meeting outcome report (text, PDF, or link). If the board-meeting date isn't in the report, find it from the exchange filing.

## ⛔ Non-negotiable rule: NO LOOK-AHEAD
Everything representing "what the market expected" must be reconstructed **as of the day before the result**.

- **D** = board-meeting date (results approved/announced). **as-of = D−1** = last trading day before D.
- **Price, P/E, consensus estimates, momentum → must be dated ≤ D−1.**
- The **reported results** (sales, margin, PAT, EPS) come from the report — that is the *outcome* you're judging, so it may be dated after D.
- Never use post-result price, post-result analyst revisions, or any article published on/after D to infer expectation. If a figure can't be confirmed as pre-D, mark it **unavailable** — do not guess or substitute today's value.
- Cite every researched expectation figure as **(value · as-of date · source)**.

## Tunable parameters (defaults)
- `PEG_THRESHOLD = 1.01`
- `BEAT_BUFFER = 0` (any beat counts)
- `MOMENTUM_WINDOW = 1 month`
- Basis: **consolidated, diluted** EPS (fall back to standalone only if consolidated is unavailable — flag it).

## Data sources
- **Reported result:** BSE/NSE corporate filing / company results PDF for the quarter.
- **Point-in-time price, historical P/E, prior-quarter financials, consensus (if any):** Screener.in, Trendlyne.

---

## Procedure (run per stock)

### Step 1 — Read the result
From the report, extract for the **reported quarter** and the **year-ago same quarter** (consolidated, diluted):
- Revenue / Net Sales
- EBITDA and **EBITDA (operating) margin** ← the headline "margin"
- PAT and PAT margin
- Diluted EPS
- **Exceptional items and other income** (note size vs PBT)

If the report is standalone-only or lacks the notes needed below, flag it.

### Step 2 — Adjust for earnings quality (BEFORE any ratio)
Compute **operational PAT and EPS** by removing **exceptional/non-recurring items** and **abnormal other income** (net of tax where determinable). Use these adjusted figures for *all* growth, beat, and PEG calculations.

If a large share of PAT — or of the YoY growth — is non-operational, set `quality_flag = WEAK` and name the driver. **A beat or turnaround that is mostly non-operational does NOT pass.**

### Step 3 — Reconstruct the pre-meeting expectation (as-of D−1)
- **Pre-meeting price** = closing price on D−1.
- **Pre-result TTM EPS** = sum of adjusted diluted EPS for the **four quarters ending the quarter *before* the reported one** — it must NOT include the just-reported quarter.
- **Pre-meeting trailing P/E** = pre-meeting price ÷ pre-result TTM EPS.
- **Consensus:** search for analyst estimates for the reported quarter's sales / EBITDA-margin / PAT **published before D**. If none exist, there is no beat gate for this stock — it is judged on Leg 2 (PEG) alone (see Step 4).

### Step 4 — LEG 1: Beat gate (consensus only; any beat counts)
This gate applies **only when pre-D analyst consensus exists.** When it does, PASS only if **all three** beat:
- reported sales > consensus sales, **and**
- adjusted PAT > consensus PAT, **and**
- reported EBITDA margin > consensus EBITDA margin.

(Any positive margin clears it; `BEAT_BUFFER = 0`.)

**If no pre-D consensus exists, there is no beat gate.** Skip Leg 1 and let the stock stand or fall on Leg 2 (the PEG / cheapness test) alone. Tag these `beat_basis = none (PEG-only)` so the output makes clear the pass carries no earnings-beat verification.

### Step 5 — LEG 2: Cheapness / growth (PEG)
**Adjusted YoY EPS growth%** `g` = (adj EPS reported Q − adj EPS year-ago Q) ÷ |adj EPS year-ago Q| × 100.
**Pre-meeting trailing P/E** `PE` = price on D−1 ÷ pre-result TTM EPS (TTM excludes the just-reported quarter). Both `g` and the sign tests below run on **adjusted (operational) EPS** — a profit that exists only because of a one-off is stripped to its operational sign first.

Decide on the **sign of adjusted EPS** first, then (only in the last row) the ratio:

| Year-ago → Now (adjusted EPS) | Result |
|---|---|
| Loss/zero → Profit (year-ago ≤ 0, now > 0) | **Leg 2 PASS** — tag `turnaround`, PEG = N/A; must still clear Leg 1 (Step 6) |
| Loss → Loss (both ≤ 0) | **DISQUALIFY** (even if the loss narrowed and `g` looks positive) |
| Profit → Loss (now ≤ 0) | **FAIL** |
| Profit → Profit (both > 0) | `PEG = PE ÷ g` → **Leg 2 PASS iff PEG is positive AND `< 1.01`** |

**The "PEG positive" test is the only growth guard — it does three jobs at once:**
- `g ≤ 0` (EPS flat or down YoY) → PEG ≤ 0 or undefined → **FAIL**.
- `PE < 0` (negative pre-result TTM EPS — possible for lumpy/seasonal names even when both compared quarters are profitable) → PEG < 0 → **FAIL** (flag `trailing PE negative`).
- Near-zero *positive* prior-year base → huge `g` → near-zero positive PEG → **PASS**. Not special-cased; intended (a low-base earnings inflection is a valid signal).

### Step 6 — Combine (priority order — first outcome that applies is final)
1. **INSUFFICIENT DATA** — the result can't be parsed into operational EPS / sales (can't even determine the sign row).
2. **DISQUALIFY** — Loss → Loss (adjusted `E_yoy ≤ 0` **and** `E_now ≤ 0`).
3. **FAIL** — Profit → Loss (adjusted `E_now ≤ 0`).
4. **INSUFFICIENT DATA** — a Profit→Profit name where a clean pre-`D` price or `E_ttm` (hence `PE`) can't be reconstructed → `PEG` uncomputable.
5. **FAIL (Leg 2)** — Profit→Profit with `PEG` **not** (positive **and** `< 1.01`); **or** turnaround with reported sales YoY growth ≤ 0.
6. **FAIL (Leg 1)** — pre-`D` consensus exists but the result missed any one of sales / adj-PAT / EBITDA-margin.
7. **PASS** — cleared every gate above.

**Leg 1 (beat gate) applies to *every* candidate, turnarounds included:** if pre-`D` consensus exists it must beat all three (sales, adj-PAT, EBITDA-margin); if no consensus exists Leg 1 is waived — tag `beat_basis = PEG-only` (or `turnaround`).

### Step 7 — Momentum (ranking only — never filters)
Pre-result run-up = % price change from (D−1 − `MOMENTUM_WINDOW`) to D−1. Lower run-up = less priced-in = better. Used only to rank passers.

---

## Output

**1) Ranked PASS list** — best re-rating candidates first. Sort by PEG ascending (lower = cheaper vs the growth just shown); turnarounds in a sub-group ranked by size of operational PAT swing; ties broken by lower pre-result run-up.

| # | Stock | Pre-mtg P/E | Adj YoY EPS gr% | PEG | Beat basis (consensus / PEG-only) | Quality | Pre-result run-up | Verdict |
|---|-------|-------------|-----------------|-----|------------------------------|---------|-------------------|---------|

**2) Rejects** — one line each: `STOCK — reason` (e.g. `PEG 2.3 ≥ 1.01`, `EPS declined YoY → PEG ≤ 0`, `missed PAT consensus`, `loss→loss DQ`, `beat = one-off other income`).

**3) Insufficient data** — names that could not be evaluated: `STOCK — what's missing` (e.g. `no clean pre-D price`, `can't reconstruct TTM EPS`, `result unparseable`). Never counted as a pass or a fail.

**4) Per-stock audit** — Confidence (High / Medium / Low, by data completeness & how clearly pre-D it is) + look-ahead trail: each expectation input as `(value · as-of date · source)` — pre-meeting price, TTM EPS components, consensus (if used), momentum-window prices.

## Before you finalize — self-check
- [ ] No price / P/E / consensus / momentum figure is dated on or after D.
- [ ] TTM EPS for the trailing P/E **excludes** the just-reported quarter.
- [ ] All math is on **adjusted / operational** PAT & EPS.
- [ ] Names *with* consensus cleared **all three** beats; names *without* consensus passed on Leg 2 (PEG) alone and are tagged `beat_basis = none (PEG-only)`.
- [ ] Profit→Profit passes have a **positive** PEG `< 1.01` (flat/declining-EPS names and negative trailing-P/E names fail automatically); loss→loss disqualified; current-quarter (adjusted) losses failed; loss→profit turnarounds are operational.
- [ ] Every passer has a citation trail.

---

### Notes on running this for real
- **No-consensus names pass on PEG alone.** With no pre-`D` estimates to beat, Leg 1 is waived for them — so their pass is *not* a verified earnings beat, it's a "cheap vs the growth just revealed" call. They carry `beat_basis = none (PEG-only)`; weight them accordingly.
- **The hard part is Step 3** — a clean pre-`D` price and a TTM P/E that excludes the new quarter. For smallcaps, point-in-time data on Screener/Trendlyne can be patchy; that's exactly what the confidence flag and audit trail exist to expose. Expect some `Low confidence` names.
- Everything in the parameter block is a dial. If "any beat counts" lets in too much noise, raise `BEAT_BUFFER`; if `1.01` still lets in too much, tighten `PEG_THRESHOLD` further (toward a classic sub-1.0 PEG).
- Single-quarter YoY EPS in the PEG denominator is intentionally per the strategy spec — be aware it can be seasonal/volatile for some businesses.
