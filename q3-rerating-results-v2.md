# Quarterly Re-Rating Filter — Q3 FY25-26 · **v2** (PEG < 1.01, consensus-only beat gate)

*Supersedes `q3-rerating-results.md` (v1). Quarter ended 31-Dec-2025; analysis date 2026-06-29. **All 12 stocks now analyzed.***

| | |
|---|---|
| **Analyzed** | **12 of 12** |
| Parameters | **PEG_THRESHOLD = 1.01** · BEAT_BUFFER = 0 · MOMENTUM_WINDOW = 1 month · basis: consolidated diluted |
| Verification | 10 stocks: research + independent adversarial verify (all `UNCHANGED`, no look-ahead). **POONAWALLA & RRKABEL: completed inline this session** (raw Yahoo closes self-parsed + board-PDF financials) — single-pass, not adversarially re-verified → confidence Medium. |

**Method.** Two-leg gate. **Leg 1 (beat gate)** applies only where genuine pre-D analyst consensus exists (then sales, adj-PAT & EBITDA-margin must all beat); with no consensus, Leg 1 is **waived** and the name is judged on **PEG alone** (`beat_basis = none (PEG-only)`) — not an earnings-beat-verified call. **Leg 2 (PEG)** = trailing P/E ÷ adjusted YoY EPS growth%, PASS iff positive and **< 1.01**. All math on adjusted/operational EPS; TTM EPS = the four quarters ending Sep-2025 (excludes the reported Dec-2025 quarter). Turnaround = year-ago adjusted EPS ≤ 0 (none here).

All 12 are **Profit→Profit** on adjusted EPS. Only **AARTIIND** had genuine pre-D consensus (beat all three); the other 11 are PEG-only.

---

## 1) Ranked PASS list — re-rating candidates

Sorted by PEG ascending; ties broken by lower 1-month run-up.

| # | Stock | Pre-mtg P/E | Adj YoY EPS gr% | PEG | Beat basis | Quality | 1-mo run-up | Confidence |
|---|-------|-------------|-----------------|-----|------------|---------|-------------|------------|
| 1 | **IIFL** | 31.05 | 1042.1% | **0.03** | none (PEG-only) | OK | 10.3% | High |
| 2 | **AARTIIND** | 46.38 | 220.7% | **0.21** | consensus (beat all 3) | OK | 1.1% | High |
| 3 | **CRAFTSMAN** | 75.19 | 357.8% | **0.21** | none (PEG-only) | WEAK | 2.6% | High |
| 4 | **POONAWALLA** | 165.93 | 670.8% | **0.25** | none (PEG-only) | OK | 6.3% | Medium |
| 5 | **ENGINERSIN** | 19.90 | 69.5% | **0.29** | none (PEG-only) | WEAK | -8.2% | Medium |
| 6 | **RRKABEL** | 38.47 | 92.9% | **0.41** | none (PEG-only) | OK | -4.2% | Medium |
| 7 | **NAVINFLUOR** | 72.59 | 133.0% | **0.55** | none (PEG-only) | OK | 8.8% | Medium |
| 8 | **SYRMA** | 57.69 | 93.0% | **0.62** | none (PEG-only) | OK | -2.0% | High |
| 9 | **SAILIFE** | 58.08 | 76.1% | **0.76** | none (PEG-only) | OK | -12.0% | High |
| 10 | **ECLERX** | 34.50 | 39.3% | **0.88** | none (PEG-only) | OK | -9.6% | Medium |

> **PEG-only caveat:** all passers except AARTIIND cleared Leg 1 only because no pre-D consensus existed to test — "cheap vs the growth just revealed," not a verified earnings beat.
> **WEAK flags:** ENGINERSIN & CRAFTSMAN pass on adjusted/operational EPS (one-offs stripped) with lower headline confidence — see §4.

---

## 2) Rejects

- **CHOICEIN** — FAIL (Leg 2): PEG 1.13 ≥ 1.01 (P/E 83.73, adj EPS gr 74%). *(PASS in v1 under PEG < 1.2.)*
- **TARIL** — FAIL (Leg 2): PEG 1.14 ≥ 1.01 (P/E 38.47, adj EPS gr 34%). *(PASS in v1 under PEG < 1.2.)*

---

## 3) Per-stock audit & look-ahead trail

Each expectation input as **(value · as-of date · source)**; every price/momentum figure is dated ≤ D−1.

### IIFL — PASS
*D = 2026-01-22 · as-of D−1 = 2026-01-21 · NBFC-lender (NBFC-Middle Layer, RBI-registered). EBITDA / EBITDA-margin leg is N*

- **Verdict:** PASS — PEG 0.03 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 31.05 ÷ adj-EPS-growth 1042.1% = **0.03** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D (before 22-Jan-2026) quarterly consensus for Dec-2025 (sales/PAT/margin) could be confirmed. Trendlyne shows only a current Rs 585 target and F
- **Earnings quality:** OK — Earnings are operational: PPOP rose ~79% YoY (592.45 -> 1,062.62), driven by gold-loan-led AUM growth (consolidated AUM ~Rs 98,336 cr, +9% QoQ), lower cost of funds and sharply lower impairment vs the gold-loan-ban-affected base. The YoY jump is NOT flattered by one-offs — in fact a non-recurring Labour-Code charge depressed it. Asset quality improved (GNPA 1.60%, NNPA 0.75%).
- **Confidence:** High (verifier: UNCHANGED; look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹622.85 · 2026-01-21 · equitypandit.com historical data (close Rs 622.85 on 21-Jan-2026); cross-checked against marketsmojo.com which reported a -13.15% fall to Rs 540.80 on
   - Pre-result TTM EPS = ₹20.06 = [Dec-2024 (Q3 FY25) ₹0.95; Mar-2025 (Q4 FY25) ₹4.86; Jun-2025 (Q1 FY26) ₹5.45; Sep-2025 (Q2 FY26) ₹8.80] (excludes Dec-2025)
   - Momentum: ₹564.60 on 2025-12-19 → run-up 10.3%

### AARTIIND — PASS
*D = 2026-02-02 · as-of D−1 = 2026-01-30 · Manufacturing - specialty chemicals (single reportable segment). EBITDA/EBITDA-m*

- **Verdict:** PASS — PEG 0.21 < 1.01 (consensus-backed: beat all 3)
- **PEG (Leg 2):** P/E 46.38 ÷ adj-EPS-growth 220.7% = **0.21** (threshold < 1.01)
- **Beat basis (Leg 1):** consensus (beat all 3) — Univest Q3 FY26 preview, published 01-Feb-2026 (pre-D): expected Revenue ~Rs 1,840 Cr (+13.1% YoY), expected PAT ~Rs 46 Cr, expected EBITDA +35.96% YoY (~Rs 322
- **Earnings quality:** OK — Beat is fully operational: revenue +25.8% YoY, EBITDA +37% YoY, PBT-before-exceptionals tripled (40->134), operating margin expanded 11.36%->12.89%. The sole exceptional was an expense that reduced reported PAT; adjusting increases it. No abnormal other income.
- **Confidence:** High (verifier: UNCHANGED; look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹371.95 · 2026-01-30 · Yahoo Finance AARTIIND.NS daily chart, close 371.95 on Fri 30-Jan-2026 (last trading day strictly before D=02-Feb). Cross-checked vs BSE feed AARTIIND
   - Pre-result TTM EPS = ₹8.02 = [Sep-2025 ₹2.91; Jun-2025 ₹1.19; Mar-2025 ₹2.65; Dec-2024 ₹1.27] (excludes Dec-2025)
   - Momentum: ₹368.10 on 2025-12-30 → run-up 1.1%

### CRAFTSMAN — PASS
*D = 2026-01-28 · as-of D−1 = 2026-01-27 · Manufacturing (auto components — powertrain, aluminium products, industrial & en*

- **Verdict:** PASS — PEG 0.21 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 75.19 ÷ adj-EPS-growth 357.8% = **0.21** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No full 3-leg pre-D consensus (sales+PAT+margin) confirmable. Trendlyne/Simply Wall St carried only FY-level forecasts. A single Motilal Oswal Q3 PAT estimate o
- **Earnings quality:** WEAK — Two issues: (1) the headline 728% YoY PAT surge is largely a BASE EFFECT off an exceptionally depressed Dec-24 quarter (owners actually booked a small loss, EPS -0.87); (2) the beat over the analyst bar was non-operational — Motilal Oswal flags the PAT beat as driven by higher-than-expected other income while EBITDA margin was merely in-line with estimate. Underlying YoY margin DID expand (12.62%->15.18%, real operating leverage), so the operational improvement is genuine, but the surprise/beat dimension is low quality.
- **Confidence:** High (verifier: UNCHANGED; look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹7570.50 · 2026-01-27 · Yahoo Finance CRAFTSMAN.NS daily history (NSE close). Cross-checked CRAFTSMAN.BO (BSE) = Rs 7,579.05 same date; also consistent with Rs 181.8bn market
   - Pre-result TTM EPS = ₹100.68 = [Sep-2025 ₹38.09; Jun-2025 ₹29.18; Mar-2025 ₹27.99; Dec-2024 ₹5.42] (excludes Dec-2025)
   - Momentum: ₹7379.50 on 2025-12-26 → run-up 2.6%

### POONAWALLA — PASS
*D = 2026-01-16 · as-of D−1 = 2026-01-15 · NBFC-lender (EBITDA-margin n/a; judged PEG-only under v2)*

- **Verdict:** PASS — PEG 0.25 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 165.93 ÷ adj-EPS-growth 670.8% = **0.25** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No pre-D (before 16-Jan-2026) quarterly consensus confirmable; broker notes (JM/Motilal) are post-result (19-Jan+).
- **Adjusted EPS:** Reported consolidated diluted EPS Dec-25 ₹1.85 vs year-ago ₹0.24 (board PDF). Labour-code opex charge (~₹6cr) slightly understates adj EPS — kept at reported (conservative).
- **Earnings quality:** OK — Operational low-base recovery: NII +50% YoY and credit-cost normalisation lift PAT off a provision-depressed FY25 base. No exceptional GAIN (a labour-code opex charge slightly DEPRESSED PAT). PPOP/NIM margin contracted YoY — but the margin leg only bites under a consensus gate, which is absent.
- **Confidence:** Medium (verifier: completed inline (not adversarially re-verified); look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹471.25 · 2026-01-15 · Yahoo Finance POONAWALLA.NS daily chart, raw JSON self-parsed; 15-Jan close 471.25 (16-Jan close 463.95).
   - Pre-result TTM EPS = ₹2.84 = [Dec-2024 ₹0.24; Mar-2025 ₹0.81 (FY25−9M); Jun-2025 ₹0.84 (9MFY26−Q2−Q3); Sep-2025 ₹0.95] (excludes Dec-2025)
   - Momentum: ₹443.10 on 2025-12-15 → run-up 6.3%

### ENGINERSIN — PASS
*D = 2026-02-12 · as-of D−1 = 2026-02-11 · Engineering/EPC consultancy + turnkey services (manufacturing-type, NOT a lender*

- **Verdict:** PASS — PEG 0.29 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 19.90 ÷ adj-EPS-growth 69.5% = **0.29** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No quarter-specific Dec-2025 consensus (sales/PAT/margin) published BEFORE 2026-02-12 could be confirmed. Available analyst targets (avg ~Rs 234-238, Trendlyne/
- **Earnings quality:** WEAK — Over half of PBT and the bulk of the headline +218% PAT / +58% revenue surge came from a one-time turnkey contract-price reversal (Note 3), not recurring operations. Adjusted EPS ~Rs 3.28 vs reported Rs 6.18. NOTE: even on adjusted figures all beat/PEG gates still clear (see beat_gate/leg2), so the operational core also improved - but headline quality is WEAK and confidence is lowered.
- **Confidence:** Medium (verifier: UNCHANGED; look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹180.90 · 2026-02-11 · Feb-11 (D-1) close not directly retrievable at paisa precision (NSE/BSE/Yahoo historical blocked). Proxy = Feb-12 close Rs 180.90 (board met after mar
   - Pre-result TTM EPS = ₹9.09 = [Dec-2024 ₹1.94; Mar-2025 ₹4.98; Jun-2025 ₹1.16; Sep-2025 ₹1.01] (excludes Dec-2025)
   - Momentum: ₹197.00 on 2026-01-12 → run-up -8.2%

### RRKABEL — PASS
*D = 2026-01-31 · as-of D−1 = 2026-01-30 · Manufacturing (wires & cables / FMEG)*

- **Verdict:** PASS — PEG 0.41 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 38.47 ÷ adj-EPS-growth 92.9% = **0.41** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No pre-D (before 31-Jan-2026) quarterly consensus confirmable; broker result-updates (PL/Motilal) are post-result.
- **Adjusted EPS:** Reported consolidated diluted EPS Dec-25 ₹10.45 incl. ₹1,901L exceptional labour-code CHARGE; added back net of ~25.17% tax (≈₹1,423L) → adj EPS ≈ ₹11.71 vs year-ago ₹6.07 → adj g +92.9%.
- **Earnings quality:** OK — Operational: record revenue +42% YoY, volume-led domestic demand (per PL Capital result update). The only adjustment is adding back a non-recurring labour-code exceptional CHARGE, which raises adj EPS — clean quality.
- **Confidence:** Medium (verifier: completed inline (not adversarially re-verified); look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹1373.70 · 2026-01-30 · Yahoo Finance RRKABEL.NS daily chart, raw JSON self-parsed; 30-Jan close 1373.70 (last trading day before Sat 31-Jan board date).
   - Pre-result TTM EPS = ₹35.71 = [Dec-2024 ₹6.07; Mar-2025 ₹11.42 (FY25−9M); Jun-2025 ₹7.95 (9MFY26−Q2−Q3); Sep-2025 ₹10.27] (excludes Dec-2025)
   - Momentum: ₹1433.20 on 2025-12-30 → run-up -4.2%

### NAVINFLUOR — PASS
*D = 2026-02-09 · as-of D−1 = 2026-02-06 · Manufacturing (specialty/fluorochemicals). EBITDA-margin leg applied normally (o*

- **Verdict:** PASS — PEG 0.55 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 72.59 ÷ adj-EPS-growth 133.0% = **0.55** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D (published before 2026-02-09) consensus sales/PAT/EBIT-margin estimate for the Dec-2025 quarter could be confirmed. An HDFC Deep-Dive dated 08-
- **Earnings quality:** OK — Beat is operational: rev from ops +47% YoY, operating EBITDA +109% YoY, EBITDA margin +1017bps. The only adjustment (labour-code one-off) is a CHARGE, so adjusting makes results stronger, not weaker. No reliance on other income or one-offs.
- **Confidence:** Medium (verifier: UNCHANGED; look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹6429.40 · 2026-02-06 · D-1 (last trading day before 2026-02-09; Feb 7-8 weekend). NSE/quote-page snapshots show 6429.40, +2.34%, prev close 6282.70. Cross-checked on two sna
   - Pre-result TTM EPS = ₹88.57 = [Sep-2025 ₹29.02; Jun-2025 ₹23.58; Mar-2025 ₹19.13; Dec-2024 ₹16.84] (excludes Dec-2025)
   - Momentum: ₹5912.00 on 2026-01-06 → run-up 8.8%

### SYRMA — PASS
*D = 2026-01-29 · as-of D−1 = 2026-01-28 · Manufacturing (Electronics Manufacturing Services / EMS). EBITDA-margin leg appl*

- **Verdict:** PASS — PEG 0.62 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 57.69 ÷ adj-EPS-growth 93.0% = **0.62** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D (before 29-Jan-2026) quarterly consensus for the Dec-2025 quarter could be confirmed. Trendlyne shows only annual FY26/FY27 consensus (no quart
- **Earnings quality:** OK — Growth is operational and margin-led: revenue +45% YoY, operating EBITDA margin expanded ~350bps YoY (9.15%->12.61% operating; 11.65%->13.42% incl OI), export/mix benefit + lower tax. Exceptional item trivial (2.45% of PBT) and other income declined YoY, so the 108% adj-PAT growth is genuine operating performance, not one-offs.
- **Confidence:** High (verifier: UNCHANGED; look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹722.90 · 2026-01-28 · Yahoo Finance SYRMA.NS daily close, cross-confirmed on Tickertape (SYR) daily series. NSE closed 26-Jan (Republic Day); 28-Jan is the last trading day
   - Pre-result TTM EPS = ₹12.53 = [Dec-2024 ₹2.74; Mar-2025 ₹3.67; Jun-2025 ₹2.79; Sep-2025 ₹3.33] (excludes Dec-2025)
   - Momentum: ₹737.70 on 2025-12-26 → run-up -2.0%

### SAILIFE — PASS
*D = 2026-02-05 · as-of D−1 = 2026-02-04 · Manufacturing / CRDMO pharma services (single segment: Contract Research, Develo*

- **Verdict:** PASS — PEG 0.76 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 58.08 ÷ adj-EPS-growth 76.1% = **0.76** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-5-Feb-2026 quantitative analyst consensus (sales/PAT/EBITDA numbers for Dec-2025 quarter) could be confirmed - SAILIFE is a recent IPO (listed 18
- **Earnings quality:** OK — Beat is operational: revenue +26.5% YoY, EBITDA margin expanded to 33.76% from 27.23%, no abnormal other income. The only exceptional item is a negative non-recurring provision that understates the quarter; adjusting for it raises (does not create) profit.
- **Confidence:** High (verifier: UNCHANGED; look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹810.75 · 2026-02-04 · NSE close 4-Feb-2026 (last trading day before board meeting 5-Feb), cross-checked: Business Standard/MarketsMojo report close ~Rs 810.75 (-7.73%, prev
   - Pre-result TTM EPS = ₹13.96 = [Dec-2024 (Q3 FY24-25) ₹2.84; Mar-2025 (Q4 FY24-25) ₹4.30; Jun-2025 (Q1 FY25-26) ₹2.89; Sep-2025 (Q2 FY25-26) ₹3.93] (excludes Dec-2025)
   - Momentum: ₹921.70 on 2026-01-02 → run-up -12.0%

### ECLERX — PASS
*D = 2026-01-28 · as-of D−1 = 2026-01-27 · manufacturing/services (IT-ITES / data-management, analytics & process-outsourci*

- **Verdict:** PASS — PEG 0.88 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 34.50 ÷ adj-EPS-growth 39.3% = **0.88** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D quarterly consensus (sales/PAT/EBIT-margin) for the Dec-2025 quarter could be confirmed. eClerx has thin sell-side coverage; only annual price 
- **Earnings quality:** OK — Operational: revenue +25.4% YoY, operating EBITDA +33.2% YoY, margin expansion +151 bps; clean result, no exceptionals, stable other-income share.
- **Confidence:** Medium (verifier: UNCHANGED; look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹4341.00 · 2026-01-27 · Derived from MarketsMojo: ECLERX closed Rs 4,443.40 on 28-Jan-2026 at +2.36% over previous close, implying 27-Jan-2026 (D-1) close = 4443.40/1.0236 = 
   - Pre-result TTM EPS = ₹128.01 = [Sep-2025 (Q2 FY26) ₹38.10; Jun-2025 (Q1 FY26) ₹29.64; Mar-2025 (Q4 FY25) ₹31.71; Dec-2024 (Q3 FY25) ₹28.56] (excludes Dec-2025)
   - Momentum: ₹4800.00 on 2025-12-26 → run-up -9.6%

### CHOICEIN — FAIL (Leg 2)
*D = 2026-02-03 · as-of D−1 = 2026-02-02 · Diversified financial conglomerate (stock broking + wealth distribution + insura*

- **Verdict:** FAIL (Leg 2) — PEG 1.13 ≥ 1.01
- **PEG (Leg 2):** P/E 83.73 ÷ adj-EPS-growth 74.2% = **1.13** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D (published before 2026-02-03) analyst consensus estimates for Q3FY26 sales/PAT/EBITDA could be found (smaller mid-cap; Trendlyne shows no quart
- **Earnings quality:** OK — Growth is operational: revenue from operations +44.9% YoY drove EBITDA +89.7% (operating leverage; margin 29.17%->37.92%) and PAT +113.4%. Other income immaterial to the beat. Effective tax rate normal (29.0% vs 29.0% YoY).
- **Confidence:** Medium (verifier: UNCHANGED; look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹748.55 · 2026-02-02 · Yahoo Finance CHOICEIN.NS daily close 2 Feb 2026 = Rs 748.55; cross-checked on BSE listing CHOICEIN.BO = Rs 749.40 (same date). Corroborated by point-
   - Pre-result TTM EPS = ₹8.94 = [Sep-2025 (Q2FY26) ₹2.69; Jun-2025 (Q1FY26) ₹2.19; Mar-2025 (Q4FY25) ₹2.59; Dec-2024 (Q3FY25) ₹1.47] (excludes Dec-2025)
   - Momentum: ₹850.30 on 2026-01-02 → run-up -12.0%

### TARIL — FAIL (Leg 2)
*D = 2026-01-08 · as-of D−1 = 2026-01-07 · Manufacturing (power transformers / electrical equipment) — single segment. EBIT*

- **Verdict:** FAIL (Leg 2) — PEG 1.14 ≥ 1.01
- **PEG (Leg 2):** P/E 38.47 ÷ adj-EPS-growth 33.7% = **1.14** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D (before 2026-01-08) sell-side consensus for the Dec-2025 quarter could be confirmed. Trendlyne shows only a present-day 3-analyst 1-yr target (
- **Earnings quality:** OK — Growth is operational: revenue +31.7% YoY, EBITDA margin expanded ~130bps YoY, other income actually declined YoY. No exceptionals in the compared quarters. Adjusted = reported.
- **Confidence:** High (verifier: UNCHANGED; look-ahead violation: False)
- **Look-ahead trail:**
   - Pre-mtg price: ₹321.25 · 2026-01-07 · D-1 (Jan-7-2026) close, corroborated two ways: scanx.trade reports 'previous close of Rs 321.25' before the Jan-8 result; equitypandit.com historical 
   - Pre-result TTM EPS = ₹8.35 = [Dec 2024 ₹1.84; Mar 2025 ₹3.14; Jun 2025 ₹2.24; Sep 2025 ₹1.13] (excludes Dec-2025)
   - Momentum: ₹514.05 on 2025-12-05 → run-up -37.5%

---

### Caveats
- **PEG-only passes carry no earnings-beat verification** (11 of 12 had no pre-D consensus).
- POONAWALLA & RRKABEL were completed inline (single-pass), not through the independent adversarial-verify used for the other 10.
- Single-quarter YoY EPS in the PEG denominator is per spec; can be seasonal/volatile. **Not investment advice.**
