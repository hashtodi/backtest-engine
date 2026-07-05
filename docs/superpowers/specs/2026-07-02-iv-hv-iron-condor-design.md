# IV/HV-Ratio Iron Condor (S165) — Backtest Design Spec

**Date:** 2026-07-02
**Status:** Approved for planning
**Author:** Harsh + Claude

---

## 1. Summary

Backtest **Strategy S165**: a NIFTY weekly **iron condor** that sells over-priced
volatility. Once per morning, if the ATM implied vol is richly priced relative to
recent realized vol (`ATM_IV / HV_20d > 1.3`), sell a 4-leg condor chosen by option
delta, manage it with a profit target and stop, and force-exit intraday.

The strategy is defined in `Strategy_S165_Backtest_Specification.docx`. **That document
was produced by an earlier internal calculation of ours, not by a data vendor.** Its
`delta` values were computed against a stale **Thursday** expiry calendar and are
therefore wrong for post-Sep-2025 dates. This spec rebuilds the strategy correctly,
computing delta ourselves from `iv` against the **true** contract expiry.

**Sources of truth:** `data/options/nifty/NIFTY_OPTIONS_1m.parquet` (option chain) and
`data/spot/nifty/NIFTY_1m.parquet` (index spot).

**Backtest period:** 2020-08-03 → 2026-05-22 (full data range).

---

## 2. Data sources & integrity findings

### 2.1 Available columns

`NIFTY_OPTIONS_1m.parquet` provides: `ts, datetime, underlying, option_type,
expiry_type, expiry_code, atm_strike, strike_offset, moneyness, strike, spot, open,
high, low, close, volume, oi, iv`.

Crucially, it has **`iv`** but **no `hv_20d` and no `delta`** — the S165 doc assumed all
three were pre-baked columns. We therefore **compute** HV_20d and delta ourselves.

- `iv` is annualized, in **percent** (e.g. 27.1). Convert to decimal (`iv/100`) for BS.
- `datetime` is an ISO string with `+05:30` offset. It must be parsed as **naive IST
  wall-clock** (`pd.to_datetime(s.str.slice(0,19))`). Calling `.values`/`.to_numpy()`
  on a tz-aware series silently shifts to UTC (−5:30) and corrupts time-of-day logic.

### 2.2 The delta / expiry investigation (why we do it this way)

Verified empirically against the data (fixture: 2026-05-11 09:45, spot 23,866):

1. The doc's stated deltas (e.g. CE-24300 = 0.206) are reproduced **only** by a
   Black-Scholes T of **≈3.24 calendar days** — i.e. expiry **Thursday 2026-05-14**.
2. But the actual contracts **expire Tuesday 2026-05-12**: the ATM code-1 straddle
   collapses to intrinsic on Tue and a fresh 6-DTE weekly appears Wed (straddle jumps
   70 → 428). Confirmed in both regimes (2021 → Thursdays; 2026 → Tuesdays).
3. The `iv` column inverts closest to the **Tuesday** expiry, not Thursday.

**Conclusion:** the doc's `delta` column used a stale Thursday calendar (NIFTY weekly
was Thursday for ~25 years, until it switched to Tuesday on 2025-09-01). It is
internally inconsistent with both the prices and the `iv`. We discard it and compute
delta correctly against the **true** expiry.

**Consequence (accepted):** our correct deltas differ from the doc's, so leg selection
and P&L will **not** match the doc's headline numbers. We validate the *logic*
(signal, credit, TP/SL, points→₹) independently, not the doc's P&L. Every other §6
rule is reproduced faithfully.

---

## 3. Component architecture

Standalone per-strategy engine, following the `st_pcr_vix_credit_spread_backtest.py`
family (dataclass config + `parse_config` + pushdown loader + per-minute sim loop +
standalone reporter + thin CLI). Each unit below is independently testable.

| Unit | File | Purpose |
|---|---|---|
| BS greeks | `engine/black_scholes.py` | Pure `bs_delta / bs_price / implied_vol` |
| Historical vol | `engine/historical_vol.py` | `compute_hv20(spot_path) → {date: hv%}` |
| Expiry calendar builder | `scripts/build_weekly_expiry_calendar.py` | Derive + validate true weekly expiries → write to `config.py` |
| Engine | `engine/iv_hv_iron_condor_backtest.py` | Stage-1 signal + Stage-2 trade sim + reporter |
| CLI | `run_iv_hv_condor.py` | argparse `--start/--end/--out`, run + write + print |
| Config | `saved_strategies/iv_hv_iron_condor.json` | Nested params |
| Tests | `tests/test_black_scholes.py`, `tests/test_iv_hv_iron_condor.py` | TDD fixtures |

---

## 4. Expiry calendar

`config.NIFTY_WEEKLY_EXPIRY_DATES` currently spans only 2025-01-02 → 2026-12-29. The
backtest starts 2020-08-03, so ~76% of the period has **no** expiry dates. We extend it.

**Derivation (`scripts/build_weekly_expiry_calendar.py`):**

1. Trading days = distinct dates present in the option data (no external holiday list).
2. Candidate expiry weekday: **Thursday** for dates `< 2025-09-01`, **Tuesday** for
   dates `≥ 2025-09-01` (SEBI-mandated switch; NSE effective 2025-09-01, first Tuesday
   expiry 2025-09-02).
3. If the candidate weekday is not a trading day, roll to the **previous** trading day.
4. **Validate** each derived date against the option file via code-1 roll detection:
   the ATM code-1 straddle collapses toward intrinsic on the true expiry and jumps up
   (>1.8×) the next trading day. Emit a mismatch report for manual review.
5. Write the validated, de-duplicated, sorted list into `config.NIFTY_WEEKLY_EXPIRY_DATES`
   (covering 2020-08 → 2026), preserving the existing correct 2025-2026 entries.

**Terminal instant for T:** expiry day at **15:30 IST** (settlement/close).

---

## 5. HV_20d computation (`engine/historical_vol.py`)

From `NIFTY_1m.parquet` daily closes (last bar of each trading day):

```
daily_return = ln(close_D / close_{D-1})
sigma        = stdev(last 20 daily_returns)      # sample std
hv_20d       = sigma * sqrt(252) * 100           # annualized %
```

**No-look-ahead:** HV for trading day `D` uses the 20 daily returns ending at **D−1**
(known at 09:45 on D). Days without 20 prior returns have `hv_20d = NaN` and can never
fire a signal. Returns a `{date: hv%}` map merged onto minute bars by date.

*(The doc's HV column may have peeked at D's close; our no-look-ahead version is the
correct live form and is one reason our signals differ slightly from the doc's.)*

---

## 6. Black-Scholes delta (`engine/black_scholes.py`)

Per-contract, per-minute, from the row's own `iv`:

```
sigma = iv / 100
T     = minutes_to_expiry / 525_600         # 365 × 24 × 60; minutes to expiry-day 15:30 IST
r     = 0.065                               # risk-free rate
q     = 0.0                                 # dividend yield
d1    = (ln(S/K) + (r - q + sigma^2/2)*T) / (sigma*sqrt(T))
delta = N(d1)         if CE
delta = N(d1) - 1     if PE
```

`S` = the row's `spot`; `K` = `strike`. Rows with `iv <= 0`, `T <= 0`, or NaN yield a
NaN delta and are excluded from leg selection.

**Validated:** at the true Tuesday expiry, this reproduces sensible deltas
(CE-24300 = 0.088, PE-23500 = −0.099 on 2026-05-11). It also reproduces the *doc's*
inflated deltas exactly when fed the wrong Thursday expiry — confirming the formula is
correct and only the expiry input differed.

---

## 7. Stage 1 — Signal finder

Per minute, over the full history:

1. Keep ATM rows only (`strike_offset == 0`) — one CE and one PE.
2. `atm_iv = mean(iv of ATM CE and ATM PE)`; attach `HV_20d[date]`.
3. Keep minutes in the entry window **09:45–11:30** inclusive.
4. Drop rows where `atm_iv` or `hv_20d` is missing, zero, or negative.
5. `ratio = atm_iv / hv_20d`.
6. Keep `ratio > 1.3` ("hits").
7. For each date, take the **first** hit (earliest minute) = that day's signal.
8. `direction = "bearish"` (label only; the condor is neutral).

---

## 8. Stage 2 — Trade engine

For each signal (skip if its timestamp ≤ previous trade's exit — no overlap; 1 concurrent):

**8.1 Leg selection (locked at the signal minute).** Compute delta for every weekly
`expiry_code==1` CE/PE row at that minute. For each of the four legs, pick the row with
delta **nearest** the target; lock its strike and its `close` (entry premium):

| Leg | Action | Type | Target δ |
|---|---|---|---|
| 1 | SELL | CE | 0.20 |
| 2 | BUY  | CE | 0.08 |
| 3 | SELL | PE | −0.20 |
| 4 | BUY  | PE | −0.08 |

**8.2 Net credit.** `net = Σ sell_entry − Σ buy_entry`; `reference_premium = |net|`.
No minimum-credit filter.

**8.3 Targets.** `tp = +0.50 × reference_premium`; `sl = −2.00 × reference_premium` (points).

**8.4 Monitoring.** From the minute **after** entry up to **15:10**, on each minute's
**close** recompute running P&L in points:
`pnl_pts = Σ_sell(entry − current) + Σ_buy(current − entry)`.
Exit on the first condition met — **TP** (`pnl ≥ tp`) → **SL** (`pnl ≤ sl`) → **TIME**
(15:10). (TP/SL are mutually exclusive at any instant, so order is immaterial.)

**8.5 Fills.** **Signal-bar close** for entry and the exit-condition bar's close for
exit (§6 — deliberate, reproduces the doc's convention; note it uses a price known only
at bar close). If a leg's price is missing at a minute, carry the last valid close;
force-settle at 15:10 on the last valid close.

**8.6 Expiry day.** Always trade weekly `expiry_code == 1` — on expiry day that is the
0DTE contract. (Loader needs code 1 only; no code-2 roll.)

**8.7 Sizing → rupees.** `LOT_SIZE = 65`, `LOTS = 4`,
`pnl_inr = pnl_pts × 65 × 4`. **Gross P&L only — no brokerage / slippage / costs**
(matches every existing engine in the repo).

---

## 9. Reporting & sanity flags

Standalone-style reporter:

- `write_trades_csv` — one row per trade: date, signal time, direction, entry/exit
  times, exit_reason, per-leg (`{leg}_option_type/_strike/_offset/_entry/_exit/_delta`),
  net_credit_pts/inr, tp/sl thresholds, pnl_pts/inr, running_equity, `sanity_flag`.
- `summarize_metrics` — trades, wins/losses, win rate, mean/median/total P&L,
  max drawdown, max consecutive losses, best/worst, exit-reason mix, trading days.
- `write_equity_csv` — running equity + drawdown.
- `print_summary`.

**Sanity flag (addresses doc §9a corrupt fills):** an iron condor's mark-to-market P&L
is bounded by its spread width. Flag any trade with `|pnl_pts| > max(CE_width, PE_width)`
(+ small buffer) as a likely bad-tick fill. **Report the count and a
sanity-filtered P&L alongside the headline — do not silently drop.**

---

## 10. Configuration

Full parameter set (`saved_strategies/iv_hv_iron_condor.json`, nested sections; a
`DayContext` dataclass holds defaults, `parse_config` coerces + falls back):

| Section | Param | Value |
|---|---|---|
| signal | `iv_rv_ratio_min` | 1.3 |
| signal | `hv_lookback_days` | 20 |
| signal | `hv_lookahead` | false (use returns through D−1) |
| entry | `window_start` | 09:45 |
| entry | `window_end` | 11:30 |
| entry | `fill` | "signal_close" |
| exit | `tp_pct` | 0.50 |
| exit | `sl_pct` | 2.00 |
| exit | `hard_exit_time` | 15:10 |
| structure | `sell_ce_delta` | 0.20 |
| structure | `buy_ce_delta` | 0.08 |
| structure | `sell_pe_delta` | −0.20 |
| structure | `buy_pe_delta` | −0.08 |
| structure | `min_credit_pts` | 0.0 (none) |
| structure | `max_trades_per_day` | 1 |
| structure | `strike_step` | 50 |
| expiry | `expiry_type` / `expiry_code` | WEEK / 1 (0DTE on expiry day) |
| greeks | `risk_free_rate` | 0.065 |
| greeks | `dividend_yield` | 0.0 |
| greeks | `t_basis` | minutes/525600 to expiry-day 15:30 |
| sizing | `lots` | 4 |
| sizing | `lot_size` | 65 |
| sizing | `costs` | none — **gross P&L only** |

---

## 11. Worked fixture (2026-05-11, correct deltas)

Signal at 09:45, spot 23,866, true expiry Tue 2026-05-12 (T = 1.24 d):

| Leg | Strike | Offset | δ | Entry |
|---|---|---|---|---|
| SELL CE | 24,150 | +6 | 0.182 | 37.95 |
| BUY CE | 24,300 | +9 | 0.088 | 17.80 |
| SELL PE | 23,650 | −4 | −0.215 | 41.35 |
| BUY PE | 23,450 | −8 | −0.074 | 13.40 |

`net credit = |(37.95+41.35) − (17.80+13.40)| = 48.10 pts`;
`TP = +24.05`, `SL = −96.20`; CE width 150, PE width 200.

*(Contrast the doc's Thursday-delta fixture: SELL CE 24,300 / credit 14.05. Our shorts
sit nearer ATM because the true, shorter T yields smaller deltas.)*

---

## 12. Testing plan (TDD)

1. **`bs_delta`** — textbook check (ATM ~0.5) + fixture: CE-24300 = 0.088 at Tuesday T;
   0.206 at Thursday T (proves formula correct, expiry is the only variable).
2. **`compute_hv20`** — hand-computed σ on a small close series; NaN warm-up; no-look-ahead
   (HV[D] independent of close[D]).
3. **Expiry calendar** — derived dates match empirical roll detection on ≥3 sample weeks
   across both regimes (2021 Thu, 2026 Tue); holiday roll-back case.
4. **Signal** — on a known day, ratio + first-hit-per-day + window boundary (09:45/11:30).
5. **Leg selection** — reproduces the §11 fixture strikes/deltas/credit exactly.
6. **TP/SL/points→₹** — credit, thresholds, pnl_pts formula, gross ₹ conversion
   (`pnl_pts × 65 × 4`, no costs); exit-reason on a constructed price path.
7. **Sanity flag** — a fabricated bad-tick trade is flagged; a normal one is not.

---

## 13. Deviations

**From the doc (all because its delta was our earlier Thursday error, now corrected):**
- Delta computed against **true** expiry → different strikes, credit, P&L.
- HV is **no-look-ahead** (through D−1).
- Will not reproduce the doc's 626 trades / headline P&L; logic validated independently.

**From codebase norms (deliberate, §6-driven):**
- **Computed-delta leg selection** (all existing engines use fixed `strike_offset`).
- **Signal-bar-close entry fills** (`st_pcr_vix` uses next-bar-open).

---

## 14. Known limitations & open risks

- **Wing truncation:** data reaches only ±10 strike offsets. With correct (shorter-T)
  deltas the 0.08 wings are usually reachable, but on high-IV / far-dated cases the
  nearest available may still be ~0.10–0.14. Leg selection clamps to the deepest
  available strike and the report notes when a wing's realized δ deviates > 0.05 from
  target.
- **Expiry-day 0DTE:** tiny T makes 0.20-delta strikes sit close to ATM → tight condors
  on ~1 day/week. This is faithful to the doc's "always code 1" rule (approved).
- **Signal-bar-close fills peek** at a price known only at close; a next-bar-open
  variant is a config switch we can flip later for a realism comparison.
- **Bad ticks:** flagged via §9 sanity check, not dropped.

---

## 15. File manifest

- `engine/black_scholes.py` — new
- `engine/historical_vol.py` — new
- `engine/iv_hv_iron_condor_backtest.py` — new
- `scripts/build_weekly_expiry_calendar.py` — new
- `run_iv_hv_condor.py` — new
- `saved_strategies/iv_hv_iron_condor.json` — new
- `config.py` — **modified** (extend `NIFTY_WEEKLY_EXPIRY_DATES` back to 2020)
- `tests/test_black_scholes.py`, `tests/test_iv_hv_iron_condor.py` — new
