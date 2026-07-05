# IV/HV-Ratio Iron Condor (S165) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task (Harsh's preference: execute inline, no per-task subagents). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backtest strategy S165 — a NIFTY weekly iron condor that sells when `ATM_IV / HV_20d > 1.3`, picking legs by computed Black-Scholes delta, managed with TP/SL and a 15:10 hard exit.

**Architecture:** Standalone per-strategy engine in the `st_pcr_vix_credit_spread_backtest.py` family (dataclass `DayContext` + `parse_config` + pyarrow-pushdown loader + per-minute sim loop + standalone reporter + thin CLI). Two new pure helpers (`black_scholes`, `historical_vol`) and a one-time expiry-calendar builder feed it.

**Tech Stack:** Python 3, pandas, pyarrow, numpy, stdlib `math` (NO scipy — use `math.erf`), pytest.

**Spec:** `docs/superpowers/specs/2026-07-02-iv-hv-iron-condor-design.md`

## Global Constraints

- **Datetime is naive IST wall-clock.** Parse only the tz-less prefix: `pd.to_datetime(s.str.slice(0,19))`. NEVER call `.values`/`.to_numpy()` on a tz-aware datetime series (silently shifts −5:30 to UTC).
- **Gross P&L only.** `pnl_inr = pnl_pts × 65 × 4`. No brokerage / slippage / costs.
- `LOT_SIZE = 65`, `LOTS = 4`, `strike_step = 50`.
- **Weekly `expiry_code == 1` only** (on expiry day this is the 0DTE contract — trade it; no code-2 roll).
- Delta: `bs_delta`, `sigma = iv/100`, `T = minutes_to_expiry / 525_600` to expiry-day **15:30 IST**, `r = 0.065`, `q = 0.0`.
- HV_20d: no-look-ahead (returns through **D−1**).
- No scipy. Sources of truth: `data/options/nifty/NIFTY_OPTIONS_1m.parquet`, `data/spot/nifty/NIFTY_1m.parquet`.
- **Git:** Harsh manages git. Do NOT run `git commit`/`add`/`push`. At each checkpoint, surface a copy-paste commit message.

---

## File Structure

| File | Responsibility |
|---|---|
| `engine/black_scholes.py` (new) | Pure `bs_delta` (+ `_norm_cdf`) |
| `engine/historical_vol.py` (new) | `compute_hv20(spot_path)` → `{date: hv%}` |
| `scripts/build_weekly_expiry_calendar.py` (new) | Derive + validate true weekly expiries; emit `date(...)` lines for config |
| `config.py` (modify) | Extend `NIFTY_WEEKLY_EXPIRY_DATES` back to 2020 |
| `engine/iv_hv_iron_condor_backtest.py` (new) | `DayContext`, `parse_config`, loader, Stage-1 signal, delta+leg selection, Stage-2 sim, reporter |
| `run_iv_hv_condor.py` (new) | argparse CLI |
| `saved_strategies/iv_hv_iron_condor.json` (new) | Nested config |
| `tests/test_black_scholes.py` (new) | Delta unit tests |
| `tests/test_historical_vol.py` (new) | HV unit tests |
| `tests/test_iv_hv_iron_condor.py` (new) | Signal, leg-select, sim, reporter tests |

---

## Task 1: Black-Scholes delta

**Files:**
- Create: `engine/black_scholes.py`
- Test: `tests/test_black_scholes.py`

**Interfaces:**
- Produces: `bs_delta(option_type: str, S: float, K: float, sigma: float, T: float, r: float = 0.065, q: float = 0.0) -> float` — `option_type` is `"CE"`/`"PE"`; `sigma` decimal; `T` years. Returns `float('nan')` for degenerate inputs.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_black_scholes.py
"""Tests for Black-Scholes delta."""
import math
from engine.black_scholes import bs_delta


def test_ce_delta_in_range():
    d = bs_delta("CE", 100, 100, 0.20, 1.0, r=0.0, q=0.0)
    assert 0.0 < d < 1.0
    # ATM call with zero rate/carry is slightly above 0.5
    assert abs(d - 0.5398) < 1e-3


def test_pe_delta_negative():
    d = bs_delta("PE", 100, 100, 0.20, 1.0, r=0.0, q=0.0)
    assert -1.0 < d < 0.0


def test_deep_itm_ce_delta_near_one():
    assert bs_delta("CE", 200, 100, 0.20, 0.05) > 0.99


def test_degenerate_inputs_return_nan():
    assert math.isnan(bs_delta("CE", 100, 100, 0.0, 1.0))   # sigma 0
    assert math.isnan(bs_delta("CE", 100, 100, 0.20, 0.0))  # T 0


def test_fixture_true_tuesday_expiry():
    # 2026-05-11 09:45, spot 23866, CE 24300 iv 22.45%, true expiry Tue 2026-05-12 15:30
    T = 1785 / 525600.0
    d = bs_delta("CE", 23866, 24300, 0.2245, T, r=0.065, q=0.0)
    assert abs(d - 0.088) < 0.005


def test_fixture_wrong_thursday_expiry_reproduces_doc():
    # Same option, wrong Thursday T -> reproduces the doc's buggy 0.206
    T = 4665 / 525600.0
    d = bs_delta("CE", 23866, 24300, 0.2245, T, r=0.065, q=0.0)
    assert abs(d - 0.206) < 0.01
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_black_scholes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.black_scholes'`

- [ ] **Step 3: Write minimal implementation**

```python
# engine/black_scholes.py
"""Black-Scholes greeks (stdlib only — no scipy).

Delta from the option's own IV, used for iron-condor leg selection.
sigma is decimal (iv/100); T is in years (minutes_to_expiry / 525_600).
"""
import math


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(option_type: str, S: float, K: float, sigma: float,
             T: float, r: float = 0.065, q: float = 0.0) -> float:
    """Black-Scholes delta. Returns NaN for degenerate inputs.

    CE: e^(-qT) * N(d1);  PE: e^(-qT) * (N(d1) - 1).
    """
    if not (S > 0 and K > 0 and sigma > 0 and T > 0):
        return float("nan")
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    disc = math.exp(-q * T)
    if option_type == "CE":
        return disc * _norm_cdf(d1)
    return disc * (_norm_cdf(d1) - 1.0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_black_scholes.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Checkpoint** — surface for Harsh (do NOT run git):

```
feat: add Black-Scholes delta helper (stdlib, no scipy)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## Task 2: Historical volatility (HV_20d)

**Files:**
- Create: `engine/historical_vol.py`
- Test: `tests/test_historical_vol.py`

**Interfaces:**
- Produces: `compute_hv20(spot_path: str, lookback: int = 20, annualize: int = 252) -> dict[date, float]` — `{trading_date: hv_percent}`; NaN during warm-up; HV[D] uses returns through **D−1**.
- Produces: `_hv20_from_daily_close(daily_close: pd.Series, lookback, annualize) -> pd.Series` — testable core (index = date).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_historical_vol.py
"""Tests for HV_20d computation."""
import math
import numpy as np
import pandas as pd
from datetime import date
from engine.historical_vol import _hv20_from_daily_close


def _mk_close(n):
    idx = [date(2021, 1, 1) + pd.Timedelta(days=i) for i in range(n)]
    # deterministic gently-trending closes
    vals = [100.0 * (1.01 ** i) for i in range(n)]
    return pd.Series(vals, index=idx)


def test_warmup_is_nan():
    hv = _hv20_from_daily_close(_mk_close(25), lookback=20, annualize=252)
    # first 20 entries have no full 20-return window ending at D-1
    assert hv.iloc[:20].isna().all()
    assert not math.isnan(hv.iloc[21])


def test_no_lookahead_hv_independent_of_same_day_close():
    base = _mk_close(30)
    hv_a = _hv20_from_daily_close(base, 20, 252)
    bumped = base.copy()
    bumped.iloc[25] *= 1.5  # perturb close at D=index 25
    hv_b = _hv20_from_daily_close(bumped, 20, 252)
    # HV at D=25 must NOT change (it only uses returns through D-1)
    assert abs(hv_a.iloc[25] - hv_b.iloc[25]) < 1e-9


def test_hand_computed_value():
    # constant 1% daily growth -> all log returns equal -> stdev 0 -> hv 0
    hv = _hv20_from_daily_close(_mk_close(30), 20, 252)
    assert abs(hv.iloc[25]) < 1e-6
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_historical_vol.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# engine/historical_vol.py
"""HV_20d from NIFTY daily closes.

sigma = stdev(last 20 daily log-returns) ; hv = sigma * sqrt(252) * 100.
No look-ahead: HV for day D uses the 20 returns ending at D-1.
"""
import numpy as np
import pandas as pd


def _hv20_from_daily_close(daily_close: pd.Series, lookback: int = 20,
                           annualize: int = 252) -> pd.Series:
    log_ret = np.log(daily_close / daily_close.shift(1))
    sigma = log_ret.rolling(lookback).std(ddof=1).shift(1)  # shift -> exclude day D's return
    return sigma * np.sqrt(annualize) * 100.0


def compute_hv20(spot_path: str, lookback: int = 20,
                 annualize: int = 252) -> dict:
    df = pd.read_parquet(spot_path, columns=["datetime", "close"])
    dt = pd.to_datetime(df["datetime"].str.slice(0, 19))  # naive IST
    df = df.assign(_date=dt.dt.date)
    daily_close = df.groupby("_date")["close"].last().sort_index()
    hv = _hv20_from_daily_close(daily_close, lookback, annualize)
    return hv.to_dict()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_historical_vol.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Smoke-check on real data**

Run:
```bash
python -c "from engine.historical_vol import compute_hv20; h=compute_hv20('data/spot/nifty/NIFTY_1m.parquet'); import datetime as dt; print('2026-05-11 HV=', round(h[dt.date(2026,5,11)],2), '| non-nan days:', sum(1 for v in h.values() if v==v))"
```
Expected: a plausible HV (~8–20) and thousands of non-NaN days.

- [ ] **Step 6: Checkpoint**

```
feat: add HV_20d (20d annualized realized vol, no look-ahead)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## Task 3: Weekly expiry-calendar builder + config extension

**Files:**
- Create: `scripts/build_weekly_expiry_calendar.py`
- Modify: `config.py` (insert 2020–2024 dates into `NIFTY_WEEKLY_EXPIRY_DATES`, ~line 114)
- Test: `tests/test_iv_hv_iron_condor.py` (calendar section)

**Interfaces:**
- Produces: `derive_weekly_expiries(trading_days: list[date], switch: date = date(2025,9,1)) -> list[date]` — Thursday before `switch`, Tuesday on/after; holiday-rolled to previous trading day.
- Produces: `trading_days_from_parquet(path: str) -> list[date]`.
- Produces: `validate_expiry(options_path, expiry: date) -> dict` — code-1 ATM straddle collapse check.

- [ ] **Step 1: Write the failing test (derivation logic)**

```python
# tests/test_iv_hv_iron_condor.py  (create the file with this first)
"""Tests for the IV/HV iron-condor engine."""
from datetime import date, timedelta
from scripts.build_weekly_expiry_calendar import derive_weekly_expiries


def _weekdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_thursday_regime_picks_thursdays():
    days = _weekdays(date(2021, 6, 1), date(2021, 6, 30))
    exp = derive_weekly_expiries(days)
    # June 2021 Thursdays
    assert date(2021, 6, 3) in exp
    assert date(2021, 6, 10) in exp
    assert date(2021, 6, 24) in exp
    assert all(e.weekday() == 3 for e in exp)  # all Thursdays


def test_tuesday_regime_after_switch():
    days = _weekdays(date(2026, 5, 1), date(2026, 5, 31))
    exp = derive_weekly_expiries(days)
    assert date(2026, 5, 12) in exp
    assert date(2026, 5, 19) in exp
    assert all(e.weekday() == 1 for e in exp)  # all Tuesdays


def test_holiday_rolls_back_to_previous_trading_day():
    days = _weekdays(date(2021, 6, 1), date(2021, 6, 30))
    days.remove(date(2021, 6, 10))  # simulate Thu holiday
    exp = derive_weekly_expiries(days)
    assert date(2021, 6, 9) in exp   # rolled to Wednesday
    assert date(2021, 6, 10) not in exp
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.build_weekly_expiry_calendar'`

- [ ] **Step 3: Write the builder**

```python
# scripts/build_weekly_expiry_calendar.py
"""Derive true NIFTY weekly expiry dates from the option/spot data.

Rule: Thursday before 2025-09-01, Tuesday on/after (SEBI switch). If the target
weekday is a market holiday (not a trading day in the data), roll to the previous
trading day. Validate each against the option file (code-1 ATM straddle collapse).

Usage:
    python scripts/build_weekly_expiry_calendar.py            # print date(...) lines + report
"""
import sys
from datetime import date, datetime, timedelta

import pandas as pd

SWITCH = date(2025, 9, 1)  # Thursday(3) before, Tuesday(1) on/after
SPOT_PATH = "data/spot/nifty/NIFTY_1m.parquet"
OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"


def trading_days_from_parquet(path: str = SPOT_PATH) -> list:
    df = pd.read_parquet(path, columns=["datetime"])
    d = pd.to_datetime(df["datetime"].str.slice(0, 10), format="%Y-%m-%d")
    return sorted(set(d.dt.date))


def derive_weekly_expiries(trading_days: list, switch: date = SWITCH) -> list:
    trading = set(trading_days)
    first, last = trading_days[0], trading_days[-1]
    out = set()
    d = first
    while d <= last:
        target_wd = 3 if d < switch else 1  # Thu / Tue
        if d.weekday() == target_wd:
            e = d
            while e >= first and e not in trading:
                e -= timedelta(days=1)
            if e in trading:
                out.add(e)
        d += timedelta(days=1)
    return sorted(out)


def validate_expiry(options_path: str, expiry: date) -> dict:
    """Confirm code-1 ATM straddle collapses on `expiry` vs the next trading day."""
    lo = pd.Timestamp(expiry) - pd.Timedelta(days=1)
    hi = pd.Timestamp(expiry) + pd.Timedelta(days=5)
    df = pd.read_parquet(options_path,
        columns=["datetime", "option_type", "expiry_type", "expiry_code",
                 "strike_offset", "close"],
        filters=[("expiry_type", "==", "WEEK"), ("expiry_code", "==", 1),
                 ("strike_offset", "==", 0)])
    dt = pd.to_datetime(df["datetime"].str.slice(0, 19))
    df = df.assign(_dt=dt, _date=dt.dt.date)
    df = df[(df["_date"] >= lo.date()) & (df["_date"] <= hi.date())]
    straddle = {}
    for dd, g in df.groupby("_date"):
        last = g[g["_dt"] == g["_dt"].max()]
        straddle[dd] = float(last["close"].sum())
    days = sorted(straddle)
    if expiry not in straddle:
        return {"expiry": expiry, "ok": False, "reason": "no data"}
    nxt = [x for x in days if x > expiry]
    exp_val = straddle[expiry]
    nxt_val = straddle[nxt[0]] if nxt else None
    ok = nxt_val is None or (exp_val > 0 and nxt_val / exp_val > 1.8)
    return {"expiry": expiry, "straddle": round(exp_val, 1),
            "next_straddle": round(nxt_val, 1) if nxt_val else None, "ok": ok}


def main():
    tdays = trading_days_from_parquet()
    exp = derive_weekly_expiries(tdays)
    pre = [e for e in exp if e.year < 2025]        # missing years to add to config
    print(f"# derived {len(exp)} weekly expiries {exp[0]} .. {exp[-1]}", file=sys.stderr)
    print(f"# {len(pre)} pre-2025 dates to insert into config.NIFTY_WEEKLY_EXPIRY_DATES", file=sys.stderr)
    # validate a spread-out sample against the option file
    sample = pre[::40] + [e for e in exp if e.year >= 2025][:2]
    for s in sample:
        print("#", validate_expiry(OPTIONS_PATH, s), file=sys.stderr)
    # emit the date(...) lines for the pre-2025 block
    for e in pre:
        print(f"    date({e.year}, {e.month}, {e.day}),")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run derivation tests to verify they pass**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Generate the dates and validate against data**

Run: `python scripts/build_weekly_expiry_calendar.py > /tmp/expiry_pre2025.txt`
Expected (stderr): coverage line + validation lines all showing `'ok': True`. Inspect `/tmp/expiry_pre2025.txt` — a block of `date(2020, ...)`–`date(2024, ...)` lines, all Thursdays (holiday-rolled).
**If any `'ok': False`,** stop and investigate that week before editing config.

- [ ] **Step 6: Insert the dates into `config.py`**

In `config.py`, inside `NIFTY_WEEKLY_EXPIRY_DATES = sorted([` (line ~114), add the generated block **above** the `# --- 2025 ---` comment. Update the header comment:

```python
# ============================================
# NIFTY WEEKLY EXPIRY DATES (2020-2026)
# ============================================
# Source: derived from option/spot data, validated via code-1 straddle collapse
# (scripts/build_weekly_expiry_calendar.py). Holiday-shifted dates included.
# Thursday through 2025-08-28, Tuesday from 2025-09-02 (SEBI switch 2025-09-01).
NIFTY_WEEKLY_EXPIRY_DATES = sorted([
    # --- 2020-2024 (derived) ---
    date(2020, 8, 6),
    # ... paste the full generated block here ...
    # --- 2025 ---
    date(2025, 1, 2),
    # ... existing entries unchanged ...
```

(The list is `sorted(...)`, so exact insertion order does not matter; `_EXPIRY_SET` rebuilds automatically at line ~374.)

- [ ] **Step 7: Verify config coverage test**

Add to `tests/test_iv_hv_iron_condor.py`:

```python
def test_config_covers_backtest_period():
    import config
    wk = config.NIFTY_WEEKLY_EXPIRY_DATES
    assert min(wk) <= date(2020, 8, 10)
    assert config.get_nearest_weekly_expiry(date(2021, 6, 1)) == date(2021, 6, 3)
    assert config.get_nearest_weekly_expiry(date(2021, 6, 3)) == date(2021, 6, 3)  # same-day
    assert config.get_nearest_weekly_expiry(date(2026, 5, 11)) == date(2026, 5, 12)
```

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -v`
Expected: PASS (4 passed)

- [ ] **Step 8: Checkpoint**

```
feat: extend NIFTY weekly expiry calendar back to 2020 (data-validated)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## Task 4: Engine skeleton — DayContext, parse_config, loader

**Files:**
- Create: `engine/iv_hv_iron_condor_backtest.py`
- Test: `tests/test_iv_hv_iron_condor.py`
- Reference: `engine/st_pcr_vix_credit_spread_backtest.py:129` (`OPT_COLS`), `:613` (`load_filtered_options`), `:143` (`_norm_time`), `:202` (`DayContext`), `:715` (`parse_config`).

**Interfaces:**
- Produces: `@dataclass DayContext` with fields: `iv_rv_ratio_min=1.3, hv_lookback=20, window_start="09:45", window_end="11:30", tp_pct=0.50, sl_pct=2.00, hard_exit_time="15:10", sell_ce_delta=0.20, buy_ce_delta=0.08, sell_pe_delta=-0.20, buy_pe_delta=-0.08, min_credit_pts=0.0, max_trades_per_day=1, strike_step=50, risk_free_rate=0.065, dividend_yield=0.0, lots=4, lot_size=65`.
- Produces: `parse_config(config: dict) -> DayContext`.
- Produces: `load_options(options_path, start_date, end_date) -> pd.DataFrame` — weekly code 1, columns `["datetime","option_type","strike","strike_offset","spot","open","close","iv"]`, adds `_dt` (naive IST), `_date`, `_time` (HH:MM).

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_iv_hv_iron_condor.py
from engine.iv_hv_iron_condor_backtest import parse_config, DayContext


def test_parse_config_defaults():
    ctx = parse_config({})
    assert ctx.iv_rv_ratio_min == 1.3
    assert ctx.tp_pct == 0.50 and ctx.sl_pct == 2.00
    assert ctx.sell_ce_delta == 0.20 and ctx.buy_pe_delta == -0.08
    assert ctx.lot_size == 65 and ctx.lots == 4


def test_parse_config_overrides():
    cfg = {"signal": {"iv_rv_ratio_min": 1.5},
           "exit": {"tp_pct": 0.6, "sl_pct": 1.0, "hard_exit_time": "15:15"},
           "structure": {"sell_ce_delta": 0.25}}
    ctx = parse_config(cfg)
    assert ctx.iv_rv_ratio_min == 1.5
    assert ctx.tp_pct == 0.6 and ctx.sl_pct == 1.0
    assert ctx.hard_exit_time == "15:15"
    assert ctx.sell_ce_delta == 0.25
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -k parse_config -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write skeleton (dataclass, parse_config, loader)**

```python
# engine/iv_hv_iron_condor_backtest.py
"""IV/HV-ratio NIFTY weekly iron condor (S165). See
docs/superpowers/specs/2026-07-02-iv-hv-iron-condor-design.md.

Gross P&L only. Weekly code-1 (0DTE on expiry day). Delta computed from IV.
"""
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from engine.black_scholes import bs_delta
import config

OPT_COLS = ["datetime", "option_type", "strike", "strike_offset", "spot",
            "open", "close", "iv"]


def _norm_time(t: str) -> str:
    """Normalize H:MM / HH:MM / HH:MM:SS -> HH:MM."""
    parts = str(t).split(":")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


@dataclass
class DayContext:
    iv_rv_ratio_min: float = 1.3
    hv_lookback: int = 20
    window_start: str = "09:45"
    window_end: str = "11:30"
    tp_pct: float = 0.50
    sl_pct: float = 2.00
    hard_exit_time: str = "15:10"
    sell_ce_delta: float = 0.20
    buy_ce_delta: float = 0.08
    sell_pe_delta: float = -0.20
    buy_pe_delta: float = -0.08
    min_credit_pts: float = 0.0
    max_trades_per_day: int = 1
    strike_step: int = 50
    risk_free_rate: float = 0.065
    dividend_yield: float = 0.0
    lots: int = 4
    lot_size: int = 65


def parse_config(cfg: dict) -> DayContext:
    sig = cfg.get("signal", {})
    ent = cfg.get("entry", {})
    ex = cfg.get("exit", {})
    st = cfg.get("structure", {})
    gr = cfg.get("greeks", {})
    sz = cfg.get("sizing", {})
    d = DayContext()
    return DayContext(
        iv_rv_ratio_min=float(sig.get("iv_rv_ratio_min", d.iv_rv_ratio_min)),
        hv_lookback=int(sig.get("hv_lookback", d.hv_lookback)),
        window_start=_norm_time(ent.get("window_start", d.window_start)),
        window_end=_norm_time(ent.get("window_end", d.window_end)),
        tp_pct=float(ex.get("tp_pct", d.tp_pct)),
        sl_pct=float(ex.get("sl_pct", d.sl_pct)),
        hard_exit_time=_norm_time(ex.get("hard_exit_time", d.hard_exit_time)),
        sell_ce_delta=float(st.get("sell_ce_delta", d.sell_ce_delta)),
        buy_ce_delta=float(st.get("buy_ce_delta", d.buy_ce_delta)),
        sell_pe_delta=float(st.get("sell_pe_delta", d.sell_pe_delta)),
        buy_pe_delta=float(st.get("buy_pe_delta", d.buy_pe_delta)),
        min_credit_pts=float(st.get("min_credit_pts", d.min_credit_pts)),
        max_trades_per_day=int(st.get("max_trades_per_day", d.max_trades_per_day)),
        strike_step=int(st.get("strike_step", d.strike_step)),
        risk_free_rate=float(gr.get("risk_free_rate", d.risk_free_rate)),
        dividend_yield=float(gr.get("dividend_yield", d.dividend_yield)),
        lots=int(sz.get("lots", d.lots)),
        lot_size=int(sz.get("lot_size", d.lot_size)),
    )


def load_options(options_path: str, start_date: str, end_date: str) -> pd.DataFrame:
    df = pd.read_parquet(options_path, columns=OPT_COLS,
        filters=[("underlying", "==", "NIFTY"),
                 ("expiry_type", "==", "WEEK"), ("expiry_code", "==", 1)])
    # string-slice date filter (datetime is ISO string) — inclusive range
    d10 = df["datetime"].str.slice(0, 10)
    df = df[(d10 >= start_date) & (d10 <= end_date)].copy()
    df["_dt"] = pd.to_datetime(df["datetime"].str.slice(0, 19))  # naive IST
    df["_date"] = df["_dt"].dt.date
    df["_time"] = df["_dt"].dt.strftime("%H:%M")
    return df.sort_values("_dt").reset_index(drop=True)
```

Note: `underlying` is not in `OPT_COLS` but pushdown filters can reference columns not selected. If pyarrow errors on the `underlying` filter, drop it (the NIFTY parquet is already NIFTY-only).

- [ ] **Step 4: Run to verify tests pass**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -k parse_config -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Smoke-check the loader on a short range**

Run:
```bash
python -c "from engine.iv_hv_iron_condor_backtest import load_options; df=load_options('data/options/nifty/NIFTY_OPTIONS_1m.parquet','2026-05-11','2026-05-11'); print(df.shape, sorted(df['_time'].unique())[:3], df['strike_offset'].min(), df['strike_offset'].max())"
```
Expected: rows > 0, first times `['09:15', ...]`, offset range spanning roughly −10..+10.

- [ ] **Step 6: Checkpoint**

```
feat: iron-condor engine skeleton (DayContext, parse_config, loader)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## Task 5: Stage 1 — signal finder

**Files:**
- Modify: `engine/iv_hv_iron_condor_backtest.py`
- Test: `tests/test_iv_hv_iron_condor.py`

**Interfaces:**
- Consumes: `load_options` output, `compute_hv20` map, `DayContext`.
- Produces: `find_signals(df: pd.DataFrame, hv_map: dict, ctx: DayContext) -> pd.DataFrame` — columns `_date, _dt (signal minute), atm_iv, hv, ratio, direction`; one row per signal day (first hit).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_iv_hv_iron_condor.py
import pandas as pd
from datetime import date
from engine.iv_hv_iron_condor_backtest import find_signals, DayContext


def _atm_frame(times_ivs):
    # build ATM (offset 0) CE+PE rows for one day
    rows = []
    for t, ce_iv, pe_iv in times_ivs:
        dt = pd.Timestamp(f"2021-06-01 {t}")
        for ot, iv in [("CE", ce_iv), ("PE", pe_iv)]:
            rows.append({"_dt": dt, "_date": date(2021, 6, 1), "_time": t,
                         "strike_offset": 0, "option_type": ot, "iv": iv})
    return pd.DataFrame(rows)


def test_first_hit_per_day_and_ratio():
    df = _atm_frame([("09:45", 10, 10), ("09:46", 20, 20), ("09:47", 22, 22)])
    hv = {date(2021, 6, 1): 12.0}  # ratio at 09:46 = 20/12=1.67 (>1.3); 09:45=10/12=0.83
    sigs = find_signals(df, hv, DayContext())
    assert len(sigs) == 1
    assert sigs.iloc[0]["_time"] == "09:46"          # first minute crossing 1.3
    assert abs(sigs.iloc[0]["ratio"] - 20 / 12) < 1e-6
    assert sigs.iloc[0]["direction"] == "bearish"


def test_window_excludes_after_1130():
    df = _atm_frame([("11:31", 30, 30)])
    sigs = find_signals(df, {date(2021, 6, 1): 12.0}, DayContext())
    assert len(sigs) == 0


def test_missing_hv_no_signal():
    df = _atm_frame([("09:45", 30, 30)])
    sigs = find_signals(df, {}, DayContext())  # no HV for the date
    assert len(sigs) == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -k signals -v` (and `-k first_hit -k window -k missing_hv`)
Expected: FAIL — `cannot import name 'find_signals'`

- [ ] **Step 3: Implement `find_signals`**

```python
# add to engine/iv_hv_iron_condor_backtest.py
def find_signals(df: pd.DataFrame, hv_map: dict, ctx: DayContext) -> pd.DataFrame:
    atm = df[df["strike_offset"] == 0]
    g = (atm.groupby(["_dt", "_date", "_time"], as_index=False)
            .agg(atm_iv=("iv", "mean")))
    g["hv"] = g["_date"].map(hv_map)
    g = g[(g["_time"] >= ctx.window_start) & (g["_time"] <= ctx.window_end)]
    g = g.dropna(subset=["atm_iv", "hv"])
    g = g[(g["atm_iv"] > 0) & (g["hv"] > 0)]
    if g.empty:
        return g.assign(ratio=[], direction=[])
    g = g.assign(ratio=g["atm_iv"] / g["hv"])
    hits = g[g["ratio"] > ctx.iv_rv_ratio_min].sort_values("_dt")
    signals = hits.groupby("_date", as_index=False).first()
    signals["direction"] = "bearish"
    return signals.sort_values("_dt").reset_index(drop=True)
```

- [ ] **Step 4: Run to verify tests pass**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -k "first_hit or window or missing_hv" -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Checkpoint**

```
feat: iron-condor Stage-1 IV/HV-ratio signal finder

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## Task 6: Delta computation + leg selection

**Files:**
- Modify: `engine/iv_hv_iron_condor_backtest.py`
- Test: `tests/test_iv_hv_iron_condor.py`

**Interfaces:**
- Consumes: `bs_delta`, `config.get_nearest_weekly_expiry`, `DayContext`.
- Produces: `minutes_to_expiry(signal_dt: datetime, expiry_date) -> float` — minutes to `expiry_date` 15:30.
- Produces: `add_delta(bar: pd.DataFrame, spot: float, T: float, ctx) -> pd.DataFrame` — adds `delta` column.
- Produces: `LegFill` dataclass (`option_type, strike, strike_offset, delta, entry`).
- Produces: `select_legs(bar, spot, T, ctx) -> dict[str, LegFill]` — keys `sell_ce, buy_ce, sell_pe, buy_pe`; `None` for any leg with no valid delta rows.
- Produces: `net_credit_pts(legs) -> float`.

- [ ] **Step 1: Write the failing test (reproduces the spec §11 fixture)**

```python
# add to tests/test_iv_hv_iron_condor.py
from engine.iv_hv_iron_condor_backtest import (
    load_options, minutes_to_expiry, add_delta, select_legs, net_credit_pts)
from datetime import datetime


def test_fixture_leg_selection_2026_05_11():
    df = load_options("data/options/nifty/NIFTY_OPTIONS_1m.parquet",
                      "2026-05-11", "2026-05-11")
    bar = df[df["_time"] == "09:45"]
    spot = float(bar["spot"].iloc[0])
    T = minutes_to_expiry(datetime(2026, 5, 11, 9, 45), date(2026, 5, 12)) / 525600.0
    bar = add_delta(bar, spot, T, DayContext())
    legs = select_legs(bar, spot, T, DayContext())
    assert legs["sell_ce"].strike == 24150
    assert legs["buy_ce"].strike == 24300
    assert legs["sell_pe"].strike == 23650
    assert legs["buy_pe"].strike == 23450
    assert abs(net_credit_pts(legs) - 48.10) < 0.05
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -k fixture_leg -v`
Expected: FAIL — import error / attribute missing

- [ ] **Step 3: Implement delta + leg selection**

```python
# add to engine/iv_hv_iron_condor_backtest.py
from dataclasses import dataclass as _dc

@_dc
class LegFill:
    option_type: str
    strike: float
    strike_offset: int
    delta: float
    entry: float


def minutes_to_expiry(signal_dt: datetime, expiry_date) -> float:
    expiry_dt = datetime(expiry_date.year, expiry_date.month, expiry_date.day, 15, 30)
    return (expiry_dt - signal_dt).total_seconds() / 60.0


def add_delta(bar: pd.DataFrame, spot: float, T: float, ctx: DayContext) -> pd.DataFrame:
    bar = bar.copy()
    bar["delta"] = [
        bs_delta(ot, spot, k, iv / 100.0, T, ctx.risk_free_rate, ctx.dividend_yield)
        for ot, k, iv in zip(bar["option_type"], bar["strike"], bar["iv"])
    ]
    return bar


def _nearest(bar: pd.DataFrame, option_type: str, target: float):
    c = bar[(bar["option_type"] == option_type) & bar["delta"].notna()]
    if c.empty:
        return None
    row = c.loc[(c["delta"] - target).abs().idxmin()]
    return LegFill(option_type, float(row["strike"]), int(row["strike_offset"]),
                   float(row["delta"]), float(row["close"]))


def select_legs(bar: pd.DataFrame, spot: float, T: float, ctx: DayContext) -> dict:
    bar = add_delta(bar, spot, T, ctx) if "delta" not in bar else bar
    return {
        "sell_ce": _nearest(bar, "CE", ctx.sell_ce_delta),
        "buy_ce":  _nearest(bar, "CE", ctx.buy_ce_delta),
        "sell_pe": _nearest(bar, "PE", ctx.sell_pe_delta),
        "buy_pe":  _nearest(bar, "PE", ctx.buy_pe_delta),
    }


def net_credit_pts(legs: dict) -> float:
    net = (legs["sell_ce"].entry + legs["sell_pe"].entry
           - legs["buy_ce"].entry - legs["buy_pe"].entry)
    return abs(net)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -k fixture_leg -v`
Expected: PASS (1 passed) — strikes 24150/24300/23650/23450, credit ≈48.10

- [ ] **Step 5: Checkpoint**

```
feat: iron-condor delta computation + delta-nearest leg selection

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## Task 7: Stage 2 — trade simulation (leg maps, TP/SL/TIME, sizing)

**Files:**
- Modify: `engine/iv_hv_iron_condor_backtest.py`
- Test: `tests/test_iv_hv_iron_condor.py`
- Reference: `engine/st_pcr_vix_credit_spread_backtest.py:392` (`_leg_maps`), `:508` (`run_one_day`), `:504` (`_settle`).

**Interfaces:**
- Consumes: `select_legs`, `net_credit_pts`, `DayContext`.
- Produces: `simulate_trade(day_df, legs, credit, entry_dt, ctx) -> dict` — running P&L on each minute close after entry to `hard_exit_time`; keys: `exit_time, exit_reason ("TP"/"SL"/"TIME"), pnl_pts, pnl_inr, exit_prices{leg:price}`.
- Produces module-level: `LOT_SIZE`, `LOTS` are read from `ctx`.

- [ ] **Step 1: Write the failing test (constructed price path)**

```python
# add to tests/test_iv_hv_iron_condor.py
from engine.iv_hv_iron_condor_backtest import simulate_trade, LegFill


def _mk_leg(ot, strike, off, entry):
    return LegFill(ot, strike, off, 0.2, entry)


def _day_df_from_paths(paths):
    # paths: {(option_type, strike): {time: close}}
    rows = []
    for (ot, strike), series in paths.items():
        for t, close in series.items():
            rows.append({"_time": t, "option_type": ot, "strike": strike, "close": close})
    return pd.DataFrame(rows)


def test_tp_hit():
    legs = {"sell_ce": _mk_leg("CE", 100, 2, 20), "buy_ce": _mk_leg("CE", 200, 4, 5),
            "sell_pe": _mk_leg("PE", 100, -2, 20), "buy_pe": _mk_leg("PE", 50, -4, 5)}
    # credit = |(20+20)-(5+5)| = 30 ; tp = +15
    # at 09:47 shorts decay to 5 each, longs to 3 each:
    # pnl = (20-5)+(20-5)+(3-5)+(3-5) = 30-4 = 26 >= 15 -> TP
    paths = {("CE", 100): {"09:46": 20, "09:47": 5}, ("CE", 200): {"09:46": 5, "09:47": 3},
             ("PE", 100): {"09:46": 20, "09:47": 5}, ("PE", 50): {"09:46": 5, "09:47": 3}}
    df = _day_df_from_paths(paths)
    ctx = DayContext()
    r = simulate_trade(df, legs, 30.0, datetime(2021, 6, 1, 9, 45), ctx)
    assert r["exit_reason"] == "TP"
    assert r["exit_time"] == "09:47"
    assert r["pnl_inr"] == r["pnl_pts"] * 65 * 4


def test_time_exit_when_flat():
    legs = {"sell_ce": _mk_leg("CE", 100, 2, 20), "buy_ce": _mk_leg("CE", 200, 4, 5),
            "sell_pe": _mk_leg("PE", 100, -2, 20), "buy_pe": _mk_leg("PE", 50, -4, 5)}
    # prices unchanged all day -> pnl ~0 -> TIME exit at 15:10
    times = ["09:46", "12:00", "15:10"]
    paths = {("CE", 100): {t: 20 for t in times}, ("CE", 200): {t: 5 for t in times},
             ("PE", 100): {t: 20 for t in times}, ("PE", 50): {t: 5 for t in times}}
    df = _day_df_from_paths(paths)
    r = simulate_trade(df, legs, 30.0, datetime(2021, 6, 1, 9, 45), DayContext())
    assert r["exit_reason"] == "TIME"
    assert r["exit_time"] == "15:10"
    assert abs(r["pnl_pts"]) < 1e-6
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -k "tp_hit or time_exit" -v`
Expected: FAIL — `cannot import name 'simulate_trade'`

- [ ] **Step 3: Implement `simulate_trade`**

```python
# add to engine/iv_hv_iron_condor_backtest.py
def _leg_price_maps(day_df: pd.DataFrame, legs: dict) -> dict:
    """{leg_key: {time: close}} for the 4 locked contracts."""
    maps = {}
    for key, leg in legs.items():
        sub = day_df[(day_df["option_type"] == leg.option_type)
                     & (day_df["strike"] == leg.strike)]
        maps[key] = dict(zip(sub["_time"], sub["close"]))
    return maps


def _running_pnl_pts(legs: dict, cur: dict) -> float:
    pnl = 0.0
    for key, leg in legs.items():
        c = cur[key]
        if key.startswith("sell"):
            pnl += leg.entry - c
        else:
            pnl += c - leg.entry
    return pnl


def simulate_trade(day_df: pd.DataFrame, legs: dict, credit: float,
                   entry_dt: datetime, ctx: DayContext) -> dict:
    tp = ctx.tp_pct * credit
    sl = -ctx.sl_pct * credit
    maps = _leg_price_maps(day_df, legs)
    entry_time = entry_dt.strftime("%H:%M")
    # minutes strictly after entry, up to hard exit, in chronological order
    times = sorted({t for m in maps.values() for t in m}
                   .intersection(day_df["_time"]))
    last_valid = {k: legs[k].entry for k in legs}
    exit_reason, exit_time, exit_prices = None, None, None
    for t in times:
        if t <= entry_time or t > ctx.hard_exit_time:
            continue
        cur = {}
        for k in legs:
            if t in maps[k]:
                last_valid[k] = maps[k][t]
            cur[k] = last_valid[k]
        pnl = _running_pnl_pts(legs, cur)
        is_time = (t == ctx.hard_exit_time)
        if pnl >= tp:
            exit_reason, exit_time, exit_prices = "TP", t, dict(cur); break
        if pnl <= sl:
            exit_reason, exit_time, exit_prices = "SL", t, dict(cur); break
        if is_time:
            exit_reason, exit_time, exit_prices = "TIME", t, dict(cur); break
    if exit_reason is None:  # no bar reached hard_exit_time — settle on last seen
        exit_reason, exit_time, exit_prices = "TIME", (times[-1] if times else entry_time), dict(last_valid)
    pnl_pts = _running_pnl_pts(legs, exit_prices)
    return {"exit_time": exit_time, "exit_reason": exit_reason,
            "pnl_pts": pnl_pts, "pnl_inr": pnl_pts * ctx.lot_size * ctx.lots,
            "exit_prices": exit_prices}
```

- [ ] **Step 4: Run to verify tests pass**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -k "tp_hit or time_exit" -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Checkpoint**

```
feat: iron-condor Stage-2 per-minute trade simulation (TP/SL/TIME)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## Task 8: Reporter, sanity flag, and the day-loop driver

**Files:**
- Modify: `engine/iv_hv_iron_condor_backtest.py`
- Test: `tests/test_iv_hv_iron_condor.py`
- Reference: `engine/st_pcr_vix_credit_spread_backtest.py:880` (`summarize_metrics`), `:916` (`trades_to_dataframe`), `:842` (`build_equity_curve`).

**Interfaces:**
- Consumes: everything above.
- Produces: `sanity_flag(legs, pnl_pts) -> bool` — `abs(pnl_pts) > max(CE_width, PE_width)`.
- Produces: `run_backtest(options_df, hv_map, ctx) -> list[dict]` — iterates signal days, selects legs, simulates, returns trade dicts (flattened legs + credit/tp/sl/pnl/exit/sanity + running_equity), honoring no-overlap (`signal_dt > prev_exit_dt`).
- Produces: `trades_to_dataframe(trades) -> pd.DataFrame`, `summarize_metrics(trades) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_iv_hv_iron_condor.py
from engine.iv_hv_iron_condor_backtest import sanity_flag, summarize_metrics


def test_sanity_flag_bounds_by_width():
    legs = {"sell_ce": _mk_leg("CE", 24150, 6, 37.95), "buy_ce": _mk_leg("CE", 24300, 9, 17.80),
            "sell_pe": _mk_leg("PE", 23650, -4, 41.35), "buy_pe": _mk_leg("PE", 23450, -8, 13.40)}
    # CE width 150, PE width 200 -> max 200
    assert sanity_flag(legs, 500.0) is True     # impossible -> flagged
    assert sanity_flag(legs, 24.0) is False      # normal TP-sized move


def test_summarize_metrics_basic():
    trades = [{"pnl_inr": 100.0, "exit_reason": "TP"},
              {"pnl_inr": -50.0, "exit_reason": "SL"},
              {"pnl_inr": 0.0, "exit_reason": "TIME"}]
    s = summarize_metrics(trades)
    assert s["total_trades"] == 3
    assert s["wins"] == 1 and s["losses"] == 1
    assert abs(s["total_pnl_inr"] - 50.0) < 1e-6
    assert s["exit_reason_counts"] == {"TP": 1, "SL": 1, "TIME": 1}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -k "sanity or summarize" -v`
Expected: FAIL — import error

- [ ] **Step 3: Implement sanity flag, day loop, reporter**

```python
# add to engine/iv_hv_iron_condor_backtest.py
from engine.historical_vol import compute_hv20


def sanity_flag(legs: dict, pnl_pts: float) -> bool:
    ce_w = abs(legs["buy_ce"].strike - legs["sell_ce"].strike)
    pe_w = abs(legs["sell_pe"].strike - legs["buy_pe"].strike)
    return abs(pnl_pts) > max(ce_w, pe_w)


def run_backtest(options_df: pd.DataFrame, hv_map: dict, ctx: DayContext) -> list:
    signals = find_signals(options_df, hv_map, ctx)
    by_day = {d: g for d, g in options_df.groupby("_date")}
    trades, equity, prev_exit_dt = [], 0.0, None
    for _, sig in signals.iterrows():
        sig_dt = sig["_dt"].to_pydatetime()
        if prev_exit_dt is not None and sig_dt <= prev_exit_dt:
            continue  # no overlapping trades
        day_df = by_day[sig["_date"]]
        bar = day_df[day_df["_dt"] == sig["_dt"]]
        spot = float(bar["spot"].iloc[0])
        expiry = config.get_nearest_weekly_expiry(sig["_date"])
        T = minutes_to_expiry(sig_dt, expiry) / 525600.0
        legs = select_legs(bar, spot, T, ctx)
        if any(v is None for v in legs.values()):
            continue
        credit = net_credit_pts(legs)
        if credit < ctx.min_credit_pts:
            continue
        res = simulate_trade(day_df, legs, credit, sig_dt, ctx)
        equity += res["pnl_inr"]
        exit_dt = datetime.combine(sig["_date"],
                    datetime.strptime(res["exit_time"], "%H:%M").time())
        prev_exit_dt = exit_dt
        row = {"date": str(sig["_date"]), "signal_time": sig["_time"],
               "direction": sig["direction"], "atm_iv": round(sig["atm_iv"], 3),
               "hv_20d": round(sig["hv"], 3), "ratio": round(sig["ratio"], 3),
               "spot": spot, "expiry": str(expiry),
               "net_credit_pts": round(credit, 2),
               "tp_pts": round(ctx.tp_pct * credit, 2),
               "sl_pts": round(-ctx.sl_pct * credit, 2),
               "exit_time": res["exit_time"], "exit_reason": res["exit_reason"],
               "pnl_pts": round(res["pnl_pts"], 2), "pnl_inr": round(res["pnl_inr"], 2),
               "running_equity_inr": round(equity, 2),
               "sanity_flag": sanity_flag(legs, res["pnl_pts"])}
        for k, leg in legs.items():
            row[f"{k}_strike"] = leg.strike
            row[f"{k}_offset"] = leg.strike_offset
            row[f"{k}_delta"] = round(leg.delta, 3)
            row[f"{k}_entry"] = leg.entry
            row[f"{k}_exit"] = round(res["exit_prices"][k], 2)
        trades.append(row)
    return trades


def trades_to_dataframe(trades: list) -> pd.DataFrame:
    return pd.DataFrame(trades)


def summarize_metrics(trades: list) -> dict:
    n = len(trades)
    pnls = [t["pnl_inr"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    counts = {}
    for t in trades:
        counts[t["exit_reason"]] = counts.get(t["exit_reason"], 0) + 1
    eq, peak, mdd = 0.0, 0.0, 0.0
    for p in pnls:
        eq += p; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    flagged = sum(1 for t in trades if t.get("sanity_flag"))
    clean = [t["pnl_inr"] for t in trades if not t.get("sanity_flag")]
    return {"total_trades": n, "wins": wins, "losses": losses,
            "win_rate": round(wins / n, 4) if n else 0.0,
            "total_pnl_inr": round(sum(pnls), 2),
            "mean_pnl_inr": round(sum(pnls) / n, 2) if n else 0.0,
            "max_drawdown_inr": round(mdd, 2),
            "best_trade_inr": round(max(pnls), 2) if pnls else 0.0,
            "worst_trade_inr": round(min(pnls), 2) if pnls else 0.0,
            "exit_reason_counts": counts,
            "sanity_flagged": flagged,
            "total_pnl_inr_sanity_filtered": round(sum(clean), 2)}
```

- [ ] **Step 4: Run to verify tests pass**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py -k "sanity or summarize" -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the full engine test file**

Run: `python -m pytest tests/test_iv_hv_iron_condor.py tests/test_black_scholes.py tests/test_historical_vol.py -v`
Expected: all PASS.

- [ ] **Step 6: Checkpoint**

```
feat: iron-condor day-loop driver, reporter, and bad-tick sanity flag

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## Task 9: CLI + saved config JSON

**Files:**
- Create: `run_iv_hv_condor.py`
- Create: `saved_strategies/iv_hv_iron_condor.json`
- Reference: `run_sensex_dual_short.py` (argparse + write CSV + print).

**Interfaces:**
- Consumes: `load_options`, `compute_hv20`, `parse_config`, `run_backtest`, `trades_to_dataframe`, `summarize_metrics`.

- [ ] **Step 1: Write the saved config JSON**

```json
{
  "strategy_name": "IV/HV-Ratio Iron Condor (S165)",
  "description": "Sell NIFTY weekly iron condor when ATM_IV/HV_20d > 1.3; delta-picked legs; TP 50% / SL 200% of credit; 15:10 hard exit. Gross P&L.",
  "backtest_start": "2020-08-03",
  "backtest_end": "2026-05-22",
  "signal":    { "iv_rv_ratio_min": 1.3, "hv_lookback": 20 },
  "entry":     { "window_start": "09:45", "window_end": "11:30", "fill": "signal_close" },
  "exit":      { "tp_pct": 0.50, "sl_pct": 2.00, "hard_exit_time": "15:10" },
  "structure": { "sell_ce_delta": 0.20, "buy_ce_delta": 0.08,
                 "sell_pe_delta": -0.20, "buy_pe_delta": -0.08,
                 "min_credit_pts": 0.0, "max_trades_per_day": 1, "strike_step": 50 },
  "expiry":    { "expiry_type": "WEEK", "expiry_code": 1 },
  "greeks":    { "risk_free_rate": 0.065, "dividend_yield": 0.0 },
  "sizing":    { "lots": 4, "lot_size": 65 }
}
```

- [ ] **Step 2: Write the CLI**

```python
# run_iv_hv_condor.py
"""Run the IV/HV-ratio iron condor backtest.

    python run_iv_hv_condor.py --start 2020-08-03 --end 2026-05-22 --out condor_trades.csv
"""
import argparse
import json

from engine.iv_hv_iron_condor_backtest import (
    load_options, parse_config, run_backtest, trades_to_dataframe, summarize_metrics)
from engine.historical_vol import compute_hv20

OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"
SPOT_PATH = "data/spot/nifty/NIFTY_1m.parquet"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-08-03")
    ap.add_argument("--end", default="2026-05-22")
    ap.add_argument("--config", default="saved_strategies/iv_hv_iron_condor.json")
    ap.add_argument("--out", default="iv_hv_condor_trades.csv")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    ctx = parse_config(cfg)
    print(f"Loading options {args.start}..{args.end} ...")
    df = load_options(OPTIONS_PATH, args.start, args.end)
    print(f"  {len(df):,} rows, {df['_date'].nunique()} trading days")
    hv = compute_hv20(SPOT_PATH, ctx.hv_lookback)
    trades = run_backtest(df, hv, ctx)
    tdf = trades_to_dataframe(trades)
    tdf.to_csv(args.out, index=False)
    s = summarize_metrics(trades)
    print(f"\nTrades: {s['total_trades']}  Win%: {s['win_rate']*100:.1f}  "
          f"Total P&L: Rs {s['total_pnl_inr']:,.0f}  MaxDD: Rs {s['max_drawdown_inr']:,.0f}")
    print(f"Exit mix: {s['exit_reason_counts']}")
    print(f"Sanity-flagged: {s['sanity_flagged']}  "
          f"(filtered P&L: Rs {s['total_pnl_inr_sanity_filtered']:,.0f})")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Smoke-run on one month**

Run: `python run_iv_hv_condor.py --start 2026-05-01 --end 2026-05-22 --out /tmp/condor_smoke.csv`
Expected: prints trade count / win% / P&L without error; `/tmp/condor_smoke.csv` has rows with the per-leg columns.

- [ ] **Step 4: Checkpoint**

```
feat: iron-condor CLI runner + saved strategy config

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## Task 10: Full-period run + verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full backtest**

Run: `python run_iv_hv_condor.py --start 2020-08-03 --end 2026-05-22 --out iv_hv_condor_trades.csv`
Expected: completes; prints headline metrics; writes the CSV. (May take a few minutes loading the option chain — acceptable.)

- [ ] **Step 2: Verify the fixture trade appears correctly**

Run:
```bash
python -c "import pandas as pd; df=pd.read_csv('iv_hv_condor_trades.csv'); r=df[df['date']=='2026-05-11']; print(r[['signal_time','ratio','sell_ce_strike','buy_ce_strike','sell_pe_strike','buy_pe_strike','net_credit_pts','exit_reason','pnl_inr','sanity_flag']].to_string())"
```
Expected: signal 09:45, legs 24150/24300/23650/23450, credit ≈48.10, a plausible exit and P&L, `sanity_flag=False`.

- [ ] **Step 3: Sanity-review the aggregate**

Confirm: signals fire mostly at 09:45; win rate is plausible for a credit condor (60–85%); exit mix has TIME/TP/SL all present; `sanity_flagged` count is small (report it — do not drop). Note any surprises for Harsh.

- [ ] **Step 4: Verification-before-completion**

Invoke `superpowers:verification-before-completion` — confirm all tests pass and the full run produced the expected artifacts before declaring done.

Run: `python -m pytest tests/test_black_scholes.py tests/test_historical_vol.py tests/test_iv_hv_iron_condor.py -v`
Expected: all PASS.

- [ ] **Step 5: Final checkpoint**

```
feat: full-period IV/HV iron-condor backtest run + verification

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## Self-Review notes

- **Spec coverage:** §4 expiry → Task 3; §5 HV → Task 2; §6 delta → Task 1 + Task 6; §7 signal → Task 5; §8 trade engine → Task 6/7; §9 reporter+sanity → Task 8; §10 config → Task 4/9; §11 fixture → Task 6 test + Task 10 verify; §12 tests → each task; §13 deviations honored (gross P&L, close fills, computed delta). ✅
- **No placeholders:** all steps carry runnable code/commands.
- **Type consistency:** `LegFill`, `DayContext`, `select_legs`→`simulate_trade`→`run_backtest` signatures match across tasks. `_time` is HH:MM everywhere; time comparisons are lexicographic on HH:MM.
- **Open realism note (not blocking):** entry/exit fills use the signal/exit-bar **close** per §6; a next-bar-open switch is a future config toggle.
