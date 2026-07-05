# Quarterly Re-Rating Filter — **Q4 FY25-26** (quarter ended 31-Mar-2026)

*v2 rules (PEG < 1.01, consensus-only beat gate). Analysis date 2026-06-29; every expectation input reconstructed as-of D−1.*

| | |
|---|---|
| **Analyzed** | **15 of 15** |
| Parameters | PEG_THRESHOLD = 1.01 · BEAT_BUFFER = 0 · MOMENTUM_WINDOW = 1 month · basis: consolidated diluted |
| Verification | Each stock: research + independent adversarial verify. **All D−1 closes independently re-derived by me from raw Yahoo 2026 JSON** — 14/15 matched the agents; MINDACORP corrected (495.2→536.85), SBFC corrected (1-day shift, 94.93→94.94). |

**Method.** Leg 1 (beat gate) applies only where genuine pre-D consensus exists (sales, adj-PAT & margin must all beat); else **waived → judged on PEG alone** (`beat_basis = PEG-only`). Leg 2: trailing P/E ÷ adjusted YoY EPS growth%, PASS iff positive and **< 1.01**. Turnaround = year-ago adj EPS ≤ 0 → now > 0 (PEG N/A; needs sales YoY > 0 and to clear any consensus gate). TTM EPS = the four quarters ending Dec-2025 (excludes reported Mar-2026). All math on adjusted/operational EPS.

---

## 1) Ranked PASS list — re-rating candidates

Sorted by PEG ascending; ties broken by lower 1-month run-up. Turnarounds listed separately (PEG N/A).

| # | Stock | Pre-mtg P/E | Adj YoY EPS gr% | PEG | Beat basis | Quality | 1-mo run-up | Confidence |
|---|-------|-------------|-----------------|-----|------------|---------|-------------|------------|
| 1 | **IIFL** | 14.63 | 182.9% | **0.08** | none (PEG-only) | OK | -2.3% | High |
| 2 | **NEULANDLAB** | 125.71 | 664.9% | **0.19** | consensus (beat all 3) | OK | 20.9% | High |
| 3 | **EMMVEE** | 20.40 | 89.3% | **0.23** | none (PEG-only) | OK | 18.1% | High |
| 4 | **POONAWALLA** | 100.25 | 291.2% | **0.34** | none (PEG-only) | OK | 11.1% | High |
| 5 | **MINDACORP** | 48.38 | 139.0% | **0.35** | consensus (beat all 3) | OK | -1.4% | Medium |
| 6 | **ANGELONE** | 36.00 | 82.0% | **0.44** | none (PEG-only) | OK | 38.1% | Medium |
| 7 | **CGCL** | 20.25 | 36.7% | **0.55** | none (PEG-only) | OK | 11.0% | Medium |
| 8 | **NAVINFLUOR** | 59.77 | 106.0% | **0.56** | consensus (beat all 3) | OK | 9.4% | Medium |
| 9 | **SHYAMMETL** | 25.96 | 41.4% | **0.63** | none (PEG-only) | OK | 7.8% | High |
| 10 | **SBFC** | 24.85 | 29.1% | **0.85** | none (PEG-only) | OK | 12.5% | High |
| 11 | **CRAFTSMAN** | 54.17 | 56.2% | **0.96** | none (PEG-only) | OK | 14.6% | High |

**Turnaround sub-group** (loss→operational profit; PEG N/A):

| # | Stock | Adj EPS swing | Beat basis | Sales YoY | Quality | 1-mo run-up | Confidence |
|---|-------|---------------|------------|-----------|---------|-------------|------------|
| 12 | **HFCL** | ₹-0.56 → ₹1.21 | consensus (beat all 3) | >0 | OK | 51.4% | High |
| 13 | **NSLNISP** | ₹-1.62 → ₹1.34 | none (PEG-only) | >0 | OK | 10.9% | Medium |

> **PEG-only caveat:** names tagged PEG-only had no pre-D consensus to test — "cheap vs the growth just revealed," not a verified earnings beat. Consensus-backed passes (NEULANDLAB, NAVINFLUOR, MINDACORP, HFCL) are stronger.
> **Borderline:** CRAFTSMAN PEG 0.96 sits just under the 1.01 cutoff — sensitive to small input changes.

---

## 2) Rejects

- **RAILTEL** — FAIL (Leg 2): PEG 1.33 ≥ 1.01 (P/E 32.98, adj EPS gr 25%).
- **VIJAYA** — FAIL (Leg 2): PEG 2.06 ≥ 1.01 (P/E 76.45, adj EPS gr 37%).

---

## 3) Per-stock audit & look-ahead trail

Each expectation input as **(value · as-of date · source)**; every price is dated ≤ D−1 and was independently re-derived.

### IIFL — PASS
*D = 2026-04-29 · as-of D−1 = 2026-04-28 · NBFC-lender (financial company). EBITDA / EBITDA-margin leg is not meaningful an*

- **Verdict:** PASS — PEG 0.08 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 14.63 ÷ adj-EPS-growth 182.9% = **0.08** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D consensus estimate set located. Leg 1 WAIVED; judged on Leg 2 (PEG) alone — beat_basis PEG-only.
- **Earnings quality:** OK — —
- **Confidence:** High (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹438.05 · 2026-04-28 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹29.95 = [Mar-2025 (Q4FY25) ₹4.85; Jun-2025 (Q1FY26) ₹5.45; Sep-2025 (Q2FY26) ₹8.80; Dec-2025 (Q3FY26) ₹10.85] (excludes Mar-2026)
   - Momentum: ₹448.25 on 2026-03-27 → run-up -2.3%

### NEULANDLAB — PASS
*D = 2026-05-12 · as-of D−1 = 2026-05-11 · Manufacturing (pharma API/CDMO). EBITDA-margin leg applies normally.*

- **Verdict:** PASS — PEG 0.19 < 1.01 (consensus-backed: beat all 3)
- **PEG (Leg 2):** P/E 125.71 ÷ adj-EPS-growth 664.9% = **0.19** (threshold < 1.01)
- **Beat basis (Leg 1):** consensus (beat all 3) — Pre-D consensus published 9 Apr 2026 on Univest, sourced from MOFSL, YES Securities, JM Financial: Revenue Rs 560-610 Cr, PAT Rs 92-108 Cr, EBITDA margin 26-28%. Confirme
- **Earnings quality:** OK — Operational beat: revenue from ops up 136% YoY (Europe + USA/North America segment surge) with EBITDA margin expansion from 15.6% to ~40%; profit growth is operational, not driven by exceptionals or other income.
- **Confidence:** High (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹17550.00 · 2026-05-11 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹139.62 = [Mar-2025 (Q4FY25) ₹21.68; Jun-2025 (Q1FY26) ₹10.83; Sep-2025 (Q2FY26) ₹75.49; Dec-2025 (Q3FY26) ₹31.62] (excludes Mar-2026)
   - Momentum: ₹14512.00 on 2026-04-13 → run-up 20.9%

### EMMVEE — PASS
*D = 2026-04-28 · as-of D−1 = 2026-04-27 · Manufacturing (solar PV cells/modules + small EPC & IPP). EBITDA-margin leg appl*

- **Verdict:** PASS — PEG 0.23 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 20.40 ÷ adj-EPS-growth 89.3% = **0.23** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D analyst consensus confirmable for the Mar-2026 quarter. Recent IPO with thin/no published estimates. Leg 1 WAIVED; judged on Leg 2 (PEG) alone - beat_bas
- **Earnings quality:** OK — PAT growth driven by 61.7% revenue growth (capacity ramp post-IPO) with stable ~33% EBITDA margin and lower finance cost (post-IPO deleveraging); fully operational, not other income or one-offs.
- **Confidence:** High (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹263.73 · 2026-04-27 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹12.94 = [Mar-2025 (Q4FY25) ₹2.99; Jun-2025 (Q1FY26) ₹2.72; Sep-2025 (Q2FY26) ₹3.44; Dec-2025 (Q3FY26) ₹3.79] (excludes Mar-2026)
   - Momentum: ₹223.30 on 2026-03-27 → run-up 18.1%

### POONAWALLA — PASS
*D = 2026-05-05 · as-of D−1 = 2026-05-04 · NBFC-lender (Poonawalla Fincorp Ltd). EBITDA/EBITDA-margin not meaningful for a *

- **Verdict:** PASS — PEG 0.34 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 100.25 ÷ adj-EPS-growth 291.2% = **0.34** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No reliable, internally-consistent, dated pre-D analyst consensus found. Leg 1 waived; judged on Leg 2 PEG alone (beat_basis: PEG-only).
- **Earnings quality:** OK — Operational: net interest income growth, AUM expansion and margin (NIM) improvement; no exceptionals or abnormal other income.
- **Confidence:** High (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹437.10 · 2026-05-04 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹4.36 = [Mar-2025 ₹0.80; Jun-2025 ₹0.80; Sep-2025 ₹0.91; Dec-2025 ₹1.85] (excludes Mar-2026)
   - Momentum: ₹393.30 on 2026-04-02 → run-up 11.1%

### MINDACORP — PASS
*D = 2026-05-22 · as-of D−1 = 2026-05-21 · manufacturing (automotive component maker). EBITDA-margin leg applies normally.*

- **Verdict:** PASS — PEG 0.35 < 1.01 (consensus-backed: beat all 3)
- **PEG (Leg 2):** P/E 48.38 ÷ adj-EPS-growth 139.0% = **0.35** (threshold < 1.01)
- **Beat basis (Leg 1):** consensus (beat all 3) — Pre-D consensus from Univest preview published 27-Apr-2026 (pre-D): Revenue ~Rs 1,598 Cr, PAT ~Rs 100 Cr, with expected margin improvement. Press release explicitly state
- **Earnings quality:** OK — Operating performance strong (EBITDA +33%, PBT +90% YoY); however the headline PAT growth (+138%) is amplified by tax-rate normalization (lower effective tax / tax credit), a recurring rather than one-off item, so no exceptional adjustment applied.
- **Confidence:** Medium (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹536.85 · 2026-05-21 · Yahoo raw chart JSON (2026), self-parsed & QC'd; QC: agent used Rs 495.2 (2025 data via a mis-ranged URL); true D-1 close Rs 536.85 (raw Yahoo 2026, result-day 22-May gapped +7%). PE/PEG recomputed; verdict unaffected.
   - Pre-result TTM EPS = ₹12.03 = [Mar-2025 ₹2.18; Jun-2025 ₹2.73; Sep-2025 ₹3.54; Dec-2025 ₹3.58] (excludes Mar-2026)
   - Momentum: ₹544.45 on 2026-04-21 → run-up -1.4%

### ANGELONE — PASS
*D = 2026-04-16 · as-of D−1 = 2026-04-15 · NBFC/broker (retail broking + MTF lending). EBITDA-margin leg is not meaningful *

- **Verdict:** PASS — PEG 0.44 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 36.00 ÷ adj-EPS-growth 82.0% = **0.44** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — Leg 1 WAIVED — no genuine, confirmable pre-2026-04-16 three-metric (sales+PAT+margin) consensus. Judged on Leg 2 (PEG) alone; beat_basis = PEG-only.
- **Earnings quality:** OK — Clean operational growth — PAT and EPS nearly doubled YoY driven by higher interest income (MTF book) and broking F&O revenue; no reliance on one-offs or abnormal other income.
- **Confidence:** Medium (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹297.76 · 2026-04-15 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹8.27 = [Mar-2025 (Q4FY25) ₹1.89; Jun-2025 (Q1FY26) ₹1.23; Sep-2025 (Q2FY26) ₹2.27; Dec-2025 (Q3FY26) ₹2.88] (excludes Mar-2026)
   - Momentum: ₹215.58 on 2026-03-13 → run-up 38.1%

### CGCL — PASS
*D = 2026-04-30 · as-of D−1 = 2026-04-29 · NBFC-lender (Capri Global Capital - diversified NBFC: MSME, gold, housing, const*

- **Verdict:** PASS — PEG 0.55 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 20.25 ÷ adj-EPS-growth 36.7% = **0.55** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D analyst consensus for the Mar-2026 quarter (sales/PAT/margin) could be confirmed as published before 2026-04-30. Leg 1 WAIVED; stock judged on Leg 2 PEG 
- **Earnings quality:** OK — —
- **Confidence:** Medium (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹183.69 · 2026-04-29 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹9.07 = [Mar-2025 (Q4FY25) ₹2.15; Jun-2025 (Q1FY26) ₹1.82; Sep-2025 (Q2FY26) ₹2.45; Dec-2025 (Q3FY26) ₹2.65] (excludes Mar-2026)
   - Momentum: ₹165.54 on 2026-03-27 → run-up 11.0%

### NAVINFLUOR — PASS
*D = 2026-04-29 · as-of D−1 = 2026-04-28 · manufacturing (specialty fluorochemicals). EBITDA-margin leg applies normally.*

- **Verdict:** PASS — PEG 0.56 < 1.01 (consensus-backed: beat all 3)
- **PEG (Leg 2):** P/E 59.77 ÷ adj-EPS-growth 106.0% = **0.56** (threshold < 1.01)
- **Beat basis (Leg 1):** consensus (beat all 3) — Pre-D consensus from Univest preview updated 9-Apr-2026 (before D=29-Apr-2026): Revenue Rs 600-660 cr, PAT Rs 96-112 cr, EBITDA margin 26-28%. Basis ambiguous/standalone-
- **Earnings quality:** OK — YoY growth is operational: revenue +33.8% YoY, PBT-before-exceptional +111% YoY, EBITDA +80% YoY. Exceptional gain is small (~5% of PAT) and removed in adjusted figures.
- **Confidence:** Medium (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹6627.00 · 2026-04-28 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹110.88 = [Mar-2025 (Q4 FY25) ₹19.13; Jun-2025 (Q1 FY26) ₹23.59; Sep-2025 (Q2 FY26) ₹28.92; Dec-2025 (Q3 FY26) ₹39.24] (excludes Mar-2026)
   - Momentum: ₹6055.00 on 2026-03-27 → run-up 9.4%

### SHYAMMETL — PASS
*D = 2026-05-11 · as-of D−1 = 2026-05-08 · Manufacturing (integrated steel & allied products) — EBITDA/operating-margin leg*

- **Verdict:** PASS — PEG 0.63 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 25.96 ÷ adj-EPS-growth 41.4% = **0.63** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No complete pre-D consensus confirmable. Only a SINGLE-broker estimate exists pre-D: ICICI Securities Q4 PAT estimate Rs 325.4 cr (up 47.8% YoY), published 24-Apr-2026 (m
- **Earnings quality:** OK — Operational margin expansion in core steel business; no exceptional items, no abnormal other income.
- **Confidence:** High (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹903.50 · 2026-05-08 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹34.81 = [Mar-2025 (Q4 FY25) ₹7.89; Jun-2025 (Q1 FY26) ₹10.47; Sep-2025 (Q2 FY26) ₹9.38; Dec-2025 (Q3 FY26) ₹7.07] (excludes Mar-2026)
   - Momentum: ₹838.40 on 2026-04-08 → run-up 7.8%

### SBFC — PASS
*D = 2026-04-25 · as-of D−1 = 2026-04-24 · NBFC-lender (RBI Middle Layer, secured MSME / gold lending). EBITDA/operating-ma*

- **Verdict:** PASS — PEG 0.85 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 24.85 ÷ adj-EPS-growth 29.1% = **0.85** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine quarter-specific consensus (Mar-2026 quarter sales/PAT/margin) could be confirmed as published before 2026-04-25. Only annual FY26/FY27 growth forecasts (e.g. 
- **Earnings quality:** OK — Clean operational growth from core lending (interest income up 28% YoY, AUM/loans up ~29%); PAT growth is fully operational, not driven by one-offs or other income.
- **Confidence:** High (verifier: UNCHANGED; look-ahead flag: True)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹94.94 · 2026-04-24 · Yahoo raw chart JSON (2026), self-parsed & QC'd; QC: verifier corrected a 1-day sourcing shift; true D-1 (Fri 24-Apr) close Rs 94.94 vs claimed 94.93 — immaterial.
   - Pre-result TTM EPS = ₹3.82 = [Q4 FY25 (Mar-2025) ₹0.86; Q1 FY26 (Jun-2025) ₹0.91; Q2 FY26 (Sep-2025) ₹0.99; Q3 FY26 (Dec-2025) ₹1.06] (excludes Mar-2026)
   - Momentum: ₹84.41 on 2026-03-24 → run-up 12.5%

### CRAFTSMAN — PASS
*D = 2026-05-07 · as-of D−1 = 2026-05-06 · manufacturing (auto components / precision engineering — Powertrain, Aluminium P*

- **Verdict:** PASS — PEG 0.96 < 1.01 (PEG-only)
- **PEG (Leg 2):** P/E 54.17 ÷ adj-EPS-growth 56.2% = **0.96** (threshold < 1.01)
- **Beat basis (Leg 1):** none (PEG-only) — No genuine pre-D (before 2026-05-07) QUARTERLY consensus for the Mar-2026 quarter (sales/PAT/margin) could be confirmed. Available analyst data are forward annual estimat
- **Earnings quality:** OK — Growth is operationally driven: revenue +27% YoY, EBITDA margin expanded 13.9%->16.1%, Aluminium Products & Powertrain segment profits up sharply. Exceptional/other-income items minor; year-ago add-back (Rs 1,071 lakh exceptional loss) modestly lifts the year-ago adjusted base, reducing adjusted growth vs reported, but verdict robust to this.
- **Confidence:** High (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹7774.50 · 2026-05-06 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹143.53 = [Mar-2025 (Q4FY25) ₹31.36; Jun-2025 (Q1FY26) ₹29.18; Sep-2025 (Q2FY26) ₹38.09; Dec-2025 (Q3FY26) ₹44.90] (excludes Mar-2026)
   - Momentum: ₹6781.50 on 2026-04-06 → run-up 14.6%

### HFCL — TURNAROUND_PASS
*D = 2026-04-30 · as-of D−1 = 2026-04-29 · manufacturing (telecom equipment / optical fibre + defence + EPC); EBITDA-margin*

- **Verdict:** TURNAROUND_PASS — loss→operational profit, reported sales YoY > 0 (consensus-backed)
- **Leg 2:** turnaround — adj EPS ₹-0.56 (Mar-25) → ₹1.21 (Mar-26); PEG N/A
- **Beat basis (Leg 1):** consensus (beat all 3) — Pre-D consensus from univest.in Q4FY26 preview, published/updated 14-Apr-2026 (before D=30-Apr-2026): Revenue ~Rs 1,050 Cr, PAT ~Rs 42 Cr, operating/EBITDA margin ~8%. Co
- **Earnings quality:** OK — Genuine operational turnaround: revenue +127.8% YoY (telecom export surge + defence ramp), operating EBITDA swung from -35.97 Cr to +314.67 Cr. PAT improvement is operating-driven, not from exceptional gains or abnormal other income.
- **Confidence:** High (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹107.10 · 2026-04-29 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹0.36 = [Mar-2025 ₹-0.56; Jun-2025 ₹-0.22; Sep-2025 ₹0.47; Dec-2025 ₹0.67] (excludes Mar-2026)
   - Momentum: ₹70.76 on 2026-03-27 → run-up 51.4%

### NSLNISP — TURNAROUND_PASS
*D = 2026-05-29 · as-of D−1 = 2026-05-28 · Manufacturing (iron & steel producer). EBITDA/operating-margin leg is meaningful*

- **Verdict:** TURNAROUND_PASS — loss→operational profit, reported sales YoY > 0 (PEG-only)
- **Leg 2:** turnaround — adj EPS ₹-1.62 (Mar-25) → ₹1.34 (Mar-26); PEG N/A
- **Beat basis (Leg 1):** none (PEG-only) — Pre-D estimates from MOFSL/YES/JM/ICICIdirect were cited in aggregated previews but figures are mutually contradictory and not confirmable as dated pre-D; treated as no g
- **Earnings quality:** OK — Operational: highest-ever quarterly revenue and record 20.77% operating margin drove the swing from loss to profit; no exceptional items or abnormal other income.
- **Confidence:** Medium (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹46.01 · 2026-05-28 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹-2.75 = [Mar-2025 (Q4 FY25) ₹-1.62; Jun-2025 (Q1 FY26) ₹0.09; Sep-2025 (Q2 FY26) ₹-0.39; Dec-2025 (Q3 FY26) ₹-0.83] (excludes Mar-2026)
   - Momentum: ₹41.48 on 2026-04-28 → run-up 10.9%

### RAILTEL — FAIL (Leg 2)
*D = 2026-04-30 · as-of D−1 = 2026-04-29 · Telecom / IT-infrastructure services PSU (manufacturing-style P&L; EBITDA-margin*

- **Verdict:** FAIL (Leg 2) — PEG 1.33 ≥ 1.01
- **PEG (Leg 2):** P/E 32.98 ÷ adj-EPS-growth 24.7% = **1.33** (threshold < 1.01)
- **Beat basis (Leg 1):** consensus (beat all 3) — Pre-D consensus EXISTS. Univest Q4 FY26 results preview, updated 28-Apr-2026 6:25pm (before D=30-Apr-2026), citing brokerages MOFSL/YES Securities/JM Financial: Q4 FY26 e
- **Earnings quality:** OK — Operational project/telecom revenue growth; no exceptional items or abnormal other income.
- **Confidence:** High (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹326.64 · 2026-04-29 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹9.90 = [Mar-2025 (Q4 FY25) ₹3.53; Jun-2025 (Q1 FY26) ₹2.06; Sep-2025 (Q2 FY26) ₹2.37; Dec-2025 (Q3 FY26) ₹1.95] (excludes Mar-2026)
   - Momentum: ₹260.50 on 2026-03-27 → run-up 25.4%

### VIJAYA — FAIL (Leg 2)
*D = 2026-05-07 · as-of D−1 = 2026-05-06 · Healthcare/diagnostics services (manufacturing-style P&L; EBITDA-margin leg is m*

- **Verdict:** FAIL (Leg 2) — PEG 2.06 ≥ 1.01
- **PEG (Leg 2):** P/E 76.45 ÷ adj-EPS-growth 37.1% = **2.06** (threshold < 1.01)
- **Beat basis (Leg 1):** consensus (missed) — Only pre-D consensus locatable: Univest Q4-preview updated 2026-04-09 (pre-D), citing brokerage range Revenue INR 205-225 cr, PAT INR 53-62 cr, EBITDA margin 34-36% (attr
- **Earnings quality:** OK — Clean operational beat: volume growth + ~7-9% realization uplift + ~390bps operating-margin expansion; no exceptional/one-off items.
- **Confidence:** High (verifier: UNCHANGED; look-ahead flag: False)
- **Look-ahead trail:**
   - Pre-mtg price (D−1): ₹1190.40 · 2026-05-06 · Yahoo raw chart JSON (2026), self-parsed & QC'd
   - Pre-result TTM EPS = ₹15.57 = [Mar-2025 ₹3.40; Jun-2025 ₹3.76; Sep-2025 ₹4.21; Dec-2025 ₹4.20] (excludes Mar-2026)
   - Momentum: ₹898.60 on 2026-04-06 → run-up 32.5%

---

### Caveats
- **PEG-only passes carry no earnings-beat verification.** Several names (incl. the NBFCs IIFL, POONAWALLA, SBFC, CGCL, broker ANGELONE) had no pre-D consensus and pass on PEG alone.
- High 1-month run-ups (HFCL +51%, ANGELONE +38%, VIJAYA +32%) mean more was already priced in — momentum is ranking-only, never a filter.
- POONAWALLA & RAILTEL had low-text (part-scanned) PDFs; figures cross-checked but treat with extra care.
- Single-quarter YoY EPS in the PEG denominator is per spec; can be seasonal/volatile. **Not investment advice.**
