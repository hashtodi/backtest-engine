# Quarterly Re-Rating Filter — **Q2 FY25-26** (quarter ended 30-Sep-2025)

*v2 rules (PEG < 1.01, consensus-only beat gate). Analysis date 2026-06-29; every expectation input reconstructed as-of D−1.*

| | |
|---|---|
| **Analyzed** | **12 of 12** |
| Parameters | PEG_THRESHOLD = 1.01 · BEAT_BUFFER = 0 · MOMENTUM_WINDOW = 1 month · basis: consolidated diluted |
| Verification | **Research-only — no adversarial-verify pass** (per request). Accuracy check = **all 12 D−1 closes independently re-derived by me from raw Yahoo 2025 JSON: 12/12 matched** the agents. Confidences are single-pass. |

**Method.** Leg 1 (beat gate) applies only where genuine pre-D consensus exists; else **waived → judged on PEG alone** (`beat_basis = PEG-only`). Leg 2: trailing P/E ÷ adjusted YoY EPS growth%, PASS iff positive and **< 1.01**. TTM EPS = the four quarters ending Jun-2025 (excludes reported Sep-2025). All math on adjusted/operational EPS.

**All 12 are Profit→Profit with NO confirmable pre-D consensus → every verdict rests on PEG alone.** No turnarounds, no consensus-verified beats this batch.

> ⚠️ **The 1.01 cutoff is splitting hairs here.** Five names sit within ±0.05 of the line: CARTRADE 0.97, AARTIIND 0.98, AEGISLOG 0.95 (just pass) vs SAILIFE 1.015, SYRMA 1.031 (just fail). Single-quarter YoY EPS noise alone could flip any of them — treat the PASS/FAIL split among these five as effectively a tie.

---

## 1) Ranked PASS list — re-rating candidates

Sorted by PEG ascending; ties by lower 1-month run-up.

| # | Stock | Pre-mtg P/E | Adj YoY EPS gr% | PEG | Beat basis | Quality | 1-mo run-up | Confidence |
|---|-------|-------------|-----------------|-----|------------|---------|-------------|------------|
| 1 | **HBLENGINE** | 80.00 | 366.5% | **0.218** | none (PEG-only) | OK | 12.4% | Medium |
| 2 | **CGCL** | 29.88 | 111.1% | **0.270** | none (PEG-only) | OK | 11.4% | Medium |
| 3 | **SAGILITY** | 34.93 | 116.0% | **0.300** | none (PEG-only) | OK | 14.9% | Medium |
| 4 | **NAVINFLUOR** | 69.71 | 144.9% | **0.480** | none (PEG-only) | OK | 9.3% | High |
| 5 | **INTELLECT** | 48.00 | 92.5% | **0.520** | none (PEG-only) | OK | 7.4% | Medium |
| 6 | **HINDCOPPER** | 67.19 | 80.0% | **0.840** | none (PEG-only) | OK | -1.9% | High |
| 7 | **NEULANDLAB** | 167.36 | 194.9% | **0.860** | none (PEG-only) | OK | 15.7% | Medium |
| 8 | **AEGISLOG** | 40.58 | 42.6% | **0.952** | none (PEG-only) | OK | -7.8% | Medium |
| 9 | **CARTRADE** | 87.59 | 90.5% | **0.970** ⚠️ | none (PEG-only) | WEAK | 9.5% | Medium |
| 10 | **AARTIIND** | 59.25 | 60.6% | **0.980** ⚠️ | none (PEG-only) | WEAK | 1.2% | Medium |

⚠️ = PEG within 0.05 of the 1.01 cutoff (borderline).

---

## 2) Rejects

- **SAILIFE** — FAIL (Leg 2): PEG 1.015 ≥ 1.01 (P/E 79.00, adj EPS gr 78%). — **borderline** (barely over the line; effectively a coin-flip vs the cheapest passers)
- **SYRMA** — FAIL (Leg 2): PEG 1.031 ≥ 1.01 (P/E 71.13, adj EPS gr 69%). — **borderline** (barely over the line; effectively a coin-flip vs the cheapest passers)

---

## 3) Per-stock audit & look-ahead trail

Each expectation input as **(value · as-of date · source)**; every D−1 price independently re-derived from raw Yahoo 2025 JSON.

### HBLENGINE — PASS
*D = 2025-11-08 · as-of D−1 = 2025-11-07 · Manufacturer (industrial & defence batteries, electronics / rail signalling). *

- **Verdict:** PASS — PEG 0.218 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 80.00 ÷ adj-EPS-growth 366.5% = **0.218** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine analyst consensus for the Sep-2025 quarter published before 2025-11-08 could be confirmed. Trendlyne consensus-estimates page shows no quarterly esti
- **Earnings quality:** OK — Profit surge is OPERATIONAL - Electronics segment revenue rose from 97.52 cr (Sep-24) to 793.50 cr (Sep-25) with segment result 461.49 cr. Exceptional item is an expense (reduces reported profit), other income immaterial. Genuine operating beat, not an accounting one-off. (Management flags it as non-repeatable, but that is a forward-guidance caveat, not an earnings-quality defect.)
- **Confidence:** Medium (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹979.20 · 2025-11-07 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹12.24 = [Sep-2024 ₹3.13; Dec-2024 ₹2.33; Mar-2025 ₹1.62; Jun-2025 ₹5.16] (excludes Sep-2025)
   - Momentum: ₹871.15 on 2025-10-07 → run-up 12.4%

### CGCL — PASS
*D = 2025-10-29 · as-of D−1 = 2025-10-28 · NBFC / lender (Capri Global Capital Ltd) — fund-based financing (gold, housing*

- **Verdict:** PASS — PEG 0.270 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 29.88 ÷ adj-EPS-growth 111.1% = **0.270** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No quarter-specific (Sep-2025 quarter) analyst consensus for revenue / PAT / EPS / NII publishable BEFORE 2025-10-29 could be confirmed. Trendlyne consensus pag
- **Earnings quality:** OK — Earnings growth fully operational — NII +57% YoY, non-interest income +97%, cost-to-income improved 64%->49%, GNPA 1.23%. No one-offs.
- **Confidence:** Medium (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹206.48 · 2025-10-28 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹6.91 = [Sep-2024 (Q2FY25) ₹1.17; Dec-2024 (Q3FY25) ₹1.55; Mar-2025 (Q4FY25) ₹2.15; Jun-2025 (Q1FY26) ₹2.04] (excludes Sep-2025)
   - Momentum: ₹185.26 on 2025-09-26 → run-up 11.4%

### SAGILITY — PASS
*D = 2025-10-29 · as-of D−1 = 2025-10-28 · Non-financial — healthcare BPM/tech-enabled services provider (IT-enabled serv*

- **Verdict:** PASS — PEG 0.300 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 34.93 ÷ adj-EPS-growth 116.0% = **0.300** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No pre-D consensus confirmable; Leg 1 waived. Judged on Leg 2 alone per v2 rules (no proxy/trend gate).
- **Earnings quality:** OK — Growth is operationally driven: revenue +25.2% YoY (Payer +24.2%, Provider +33.4%), reported EBITDA margin expansion 23.9%->28.5%. Reported PAT growth (+113.8% YoY) and adjusted PAT growth (+84.0% YoY) are core-operations led, not driven by one-off gains. adj_eps fields use the reported basic EPS series (Screener-confirmed) as the cleanest comparable per-share figure with full four-quarter history; the company's own adjusted basis (year-ago ~0.348, now 0.64) yields the same PASS verdict.
- **Confidence:** Medium (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹49.60 · 2025-10-28 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹1.42 = [Sep-2024 (Q2FY25) ₹0.25; Dec-2024 (Q3FY25) ₹0.46; Mar-2025 (Q4FY25) ₹0.39; Jun-2025 (Q1FY26) ₹0.32] (excludes Sep-2025)
   - Momentum: ₹43.15 on 2025-09-26 → run-up 14.9%

### NAVINFLUOR — PASS
*D = 2025-10-30 · as-of D−1 = 2025-10-29 · Specialty chemicals manufacturer (non-financial; EBITDA meaningful)*

- **Verdict:** PASS — PEG 0.480 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 69.71 ÷ adj-EPS-growth 144.9% = **0.480** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D (before 2025-10-30) consensus confirmable from Trendlyne/TradingView/broker notes. Leg 1 WAIVED per v2; verdict rests on Leg 2 alone.
- **Earnings quality:** OK — Operational: 46% revenue growth + sharp EBITDA margin expansion (20.7% to 32.46%) across HPP/Specialty Chemicals/CRAMS; no exceptionals or abnormal other income.
- **Confidence:** High (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹4978.00 · 2025-10-29 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹71.41 = [Sep-2024 (Q2FY25) ₹11.85; Dec-2024 (Q3FY25) ₹16.84; Mar-2025 (Q4FY25) ₹19.13; Jun-2025 (Q1FY26) ₹23.59] (excludes Sep-2025)
   - Momentum: ₹4552.20 on 2025-09-29 → run-up 9.3%

### INTELLECT — PASS
*D = 2025-10-31 · as-of D−1 = 2025-10-30 · IT / software products (financial technology) - not a lender; EBITDA meaningfu*

- **Verdict:** PASS — PEG 0.520 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 48.00 ÷ adj-EPS-growth 92.5% = **0.520** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — Only thin annual consensus surfaced (Trendlyne ~4 analysts, FY26 EPS ~INR 34, target ~INR 1069-1144) and it is forward/undated relative to D; no genuine pre-D q
- **Earnings quality:** OK — Operational: revenue +35.8% YoY, EBITDA margin expansion (19.7%->24.3%) driven by license-linked / platform revenue growth; growth is operational, not one-off. Other income share of PBT actually FELL YoY, so YoY PAT growth is not flattered by non-operating items.
- **Confidence:** Medium (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹1046.75 · 2025-10-30 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹21.81 = [Sep-2024 (Q2FY25) ₹3.73; Dec-2024 (Q3FY25) ₹1.98; Mar-2025 (Q4FY25) ₹9.45; Jun-2025 (Q1FY26) ₹6.65] (excludes Sep-2025)
   - Momentum: ₹974.60 on 2025-09-30 → run-up 7.4%

### HINDCOPPER — PASS
*D = 2025-11-11 · as-of D−1 = 2025-11-10 · Non-financial (copper mining & refining PSU; manufacturer). EBITDA meaningful.*

- **Verdict:** PASS — PEG 0.840 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 67.19 ÷ adj-EPS-growth 80.0% = **0.840** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — Searched Trendlyne/Investing.com/news for brokerage estimates published before 2025-11-11; none confirmable pre-D. Leg 1 WAIVED.
- **Earnings quality:** OK — Growth is operational - copper realisation/volume drove revenue +38.6% YoY and EBITDA margin expanded from 29.3% to 39.3%. No exceptional items. Other income is small in the reported quarter and was actually higher in the year-ago base, so it does not flatter the YoY comparison.
- **Confidence:** High (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹337.95 · 2025-11-10 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹5.03 = [Sep-2024 ₹1.05; Dec-2024 ₹0.65; Mar-2025 ₹1.94; Jun-2025 ₹1.39] (excludes Sep-2025)
   - Momentum: ₹344.55 on 2025-10-10 → run-up -1.9%

### NEULANDLAB — PASS
*D = 2025-11-07 · as-of D−1 = 2025-11-06 · Pharma - API / CDMO manufacturer (non-financial); EBITDA meaningful*

- **Verdict:** PASS — PEG 0.860 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 167.36 ÷ adj-EPS-growth 194.9% = **0.860** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — Searches (Trendlyne/preview articles) returned no datable pre-2025-11-07 broker estimates for Q2FY26. Per v2 rules, with no genuine pre-D consensus the beat gat
- **Earnings quality:** OK — Reported Q2FY26 beat is operational: revenue +65.4% YoY (51,427 vs 31,084 lakhs), EBITDA margin expanded ~30.4% vs ~20%, no exceptional item, other income immaterial (1.4% of PBT). High operating leverage drove PAT +201% / EPS +195% YoY. Earnings quality of the reported quarter is clean.
- **Confidence:** Medium (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹17550.00 · 2025-11-06 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹104.86 = [Sep-2024 (Q2FY25) ₹25.60; Dec-2024 (Q3FY25) ₹46.75; Mar-2025 (Q4FY25) ₹21.68; Jun-2025 (Q1FY26) ₹10.83] (excludes Sep-2025)
   - Momentum: ₹15163.00 on 2025-10-06 → run-up 15.7%

### AEGISLOG — PASS
*D = 2025-11-07 · as-of D−1 = 2025-11-06 · Non-financial (oil & gas logistics / LPG & liquid terminals). EBITDA meaningfu*

- **Verdict:** PASS — PEG 0.952 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 40.58 ÷ adj-EPS-growth 42.6% = **0.952** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D consensus estimate located via search (Trendlyne/analyst figures not confirmable dated before 2025-11-07). Per v2 rules, Leg 1 WAIVED; verdict 
- **Earnings quality:** OK — EPS growth operationally driven: revenue +31% YoY, EBITDA margin stable ~12.7%. No exceptional in consolidated results; slump-sale gain eliminated on consolidation. Other income elevated but recurring treasury, not a one-off.
- **Confidence:** Medium (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹766.60 · 2025-11-06 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹18.89 = [Sep-2024 ₹3.59; Dec-2024 ₹3.54; Mar-2025 ₹8.02; Jun-2025 ₹3.74] (excludes Sep-2025)
   - Momentum: ₹831.00 on 2025-10-06 → run-up -7.8%

### CARTRADE — PASS
*D = 2025-10-28 · as-of D−1 = 2025-10-27 · Non-financial: auto-tech / digital classifieds & remarketing marketplace (CarW*

- **Verdict:** PASS — PEG 0.970 < 1.01 (PEG-only)  ⚠️ borderline
- **PEG (Leg 2):** P/E 87.59 ÷ adj-EPS-growth 90.5% = **0.970** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — Only annual/12-month consensus confirmable pre-D (7 analysts; consensus target ~Rs 2,204, FY26 profit-growth and FY-EPS ~Rs 43 figures on Trendlyne/MarketScreen
- **Earnings quality:** WEAK — Non-recurring 'liabilities no longer required written back' (Rs 1,050.58 lakh, vs Rs 27.27 lakh year-ago) plus sizeable treasury/other income; other income is ~36% of PBT and the abnormal write-back contributed ~26% of YoY PAT growth. Underlying operating (segment) growth is nonetheless genuine and strong (segment results up ~73% YoY), so quality is weak but not fully non-operational.
- **Confidence:** Medium (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹2665.40 · 2025-10-27 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹30.43 = [Sep-2024 (Q2FY25) ₹5.45; Dec-2024 (Q3FY25) ₹8.39; Mar-2025 (Q4FY25) ₹8.21; Jun-2025 (Q1FY26) ₹8.38] (excludes Sep-2025)
   - Momentum: ₹2434.10 on 2025-09-26 → run-up 9.5%

### AARTIIND — PASS
*D = 2025-11-06 · as-of D−1 = 2025-11-04 · Specialty chemicals manufacturer (non-financial; single reportable segment - S*

- **Verdict:** PASS — PEG 0.980 < 1.01 (PEG-only)  ⚠️ borderline
- **PEG (Leg 2):** P/E 59.25 ÷ adj-EPS-growth 60.6% = **0.980** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No firm, dated, complete pre-D (before 06-Nov-2025) sell-side consensus (sales + adj-PAT + margin) is confirmable. Only a vague pre-result 'around Rs 2,000 Cr r
- **Earnings quality:** WEAK — ~24% of reported PAT and a large part of the headline +104% YoY jump is non-operational - interest income on income-tax refunds booked as exceptional income plus a Rs 3 Cr tax relief; operating improvement is real but far smaller than the reported number suggests.
- **Confidence:** Medium (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹385.10 · 2025-11-04 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹6.50 = [Sep-2024 (Q2FY25) ₹1.39; Dec-2024 (Q3FY25) ₹1.27; Mar-2025 (Q4FY25) ₹2.65; Jun-2025 (Q1FY26) ₹1.19] (excludes Sep-2025)
   - Momentum: ₹380.60 on 2025-10-03 → run-up 1.2%

### SAILIFE — FAIL (Leg 2)
*D = 2025-11-06 · as-of D−1 = 2025-11-04 · Non-financial (CRDMO / contract research, development & manufacturing - pharma*

- **Verdict:** FAIL (Leg 2) — PEG 1.015 ≥ 1.01  ⚠️ borderline
- **PEG (Leg 2):** P/E 79.00 ÷ adj-EPS-growth 77.8% = **1.015** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — Stock IPO'd 18-Dec-2024; coverage thin and forecasts are full-year CAGR-based. No three-part (sales/PAT/margin) quarterly consensus confirmable before 2025-11-0
- **Earnings quality:** OK — Earnings are operational: rev +36% YoY, EBITDA margin expansion, finance cost down sharply (debt repaid from IPO). No exceptionals; other income moderate and recurring.
- **Confidence:** Medium (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹923.10 · 2025-11-04 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹11.68 = [Sep-2024 (Q2FY25) ₹2.21; Dec-2024 (Q3FY25) ₹2.52; Mar-2025 (Q4FY25) ₹4.12; Jun-2025 (Q1FY26) ₹2.83] (excludes Sep-2025)
   - Momentum: ₹882.60 on 2025-10-03 → run-up 4.6%

### SYRMA — FAIL (Leg 2)
*D = 2025-11-10 · as-of D−1 = 2025-11-07 · Non-financial (EMS / electronics manufacturing services)*

- **Verdict:** FAIL (Leg 2) — PEG 1.031 ≥ 1.01  ⚠️ borderline
- **PEG (Leg 2):** P/E 71.13 ÷ adj-EPS-growth 69.0% = **1.031** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No quarter-specific (Sep-2025/Q2 FY26) analyst consensus published before 2025-11-10 could be confirmed. Trendlyne shows only full-year FY26/FY27 forward estima
- **Earnings quality:** OK — YoY profit growth driven by operating revenue (+37.6%) and margin expansion (+154 bps), not one-offs; adjusted EPS equals reported EPS.
- **Confidence:** Medium (research-only; D−1 price independently QC'd ✓)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹799.50 · 2025-11-07 · Yahoo raw chart JSON 2025, self-parsed & matched
   - Pre-result TTM EPS = ₹11.24 = [Sep-2024 ₹2.04; Dec-2024 ₹2.74; Mar-2025 ₹3.67; Jun-2025 ₹2.79] (excludes Sep-2025)
   - Momentum: ₹851.95 on 2025-10-07 → run-up -6.2%

---

### Caveats
- **No adversarial-verify pass this batch** (research-only by request). Confidences are single-pass; the independent D−1 price QC is the main accuracy check (all 12 matched).
- **PEG-only across the board** — none had a pre-D consensus to verify an earnings beat. These are cheapness-vs-revealed-growth calls.
- **SYRMA** PDF was image-only; its financials were sourced from Screener/filings (lower confidence) — and it fails by a hair (1.031).
- Single-quarter YoY EPS in the PEG denominator is per spec; seasonal/volatile — the reason so many names cluster at the cutoff. **Not investment advice.**
