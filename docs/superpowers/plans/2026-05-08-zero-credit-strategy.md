# Zero Credit Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Do not dispatch Agent subagents per task** — execute inline (per user preference).

**Goal:** Build a daily, premium-targeted, four-leg NIFTY weekly-options backtest engine ("zero credit"): every trading day at 09:20 buy 1×CE + 1×PE near ₹100, sell 2×CE + 2×PE near ₹50; exit at ₹1000 TP or 15:20 time exit.

**Architecture:** New `engine/zero_credit_backtest.py` mirroring the structure of `engine/debit_spread_backtest.py`, with one novel piece — `pick_strike_by_premium()` — replacing offset-based strike resolution. New `ui/zero_credit_backtest_runner.py` wired into `app.py` as a Streamlit tab. Tests in `tests/test_zero_credit.py`.

**Tech Stack:** Python 3, pandas, pytest, Streamlit. Data: `data/options/nifty/NIFTY_OPTIONS_1m.parquet` (columns: `ts, datetime, underlying, option_type, expiry_type, expiry_code, atm_strike, strike_offset, moneyness, strike, spot, open, high, low, close, volume, oi, iv`). Datetime is ISO-8601 with `+05:30` offset.

**Spec:** `docs/superpowers/specs/2026-05-08-zero-credit-strategy-design.md`

---

## File Structure

**New files:**
- `engine/zero_credit_backtest.py` — engine (~450 lines).
- `saved_strategies/zero_credit.json` — config defaults.
- `tests/test_zero_credit.py` — full test suite.
- `ui/zero_credit_backtest_runner.py` — Streamlit form + runner.

**Modified files:**
- `app.py` — add a `tab_zero_credit` and import the runner.

**Untouched (reused):** `engine/data_loader.py`, `engine/reporter.py`, `config.py` (already has `LOT_SIZE['NIFTY']=65`).

---

## Conventions used throughout the plan

- Sign convention: `+1` for BUY, `−1` for SELL. So `signed_sum(prices) = total_paid_for_longs − total_received_from_shorts`.
- Lot size: `LOT_SIZE_NIFTY = 65` is hard-coded as a module constant (mirrors `debit_spread`).
- All datetime comparisons use ISO-8601 strings of the form `"YYYY-MM-DDTHH:MM:SS+05:30"`. The parquet stores `datetime` as a tz-aware datetime64 column; the engine normalizes it to ISO strings once at load time.
- Leg keys: `ce_long`, `pe_long`, `ce_short`, `pe_short`.
- Working directory for `pytest` is the project root: `/Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy`.

---

### Task 1: Create the saved strategy config

**Files:**
- Create: `saved_strategies/zero_credit.json`

- [ ] **Step 1: Create the config file**

```json
{
  "name": "zero_credit",
  "strategy_type": "zero_credit",
  "instruments": ["NIFTY"],
  "entry": {
    "entry_time": "09:20"
  },
  "structure": {
    "buy_premium_target_inr":  100,
    "sell_premium_target_inr":  50,
    "buy_lots":  1,
    "sell_lots": 2,
    "premium_match_tolerance_inr": 20
  },
  "exit": {
    "tp_target_inr": 1000,
    "time_exit": "15:20",
    "data_gap_force_exit_minutes": 30
  },
  "sizing": {
    "reference_capital": 200000
  },
  "backtest_start": "2025-01-01",
  "backtest_end":   "2026-05-08"
}
```

- [ ] **Step 2: Validate JSON parses**

Run: `python3 -c "import json; print(json.load(open('saved_strategies/zero_credit.json'))['structure']['buy_premium_target_inr'])"`
Expected: `100`

- [ ] **Step 3: Commit**

Suggest the user run:
```
git add saved_strategies/zero_credit.json
git commit -m "feat(zero_credit): add default strategy config"
```

---

### Task 2: Engine module skeleton + dataclasses

**Files:**
- Create: `engine/zero_credit_backtest.py`
- Test: `tests/test_zero_credit.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_zero_credit.py`:

```python
"""Tests for zero_credit backtest engine."""
from datetime import date

import pandas as pd
import pytest

from engine.zero_credit_backtest import (
    LegSpec,
    LegFill,
    ZeroCreditTrade,
    LOT_SIZE_NIFTY,
)


class TestDataclasses:
    def test_legspec_holds_premium_target(self):
        spec = LegSpec(option_type="CE", side="BUY", lots=1, premium_target_inr=100.0)
        assert spec.option_type == "CE"
        assert spec.side == "BUY"
        assert spec.lots == 1
        assert spec.premium_target_inr == 100.0

    def test_legfill_records_strike_and_prices(self):
        fill = LegFill(
            option_type="CE", side="BUY", lots=1,
            premium_target_inr=100.0,
            strike=24500.0,
            entry_price=98.5,
            exit_price=120.0,
        )
        assert fill.strike == 24500.0
        assert fill.entry_price == 98.5
        assert fill.exit_price == 120.0

    def test_trade_constructs(self):
        trade = ZeroCreditTrade(
            date="2026-04-08", entry_time="09:20",
            atm_strike=24500.0, spot_at_entry=24512.5,
            net_debit_pts=0.5, net_debit_inr=32.5, tp_target_inr=1000.0,
            exit_time="11:30", exit_reason="TP",
            pnl_pts=15.5, pnl_inr=1007.5, return_pct=0.0050375,
            running_equity_inr=201007.5,
            skip_reason=None, legs={},
        )
        assert trade.exit_reason == "TP"
        assert trade.skip_reason is None

    def test_lot_size_constant(self):
        assert LOT_SIZE_NIFTY == 65
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_zero_credit.py::TestDataclasses -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.zero_credit_backtest'`

- [ ] **Step 3: Create the engine skeleton with dataclasses**

Create `engine/zero_credit_backtest.py`:

```python
"""
Zero Credit Backtest Engine — 4-leg premium-targeted NIFTY weekly options.

Strategy:
  Daily entry. At 09:20 each trading day, buy 1×CE + 1×PE at strikes whose
  09:20 open is closest to ₹100, and sell 2×CE + 2×PE at strikes whose 09:20
  open is closest to ₹50. Net premium paid ≈ 0 ("zero credit"). Exit at
  ₹1000 combined unrealized P&L (configurable, intra-day 1-min check) or
  15:20 close. No stop loss. One trade per day.
  See docs/superpowers/specs/2026-05-08-zero-credit-strategy-design.md.
"""

import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

LOT_SIZE_NIFTY = 65  # Mirrors config.LOT_SIZE['NIFTY']; pinned for unit tests.

LEG_KEY_ORDER = ["ce_long", "pe_long", "ce_short", "pe_short"]


@dataclass
class LegSpec:
    """A single leg's static spec (independent of any specific trade)."""
    option_type: str          # "CE" | "PE"
    side: str                 # "BUY" | "SELL"
    lots: int
    premium_target_inr: float


@dataclass
class LegFill:
    """Resolved leg at a specific trade: actual strike + entry/exit prices."""
    option_type: str
    side: str
    lots: int
    premium_target_inr: float
    strike: float
    entry_price: float
    exit_price: Optional[float] = None


@dataclass
class ZeroCreditTrade:
    date: str                 # ISO date string of the entry day
    entry_time: str
    atm_strike: float
    spot_at_entry: float

    net_debit_pts: float
    net_debit_inr: float
    tp_target_inr: float

    exit_time: str
    exit_reason: str          # "TP" | "TIME" | "data_gap_force_exit"
    pnl_pts: float
    pnl_inr: float
    return_pct: float
    running_equity_inr: float

    skip_reason: Optional[str]
    legs: Dict[str, LegFill] = field(default_factory=dict)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_zero_credit.py::TestDataclasses -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

Suggest the user run:
```
git add engine/zero_credit_backtest.py tests/test_zero_credit.py
git commit -m "feat(zero_credit): scaffold engine module + dataclasses"
```

---

### Task 3: Strike picker by premium (the core novelty)

**Files:**
- Modify: `engine/zero_credit_backtest.py`
- Test: `tests/test_zero_credit.py`

The picker scans candidate rows on one side (CE or PE) of the option chain at the entry minute and returns the strike whose `open` is closest to `target_premium`. Tiebreakers: closer to ATM by strike distance; then lower strike for CE / higher strike for PE.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_zero_credit.py`:

```python
from engine.zero_credit_backtest import (
    pick_strike_by_premium,
    PickResult,
)


def _option_row(strike, opt_type, open_, atm_strike=24500.0):
    """Mock one parquet row at the entry minute."""
    return {
        "datetime": "2026-04-08T09:20:00+05:30",
        "underlying": "NIFTY",
        "option_type": opt_type,
        "expiry_type": "WEEK",
        "expiry_code": 1,
        "atm_strike": atm_strike,
        "strike_offset": int(round((strike - atm_strike) / 50)),
        "moneyness": "ATM" if strike == atm_strike else ("OTM" if (
            (opt_type == "CE" and strike > atm_strike)
            or (opt_type == "PE" and strike < atm_strike)
        ) else "ITM"),
        "strike": strike,
        "spot": atm_strike,
        "open": open_,
        "high": open_,
        "low": open_,
        "close": open_,
        "volume": 1, "oi": 1, "iv": 15.0,
    }


class TestPickStrikeByPremium:
    def _slice(self, rows):
        return pd.DataFrame(rows)

    def test_picks_closest_premium_target_100(self):
        rows = [
            _option_row(24400, "CE", 140.0),
            _option_row(24450, "CE", 110.0),
            _option_row(24500, "CE",  95.0),  # Δ=5
            _option_row(24550, "CE",  80.0),  # Δ=20
            _option_row(24600, "CE",  40.0),
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="CE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert isinstance(result, PickResult)
        assert result.strike == 24500.0
        assert result.entry_price == 95.0
        assert result.skipped is False

    def test_tiebreak_closer_to_atm(self):
        # Two strikes equidistant in premium (Δ=10 each). Pick closer to ATM.
        rows = [
            _option_row(24550, "CE", 90.0, atm_strike=24500.0),   # |Δstrike|=50
            _option_row(24400, "CE", 110.0, atm_strike=24500.0),  # |Δstrike|=100
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="CE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.strike == 24550.0  # closer to ATM (50 vs 100)

    def test_tiebreak_lower_strike_for_ce_when_strike_distance_also_ties(self):
        # Both Δpremium=10 AND |Δstrike|=50. CE → pick lower strike.
        rows = [
            _option_row(24450, "CE", 110.0, atm_strike=24500.0),
            _option_row(24550, "CE",  90.0, atm_strike=24500.0),
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="CE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.strike == 24450.0

    def test_tiebreak_higher_strike_for_pe_when_strike_distance_also_ties(self):
        rows = [
            _option_row(24450, "PE",  90.0, atm_strike=24500.0),
            _option_row(24550, "PE", 110.0, atm_strike=24500.0),
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="PE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.strike == 24550.0

    def test_skip_when_no_strike_within_tolerance(self):
        rows = [
            _option_row(24400, "CE", 200.0),
            _option_row(24500, "CE", 130.0),  # Δ=30, > tolerance=20
            _option_row(24600, "CE", 40.0),   # Δ=60
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="CE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.skipped is True
        assert result.skip_reason == "no_strike_within_tolerance"

    def test_filters_to_specified_option_type(self):
        rows = [
            _option_row(24500, "CE", 95.0),
            _option_row(24500, "PE", 99.0),  # would beat the CE on PE-side query
        ]
        result = pick_strike_by_premium(
            self._slice(rows), option_type="PE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.strike == 24500.0
        assert result.option_type == "PE"
        assert result.entry_price == 99.0

    def test_empty_slice_skips(self):
        result = pick_strike_by_premium(
            pd.DataFrame(), option_type="CE",
            target_premium_inr=100.0, tolerance_inr=20.0,
            atm_strike=24500.0,
        )
        assert result.skipped is True
        assert result.skip_reason == "no_strike_within_tolerance"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_zero_credit.py::TestPickStrikeByPremium -v`
Expected: FAIL — `ImportError: cannot import name 'pick_strike_by_premium'`

- [ ] **Step 3: Implement `pick_strike_by_premium` and `PickResult`**

Append to `engine/zero_credit_backtest.py`:

```python
@dataclass
class PickResult:
    """Outcome of a single-leg strike pick."""
    skipped: bool
    skip_reason: Optional[str]
    option_type: Optional[str]
    strike: Optional[float]
    entry_price: Optional[float]


def pick_strike_by_premium(
    slice_df: pd.DataFrame,
    option_type: str,
    target_premium_inr: float,
    tolerance_inr: float,
    atm_strike: float,
) -> PickResult:
    """Pick the strike whose `open` is closest to target_premium_inr.

    Rules (in order):
      1. Filter to rows with the given option_type.
      2. Compute |open - target| for each row; smallest wins.
      3. Tiebreaker: smallest |strike - atm_strike|.
      4. Final tiebreak: lower strike for CE / higher strike for PE.
      5. If the winner's |open - target| > tolerance_inr, return skipped.
    """
    if slice_df.empty:
        return PickResult(skipped=True, skip_reason="no_strike_within_tolerance",
                          option_type=None, strike=None, entry_price=None)

    side = slice_df[slice_df["option_type"] == option_type]
    if side.empty:
        return PickResult(skipped=True, skip_reason="no_strike_within_tolerance",
                          option_type=None, strike=None, entry_price=None)

    side = side.copy()
    side["_dpremium"] = (side["open"] - target_premium_inr).abs()
    side["_dstrike"] = (side["strike"] - atm_strike).abs()

    # Sort: primary asc by |Δpremium|, secondary asc by |Δstrike|, tertiary
    # by strike (asc for CE, desc for PE → lower CE wins / higher PE wins).
    sort_strike_ascending = (option_type == "CE")
    side = side.sort_values(
        by=["_dpremium", "_dstrike", "strike"],
        ascending=[True, True, sort_strike_ascending],
    )

    winner = side.iloc[0]
    if float(winner["_dpremium"]) > tolerance_inr:
        return PickResult(skipped=True, skip_reason="no_strike_within_tolerance",
                          option_type=option_type,
                          strike=float(winner["strike"]),
                          entry_price=float(winner["open"]))

    return PickResult(
        skipped=False, skip_reason=None,
        option_type=option_type,
        strike=float(winner["strike"]),
        entry_price=float(winner["open"]),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_zero_credit.py::TestPickStrikeByPremium -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

Suggest:
```
git add engine/zero_credit_backtest.py tests/test_zero_credit.py
git commit -m "feat(zero_credit): premium-based strike picker with tiebreakers"
```

---

### Task 4: ATM resolver

**Files:**
- Modify: `engine/zero_credit_backtest.py`
- Test: `tests/test_zero_credit.py`

The ATM strike for the day is read off the `moneyness=='ATM'` row at the entry minute. If multiple ATM rows tag, pick the one with min `|strike - spot|`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_zero_credit.py`:

```python
from engine.zero_credit_backtest import resolve_atm_strike


class TestResolveAtmStrike:
    def test_single_atm_row(self):
        rows = [
            _option_row(24400, "CE", 140.0, atm_strike=24500.0),
            _option_row(24500, "CE",  95.0, atm_strike=24500.0),  # ATM-tagged
        ]
        df = pd.DataFrame(rows)
        # Force-mark one row as ATM since _option_row only marks strike==atm
        df.loc[df["strike"] == 24500, "moneyness"] = "ATM"
        atm, spot = resolve_atm_strike(df)
        assert atm == 24500.0
        assert spot == 24500.0

    def test_no_atm_returns_none(self):
        rows = [_option_row(24500, "CE", 95.0, atm_strike=24500.0)]
        df = pd.DataFrame(rows)
        df["moneyness"] = "OTM"
        atm, spot = resolve_atm_strike(df)
        assert atm is None
        assert spot is None

    def test_multiple_atm_picks_closest_to_spot(self):
        rows = [
            _option_row(24450, "CE", 110.0, atm_strike=24500.0),
            _option_row(24550, "CE",  90.0, atm_strike=24500.0),
        ]
        df = pd.DataFrame(rows)
        df["moneyness"] = "ATM"
        df["spot"] = 24512.0  # Closer to 24500 (12) than to 24550 (38).
                              # But neither is 24500 in this fixture; check 24450 vs 24550.
                              # |24450 - 24512| = 62, |24550 - 24512| = 38 → 24550 wins.
        atm, spot = resolve_atm_strike(df)
        assert atm == 24550.0
        assert spot == 24512.0

    def test_empty_returns_none(self):
        atm, spot = resolve_atm_strike(pd.DataFrame())
        assert atm is None
        assert spot is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_zero_credit.py::TestResolveAtmStrike -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_atm_strike'`

- [ ] **Step 3: Implement `resolve_atm_strike`**

Append to `engine/zero_credit_backtest.py`:

```python
def resolve_atm_strike(slice_df: pd.DataFrame) -> Tuple[Optional[float], Optional[float]]:
    """Return (atm_strike, spot) from a 1-min slice. None if no ATM row."""
    if slice_df.empty:
        return None, None
    atm_rows = slice_df[slice_df["moneyness"] == "ATM"]
    if atm_rows.empty:
        return None, None
    if len(atm_rows) > 1:
        atm_rows = atm_rows.assign(
            _abs_dist=(atm_rows["strike"] - atm_rows["spot"]).abs()
        ).sort_values("_abs_dist")
    row = atm_rows.iloc[0]
    return float(row["strike"]), float(row["spot"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_zero_credit.py::TestResolveAtmStrike -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

Suggest:
```
git add engine/zero_credit_backtest.py tests/test_zero_credit.py
git commit -m "feat(zero_credit): ATM strike resolver"
```

---

### Task 5: Entry economics, MTM, and TP scan

**Files:**
- Modify: `engine/zero_credit_backtest.py`
- Test: `tests/test_zero_credit.py`

Three small pure functions. Mirror the math of `debit_spread_backtest.py` but adapted for fixed rupee TP.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_zero_credit.py`:

```python
from engine.zero_credit_backtest import (
    compute_entry_economics,
    compute_mtm_inr,
    scan_for_tp_exit,
)


def _legs_with_prices(prices: dict):
    """Build {leg_key: LegFill} dict with the supplied entry prices and the
    canonical 1/1/2/2 lots, ₹100/₹100/₹50/₹50 targets."""
    specs = {
        "ce_long":  ("CE", "BUY",  1, 100.0),
        "pe_long":  ("PE", "BUY",  1, 100.0),
        "ce_short": ("CE", "SELL", 2,  50.0),
        "pe_short": ("PE", "SELL", 2,  50.0),
    }
    legs = {}
    strike_map = {
        "ce_long": 24500.0, "pe_long": 24500.0,
        "ce_short": 24600.0, "pe_short": 24400.0,
    }
    for k, (ot, side, lots, target) in specs.items():
        legs[k] = LegFill(
            option_type=ot, side=side, lots=lots,
            premium_target_inr=target,
            strike=strike_map[k],
            entry_price=prices[k],
        )
    return legs


class TestComputeEntryEconomics:
    def test_perfect_zero_credit(self):
        # 1×100 + 1×100 - 2×50 - 2×50 = 0
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, net_inr, _ = compute_entry_economics(
            legs, tp_target_inr_fixed=1000.0,
        )
        assert net_pts == pytest.approx(0.0)
        assert net_inr == pytest.approx(0.0)

    def test_small_debit(self):
        # 1×102 + 1×98 - 2×48 - 2×52 = 200 - 200 = 0; bump one leg.
        legs = _legs_with_prices({
            "ce_long": 105.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        # signed = +1*105 + 1*100 - 2*50 - 2*50 = 5 pts
        net_pts, net_inr, tp = compute_entry_economics(
            legs, tp_target_inr_fixed=1000.0,
        )
        assert net_pts == pytest.approx(5.0)
        assert net_inr == pytest.approx(5.0 * 65)
        assert tp == 1000.0  # configured fixed value

    def test_tp_target_is_fixed_rupee_value(self):
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        _, _, tp = compute_entry_economics(legs, tp_target_inr_fixed=2500.0)
        assert tp == 2500.0


class TestComputeMtmInr:
    def test_zero_when_prices_equal_entry(self):
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, tp_target_inr_fixed=1000.0)
        prices = {"ce_long": 100.0, "pe_long": 100.0,
                  "ce_short": 50.0, "pe_short": 50.0}
        mtm = compute_mtm_inr(legs, prices, net_pts)
        assert mtm == pytest.approx(0.0)

    def test_positive_mtm_when_shorts_decay(self):
        # Both shorts decay 10 pts each; longs unchanged. P/L = 0 - (-2*10 - 2*10)
        # signed_now = +1*100 + +1*100 + -2*40 + -2*40 = 200 - 160 = 40
        # net_debit  = 0
        # mtm_pts = 40 → mtm_inr = 40 × 65 = 2600
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, tp_target_inr_fixed=1000.0)
        prices = {"ce_long": 100.0, "pe_long": 100.0,
                  "ce_short": 40.0, "pe_short": 40.0}
        mtm = compute_mtm_inr(legs, prices, net_pts)
        assert mtm == pytest.approx(40.0 * 65)


class TestScanForTpExit:
    def test_returns_first_bar_at_or_above_tp(self):
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, tp_target_inr_fixed=1000.0)
        # mtm_pts threshold for 1000 ₹ = 1000 / 65 ≈ 15.385 pts
        bars = [
            {"datetime": pd.Timestamp("2026-04-08T09:21:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 47, "pe_short": 47}},
             # signed_now = 200 - 2*47 - 2*47 = 200 - 188 = 12 → mtm_pts=12 → 780₹ < TP
            {"datetime": pd.Timestamp("2026-04-08T09:22:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 42, "pe_short": 42}},
             # signed_now = 200 - 168 = 32 → mtm_pts=32 → 2080₹ ≥ TP → trigger
        ]
        result = scan_for_tp_exit(legs, bars, net_pts, tp_target_inr=1000.0)
        assert result is not None
        ts, prices, mtm = result
        assert ts == pd.Timestamp("2026-04-08T09:22:00+05:30")
        assert mtm == pytest.approx(32.0 * 65)

    def test_returns_none_if_tp_never_hits(self):
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, tp_target_inr_fixed=1000.0)
        bars = [
            {"datetime": pd.Timestamp("2026-04-08T09:21:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 47, "pe_short": 47}},
        ]
        result = scan_for_tp_exit(legs, bars, net_pts, tp_target_inr=1000.0)
        assert result is None

    def test_skips_bars_with_missing_legs(self):
        legs = _legs_with_prices({
            "ce_long": 100.0, "pe_long": 100.0,
            "ce_short": 50.0, "pe_short": 50.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, tp_target_inr_fixed=1000.0)
        bars = [
            {"datetime": pd.Timestamp("2026-04-08T09:21:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100}},  # missing shorts
            {"datetime": pd.Timestamp("2026-04-08T09:22:00+05:30"),
             "prices": {"ce_long": 100, "pe_long": 100, "ce_short": 42, "pe_short": 42}},
        ]
        result = scan_for_tp_exit(legs, bars, net_pts, tp_target_inr=1000.0)
        assert result is not None
        ts, _, _ = result
        assert ts == pd.Timestamp("2026-04-08T09:22:00+05:30")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_zero_credit.py -k "EntryEconomics or Mtm or ScanForTp" -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement the three functions**

Append to `engine/zero_credit_backtest.py`:

```python
def _leg_signed_value(leg: LegFill, price: float) -> float:
    """+lots*price for BUY, -lots*price for SELL."""
    sign = 1 if leg.side == "BUY" else -1
    return sign * leg.lots * price


def compute_entry_economics(
    legs: Dict[str, LegFill],
    tp_target_inr_fixed: float,
    lot_size: int = LOT_SIZE_NIFTY,
) -> Tuple[float, float, float]:
    """Return (net_debit_pts, net_debit_inr, tp_target_inr).

    Sign convention: +1 for BUY, -1 for SELL → signed_sum = total paid - total
    received = net debit. Positive = debit (cash out); negative = credit.
    TP is a fixed rupee target from config; net_debit is recorded but not used
    to size TP (unlike debit_spread).
    """
    net_debit_pts = sum(_leg_signed_value(leg, leg.entry_price) for leg in legs.values())
    net_debit_inr = net_debit_pts * lot_size
    return net_debit_pts, net_debit_inr, float(tp_target_inr_fixed)


def compute_mtm_inr(
    legs: Dict[str, LegFill],
    current_prices: Dict[str, float],
    net_debit_pts: float,
    lot_size: int = LOT_SIZE_NIFTY,
) -> float:
    """Unrealized P&L vs entry given current per-leg prices.

    With sign=+1 for BUY and -1 for SELL, signed_sum(prices) is the position
    value. Since net_debit_pts == signed_sum(entry_prices), mtm_pts =
    signed_now - net_debit_pts.
    """
    signed_now = sum(_leg_signed_value(leg, current_prices[k]) for k, leg in legs.items())
    mtm_pts = signed_now - net_debit_pts
    return mtm_pts * lot_size


def scan_for_tp_exit(
    legs: Dict[str, LegFill],
    bars: List[Dict],
    net_debit_pts: float,
    tp_target_inr: float,
    lot_size: int = LOT_SIZE_NIFTY,
) -> Optional[Tuple[pd.Timestamp, Dict[str, float], float]]:
    """Walk the bar series; return (timestamp, leg_prices, mtm_inr) when TP fires.

    Trigger when mtm_inr >= tp_target_inr. Returns None if no bar satisfies.
    Bars where any leg's price is missing are skipped (no fake MTM).
    """
    for bar in bars:
        prices = bar["prices"]
        if any(k not in prices for k in legs):
            continue
        mtm = compute_mtm_inr(legs, prices, net_debit_pts, lot_size)
        if mtm >= tp_target_inr:
            return bar["datetime"], dict(prices), mtm
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_zero_credit.py -k "EntryEconomics or Mtm or ScanForTp" -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

Suggest:
```
git add engine/zero_credit_backtest.py tests/test_zero_credit.py
git commit -m "feat(zero_credit): entry economics, MTM, and TP scan"
```

---

### Task 6: Bar stream with carry-forward

**Files:**
- Modify: `engine/zero_credit_backtest.py`
- Test: `tests/test_zero_credit.py`

Walk a holding-period DataFrame in 1-min bars, restricted to the 4 locked strikes. On any leg's gap > `max_gap_minutes`, yield the last fully-observed bar with `force_exit=True` and stop. Without that handling, mid-trade data dropouts produce stale or fake MTM.

The lookup key is `(option_type, strike)` since strikes (not offsets) are locked.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_zero_credit.py`:

```python
from engine.zero_credit_backtest import build_bar_stream


def _bar_row(ts, opt_type, strike, close):
    return {
        "datetime": ts, "underlying": "NIFTY",
        "option_type": opt_type, "expiry_code": 1, "expiry_type": "WEEK",
        "strike": strike, "atm_strike": 24500.0,
        "strike_offset": int(round((strike - 24500.0) / 50)),
        "moneyness": "OTM", "spot": 24500.0,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 1, "oi": 1, "iv": 15.0,
    }


class TestBuildBarStream:
    def _legs(self):
        # Same shape as _legs_with_prices but with strikes assigned.
        legs = {
            "ce_long":  LegFill(option_type="CE", side="BUY",  lots=1,
                                premium_target_inr=100.0, strike=24500.0,
                                entry_price=100.0),
            "pe_long":  LegFill(option_type="PE", side="BUY",  lots=1,
                                premium_target_inr=100.0, strike=24500.0,
                                entry_price=100.0),
            "ce_short": LegFill(option_type="CE", side="SELL", lots=2,
                                premium_target_inr=50.0, strike=24600.0,
                                entry_price=50.0),
            "pe_short": LegFill(option_type="PE", side="SELL", lots=2,
                                premium_target_inr=50.0, strike=24400.0,
                                entry_price=50.0),
        }
        return legs

    def test_yields_per_minute_bars_when_all_legs_present(self):
        legs = self._legs()
        rows = []
        for minute in [21, 22, 23]:
            ts = f"2026-04-08T09:{minute:02d}:00+05:30"
            rows.append(_bar_row(ts, "CE", 24500.0, 99.0))
            rows.append(_bar_row(ts, "PE", 24500.0, 99.0))
            rows.append(_bar_row(ts, "CE", 24600.0, 49.0))
            rows.append(_bar_row(ts, "PE", 24400.0, 49.0))
        df = pd.DataFrame(rows)

        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        assert len(bars) == 3
        for b in bars:
            assert set(b["prices"].keys()) == set(legs.keys())
            assert b["force_exit"] is False

    def test_carries_forward_within_max_gap(self):
        legs = self._legs()
        rows = []
        # ce_long missing at 09:22; available again at 09:23.
        for minute in [21, 22, 23]:
            ts = f"2026-04-08T09:{minute:02d}:00+05:30"
            if minute != 22:
                rows.append(_bar_row(ts, "CE", 24500.0, 99.0))
            rows.append(_bar_row(ts, "PE", 24500.0, 99.0))
            rows.append(_bar_row(ts, "CE", 24600.0, 49.0))
            rows.append(_bar_row(ts, "PE", 24400.0, 49.0))
        df = pd.DataFrame(rows)

        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        # 09:22 still emits (carry-forward of ce_long).
        assert len(bars) == 3

    def test_force_exit_when_gap_exceeds_max(self):
        legs = self._legs()
        rows = [
            _bar_row("2026-04-08T09:21:00+05:30", "CE", 24500.0, 99.0),
            _bar_row("2026-04-08T09:21:00+05:30", "PE", 24500.0, 99.0),
            _bar_row("2026-04-08T09:21:00+05:30", "CE", 24600.0, 49.0),
            _bar_row("2026-04-08T09:21:00+05:30", "PE", 24400.0, 49.0),
            # 35-minute gap: we'll see no other rows for 35 min, then a fresh bar.
            _bar_row("2026-04-08T09:57:00+05:30", "CE", 24500.0, 99.0),
            _bar_row("2026-04-08T09:57:00+05:30", "PE", 24500.0, 99.0),
            _bar_row("2026-04-08T09:57:00+05:30", "CE", 24600.0, 49.0),
            _bar_row("2026-04-08T09:57:00+05:30", "PE", 24400.0, 49.0),
        ]
        df = pd.DataFrame(rows)

        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        # Two emits: the first full bar, then a force_exit bar with last full prices.
        assert len(bars) == 2
        assert bars[0]["force_exit"] is False
        assert bars[1]["force_exit"] is True
        # force_exit bar yields the last full prices (the 09:21 ones)
        assert bars[1]["prices"]["ce_long"] == 99.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_zero_credit.py::TestBuildBarStream -v`
Expected: FAIL — `ImportError: cannot import name 'build_bar_stream'`

- [ ] **Step 3: Implement `build_bar_stream`**

Append to `engine/zero_credit_backtest.py`:

```python
def build_bar_stream(
    df: pd.DataFrame,
    legs: Dict[str, LegFill],
    max_gap_minutes: int = 30,
):
    """Yield per-minute bars with carry-forward (≤ max_gap_minutes) handling.

    Each yielded dict has:
        datetime: pd.Timestamp
        prices:   Dict[leg_key, float]  (bar 'close' for each leg)
        force_exit: bool                (True iff any leg's gap > max_gap_minutes)

    On force_exit we yield the LAST FULLY-OBSERVED bar (its complete prices),
    not the trigger bar. Iteration stops after emitting force_exit.

    Lookup key: (option_type, strike) — strikes are locked at entry.
    """
    if df.empty:
        return

    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    leg_lookup = {(l.option_type, float(l.strike)): k for k, l in legs.items()}
    df = df.sort_values("datetime")

    last_seen: Dict[str, Tuple[pd.Timestamp, float]] = {}
    last_full_bar: Optional[Tuple[pd.Timestamp, Dict[str, float]]] = None

    for ts, grp in df.groupby("datetime"):
        for _, row in grp.iterrows():
            key = leg_lookup.get((row["option_type"], float(row["strike"])))
            if key is None:
                continue
            last_seen[key] = (ts, float(row["close"]))

        prices: Dict[str, float] = {}
        force_exit = False
        for k in legs:
            if k not in last_seen:
                force_exit = True
                break
            seen_ts, seen_price = last_seen[k]
            gap_min = (ts - seen_ts).total_seconds() / 60.0
            if gap_min > max_gap_minutes:
                force_exit = True
                break
            prices[k] = seen_price

        if force_exit:
            if last_full_bar is None:
                return
            last_ts, last_prices = last_full_bar
            yield {"datetime": last_ts, "prices": dict(last_prices), "force_exit": True}
            return

        last_full_bar = (ts, dict(prices))
        yield {"datetime": ts, "prices": prices, "force_exit": False}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_zero_credit.py::TestBuildBarStream -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

Suggest:
```
git add engine/zero_credit_backtest.py tests/test_zero_credit.py
git commit -m "feat(zero_credit): bar stream with carry-forward + force-exit"
```

---

### Task 7: Per-day orchestrator `run_one_day`

**Files:**
- Modify: `engine/zero_credit_backtest.py`
- Test: `tests/test_zero_credit.py`

Combines: entry-slice lookup → ATM resolve → 4-leg strike-pick → economics → bar-stream + TP scan → time exit at 15:20 → P&L.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_zero_credit.py`:

```python
from engine.zero_credit_backtest import run_one_day, DayContext


def _full_chain_at(ts, atm_strike, ce_premiums, pe_premiums, expiry_type="WEEK"):
    """Mock a complete option chain at one timestamp. ce_premiums/pe_premiums
    are dicts {strike: open_price}."""
    rows = []
    for strike, open_ in ce_premiums.items():
        rows.append({
            "datetime": ts, "underlying": "NIFTY",
            "option_type": "CE", "expiry_type": expiry_type, "expiry_code": 1,
            "atm_strike": atm_strike,
            "strike_offset": int(round((strike - atm_strike) / 50)),
            "moneyness": "ATM" if strike == atm_strike else "OTM",
            "strike": strike, "spot": atm_strike,
            "open": open_, "high": open_, "low": open_, "close": open_,
            "volume": 1, "oi": 1, "iv": 15.0,
        })
    for strike, open_ in pe_premiums.items():
        rows.append({
            "datetime": ts, "underlying": "NIFTY",
            "option_type": "PE", "expiry_type": expiry_type, "expiry_code": 1,
            "atm_strike": atm_strike,
            "strike_offset": int(round((strike - atm_strike) / 50)),
            "moneyness": "ATM" if strike == atm_strike else "OTM",
            "strike": strike, "spot": atm_strike,
            "open": open_, "high": open_, "low": open_, "close": open_,
            "volume": 1, "oi": 1, "iv": 15.0,
        })
    return rows


def _make_ctx(**overrides):
    defaults = dict(
        date=date(2026, 4, 8),
        entry_time_str="09:20",
        time_exit_str="15:20",
        buy_premium_target_inr=100.0,
        sell_premium_target_inr=50.0,
        buy_lots=1, sell_lots=2,
        premium_match_tolerance_inr=20.0,
        tp_target_inr=1000.0,
        data_gap_force_exit_minutes=30,
    )
    defaults.update(overrides)
    return DayContext(**defaults)


class TestRunOneDay:
    def test_skip_when_no_entry_bar(self):
        ctx = _make_ctx()
        df = pd.DataFrame()  # no rows at all
        trade = run_one_day(df, ctx)
        assert trade.skip_reason == "no_entry_bar"

    def test_skip_when_buy_leg_outside_tolerance(self):
        # No CE strike has open within 20 of 100 → skip
        rows = _full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24400: 200.0, 24500: 130.0, 24600: 35.0},
            pe_premiums={24400: 30.0,  24500: 105.0, 24600: 195.0},
        )
        ctx = _make_ctx()
        trade = run_one_day(pd.DataFrame(rows), ctx)
        assert trade.skip_reason and trade.skip_reason.startswith(
            "no_strike_within_tolerance"
        )

    def test_time_exit_at_1520_when_tp_never_fires(self):
        rows = []
        # Entry chain at 09:20: pristine zero-credit fills.
        rows.extend(_full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
        ))
        # Holding bars from 09:21 to 15:19: tiny moves, never hits TP.
        for hh in range(9, 16):
            for mm in range(0, 60):
                if (hh == 9 and mm < 21) or (hh == 15 and mm > 19):
                    continue
                ts = f"2026-04-08T{hh:02d}:{mm:02d}:00+05:30"
                rows.extend(_full_chain_at(
                    ts, atm_strike=24500.0,
                    ce_premiums={24500: 100.0, 24600: 50.0},
                    pe_premiums={24500: 100.0, 24400: 50.0},
                ))
        # 15:20 time-exit bar.
        rows.extend(_full_chain_at(
            "2026-04-08T15:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
        ))
        df = pd.DataFrame(rows)

        ctx = _make_ctx()
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        assert trade.exit_reason == "TIME"
        assert trade.exit_time == "15:20"
        assert trade.pnl_inr == pytest.approx(0.0)

    def test_tp_fires_intraday(self):
        rows = []
        rows.extend(_full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
        ))
        # 09:21: shorts decay enough to clear TP.
        # signed_now = 1*100 + 1*100 + -2*30 + -2*30 = 200 - 120 = 80; net=0.
        # mtm_pts = 80 → mtm_inr = 5200 ≥ 1000 → TP at 09:21.
        rows.extend(_full_chain_at(
            "2026-04-08T09:21:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 30.0},
            pe_premiums={24500: 100.0, 24400: 30.0},
        ))
        df = pd.DataFrame(rows)

        ctx = _make_ctx()
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        assert trade.exit_reason == "TP"
        assert trade.exit_time == "09:21"
        assert trade.pnl_inr == pytest.approx(80.0 * 65)

    def test_filters_to_week_expiry_only(self):
        # MONTH-tagged rows present at 09:20 but with very different premiums.
        # The picker must use only WEEK rows.
        rows = _full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
            expiry_type="WEEK",
        )
        rows += _full_chain_at(
            "2026-04-08T09:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 250.0, 24600: 200.0},  # would beat WEEK on Δ
            pe_premiums={24500: 250.0, 24400: 200.0},
            expiry_type="MONTH",
        )
        # 15:20 squareoff: WEEK rows only, same prices = 0 P&L.
        rows += _full_chain_at(
            "2026-04-08T15:20:00+05:30", atm_strike=24500.0,
            ce_premiums={24500: 100.0, 24600: 50.0},
            pe_premiums={24500: 100.0, 24400: 50.0},
            expiry_type="WEEK",
        )
        df = pd.DataFrame(rows)

        ctx = _make_ctx()
        trade = run_one_day(df, ctx)
        assert trade.skip_reason is None
        # If MONTH leaked in, ce_long entry_price would be 250, not 100.
        assert trade.legs["ce_long"].entry_price == 100.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_zero_credit.py::TestRunOneDay -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement `DayContext` and `run_one_day`**

Append to `engine/zero_credit_backtest.py`:

```python
@dataclass
class DayContext:
    date: _date
    entry_time_str: str                      # "HH:MM"
    time_exit_str: str                       # "HH:MM"
    buy_premium_target_inr: float
    sell_premium_target_inr: float
    buy_lots: int
    sell_lots: int
    premium_match_tolerance_inr: float
    tp_target_inr: float
    data_gap_force_exit_minutes: int
    lot_size: int = LOT_SIZE_NIFTY


LEG_DEFINITIONS: List[Tuple[str, str, str, str]] = [
    # (leg_key, option_type, side, premium_field)
    ("ce_long",  "CE", "BUY",  "buy"),
    ("pe_long",  "PE", "BUY",  "buy"),
    ("ce_short", "CE", "SELL", "sell"),
    ("pe_short", "PE", "SELL", "sell"),
]


def _make_skip_trade(ctx: "DayContext", reason: str) -> "ZeroCreditTrade":
    return ZeroCreditTrade(
        date=ctx.date.isoformat(),
        entry_time=ctx.entry_time_str,
        atm_strike=float("nan"),
        spot_at_entry=float("nan"),
        net_debit_pts=float("nan"),
        net_debit_inr=float("nan"),
        tp_target_inr=float("nan"),
        exit_time="",
        exit_reason="",
        pnl_pts=0.0,
        pnl_inr=0.0,
        return_pct=0.0,
        running_equity_inr=float("nan"),
        skip_reason=reason,
        legs={},
    )


def _entry_slice(df: pd.DataFrame, ctx: "DayContext") -> pd.DataFrame:
    target_ts = f"{ctx.date.isoformat()}T{ctx.entry_time_str}:00+05:30"
    return df[
        (df["datetime"] == target_ts)
        & (df["expiry_code"] == 1)
        & (df["expiry_type"] == "WEEK")
    ]


def _holding_slice(
    df: pd.DataFrame,
    ctx: "DayContext",
    legs: Dict[str, LegFill],
) -> pd.DataFrame:
    """All bars after entry up through time-exit timestamp, restricted to the
    locked (option_type, strike) pairs."""
    entry_ts = f"{ctx.date.isoformat()}T{ctx.entry_time_str}:00+05:30"
    exit_ts  = f"{ctx.date.isoformat()}T{ctx.time_exit_str}:00+05:30"

    sub = df[
        (df["datetime"] > entry_ts)
        & (df["datetime"] <= exit_ts)
        & (df["expiry_code"] == 1)
        & (df["expiry_type"] == "WEEK")
    ]
    if sub.empty:
        return sub

    locked_pairs = {(l.option_type, float(l.strike)) for l in legs.values()}
    pairs = list(zip(sub["option_type"].astype(str), sub["strike"].astype(float)))
    mask = [p in locked_pairs for p in pairs]
    return sub[mask].sort_values("datetime")


def _time_exit_prices(
    holding_df: pd.DataFrame,
    ctx: "DayContext",
    legs: Dict[str, LegFill],
) -> Optional[Tuple[pd.Timestamp, Dict[str, float]]]:
    """Find the latest available bar at-or-before time_exit on entry day that
    has ALL four legs. Walk back if the exact 15:20 bar is missing."""
    deadline_ts = pd.Timestamp(
        f"{ctx.date.isoformat()}T{ctx.time_exit_str}:00+05:30"
    )
    bars = holding_df[
        pd.to_datetime(holding_df["datetime"]) <= deadline_ts
    ].sort_values("datetime", ascending=False)

    leg_lookup = {(l.option_type, float(l.strike)): k for k, l in legs.items()}
    for ts, grp in bars.groupby("datetime", sort=False):
        prices: Dict[str, float] = {}
        for _, row in grp.iterrows():
            k = leg_lookup.get((row["option_type"], float(row["strike"])))
            if k:
                prices[k] = float(row["close"])
        if set(prices.keys()) == set(legs.keys()):
            return pd.Timestamp(ts), prices
    return None


def run_one_day(df: pd.DataFrame, ctx: "DayContext") -> "ZeroCreditTrade":
    """Run the strategy for one trading day against `df` (NIFTY options 1-min)."""
    entry_slice = _entry_slice(df, ctx)
    if entry_slice.empty:
        return _make_skip_trade(ctx, "no_entry_bar")

    atm_strike, spot_at_entry = resolve_atm_strike(entry_slice)
    if atm_strike is None:
        return _make_skip_trade(ctx, "no_atm_row")

    legs: Dict[str, LegFill] = {}
    for leg_key, opt_type, side, premium_field in LEG_DEFINITIONS:
        target = (ctx.buy_premium_target_inr if premium_field == "buy"
                  else ctx.sell_premium_target_inr)
        lots = ctx.buy_lots if side == "BUY" else ctx.sell_lots
        pick = pick_strike_by_premium(
            entry_slice, option_type=opt_type,
            target_premium_inr=target,
            tolerance_inr=ctx.premium_match_tolerance_inr,
            atm_strike=atm_strike,
        )
        if pick.skipped:
            return _make_skip_trade(
                ctx, f"no_strike_within_tolerance: {leg_key}"
            )
        legs[leg_key] = LegFill(
            option_type=opt_type, side=side, lots=lots,
            premium_target_inr=target,
            strike=pick.strike,
            entry_price=pick.entry_price,
        )

    net_pts, net_inr, tp = compute_entry_economics(
        legs, ctx.tp_target_inr, lot_size=ctx.lot_size
    )

    holding_df = _holding_slice(df, ctx, legs)
    bars = list(build_bar_stream(
        holding_df, legs, max_gap_minutes=ctx.data_gap_force_exit_minutes
    ))

    exit_ts_limit = pd.Timestamp(
        f"{ctx.date.isoformat()}T{ctx.time_exit_str}:00+05:30"
    )
    pre_exit_bars = [
        b for b in bars
        if pd.Timestamp(b["datetime"]) < exit_ts_limit and not b.get("force_exit")
    ]

    tp_result = scan_for_tp_exit(
        legs, pre_exit_bars, net_pts, tp, lot_size=ctx.lot_size
    )

    if tp_result is not None:
        exit_ts, exit_prices, _mtm = tp_result
        exit_reason = "TP"
    else:
        force_bar = next((b for b in bars if b.get("force_exit")), None)
        if force_bar is not None:
            exit_ts = pd.Timestamp(force_bar["datetime"])
            exit_prices = force_bar["prices"]
            exit_reason = "data_gap_force_exit"
        else:
            squareoff = _time_exit_prices(holding_df, ctx, legs)
            if squareoff is None:
                return _make_skip_trade(ctx, "no_time_exit_bar")
            exit_ts, exit_prices = squareoff
            exit_reason = "TIME"

    for k, leg in legs.items():
        leg.exit_price = exit_prices[k]
    exit_value_pts = sum(_leg_signed_value(l, l.exit_price) for l in legs.values())
    pnl_pts = exit_value_pts - net_pts
    pnl_inr = pnl_pts * ctx.lot_size

    return ZeroCreditTrade(
        date=ctx.date.isoformat(),
        entry_time=ctx.entry_time_str,
        atm_strike=atm_strike,
        spot_at_entry=spot_at_entry,
        net_debit_pts=net_pts,
        net_debit_inr=net_inr,
        tp_target_inr=tp,
        exit_time=exit_ts.strftime("%H:%M"),
        exit_reason=exit_reason,
        pnl_pts=pnl_pts,
        pnl_inr=pnl_inr,
        return_pct=0.0,                 # filled in by caller after equity update
        running_equity_inr=0.0,         # filled in by caller
        skip_reason=None,
        legs=legs,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_zero_credit.py::TestRunOneDay -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

Suggest:
```
git add engine/zero_credit_backtest.py tests/test_zero_credit.py
git commit -m "feat(zero_credit): per-day orchestrator (entry, TP scan, time exit)"
```

---

### Task 8: Multi-day backtest loop + parse_config

**Files:**
- Modify: `engine/zero_credit_backtest.py`
- Test: `tests/test_zero_credit.py`

Loops over each trading day in the date range and threads the running-equity counter through. Skipped days contribute 0 P&L but still get their `running_equity_inr` filled.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_zero_credit.py`:

```python
from engine.zero_credit_backtest import run_backtest, parse_config


class TestParseConfig:
    def test_extracts_all_fields(self):
        config = {
            "entry": {"entry_time": "09:20"},
            "structure": {
                "buy_premium_target_inr": 100,
                "sell_premium_target_inr": 50,
                "buy_lots": 1, "sell_lots": 2,
                "premium_match_tolerance_inr": 20,
            },
            "exit": {
                "tp_target_inr": 1000,
                "time_exit": "15:20",
                "data_gap_force_exit_minutes": 30,
            },
            "sizing": {"reference_capital": 200000},
            "backtest_start": "2025-01-01",
            "backtest_end":   "2026-05-08",
        }
        params = parse_config(config)
        assert params["entry_time"] == "09:20"
        assert params["time_exit"] == "15:20"
        assert params["buy_premium_target_inr"] == 100.0
        assert params["sell_premium_target_inr"] == 50.0
        assert params["buy_lots"] == 1
        assert params["sell_lots"] == 2
        assert params["premium_match_tolerance_inr"] == 20.0
        assert params["tp_target_inr"] == 1000.0
        assert params["data_gap_force_exit_minutes"] == 30
        assert params["reference_capital"] == 200000.0


class TestRunBacktest:
    def _two_day_df(self):
        rows = []
        for day, atm in [(8, 24500.0), (9, 24600.0)]:
            # 09:20 entry chain
            ts_entry = f"2026-04-{day:02d}T09:20:00+05:30"
            rows += _full_chain_at(
                ts_entry, atm_strike=atm,
                ce_premiums={atm: 100.0, atm + 100: 50.0},
                pe_premiums={atm: 100.0, atm - 100: 50.0},
            )
            # 09:21 holding bar — shorts decay enough to TP.
            ts_post = f"2026-04-{day:02d}T09:21:00+05:30"
            rows += _full_chain_at(
                ts_post, atm_strike=atm,
                ce_premiums={atm: 100.0, atm + 100: 30.0},
                pe_premiums={atm: 100.0, atm - 100: 30.0},
            )
            # 15:20 squareoff (would be used if TP didn't fire).
            ts_exit = f"2026-04-{day:02d}T15:20:00+05:30"
            rows += _full_chain_at(
                ts_exit, atm_strike=atm,
                ce_premiums={atm: 100.0, atm + 100: 30.0},
                pe_premiums={atm: 100.0, atm - 100: 30.0},
            )
        return pd.DataFrame(rows)

    def test_runs_each_trading_day_in_range(self):
        df = self._two_day_df()
        config = {
            "entry": {"entry_time": "09:20"},
            "structure": {
                "buy_premium_target_inr": 100, "sell_premium_target_inr": 50,
                "buy_lots": 1, "sell_lots": 2,
                "premium_match_tolerance_inr": 20,
            },
            "exit": {"tp_target_inr": 1000, "time_exit": "15:20",
                     "data_gap_force_exit_minutes": 30},
            "sizing": {"reference_capital": 200000},
            "backtest_start": "2026-04-08",
            "backtest_end":   "2026-04-09",
        }
        result = run_backtest(df, config)
        trades = result["trades"]
        assert len(trades) == 2
        for t in trades:
            assert t.exit_reason == "TP"
            assert t.pnl_inr > 0
        # Running equity threaded.
        assert trades[0].running_equity_inr > 200000
        assert trades[1].running_equity_inr > trades[0].running_equity_inr
        # return_pct populated.
        assert trades[0].return_pct == pytest.approx(
            trades[0].pnl_inr / 200000.0
        )

    def test_skip_day_with_no_data(self):
        df = self._two_day_df()
        config = {
            "entry": {"entry_time": "09:20"},
            "structure": {
                "buy_premium_target_inr": 100, "sell_premium_target_inr": 50,
                "buy_lots": 1, "sell_lots": 2,
                "premium_match_tolerance_inr": 20,
            },
            "exit": {"tp_target_inr": 1000, "time_exit": "15:20",
                     "data_gap_force_exit_minutes": 30},
            "sizing": {"reference_capital": 200000},
            "backtest_start": "2026-04-08",
            "backtest_end":   "2026-04-10",  # 10th has no data
        }
        result = run_backtest(df, config)
        trades = result["trades"]
        # 2 placed; 10th would have a skip row only if we explicitly track
        # missing-data days. Spec says holidays/no-data days are naturally
        # skipped (no row emitted). Confirm here.
        assert len(trades) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_zero_credit.py -k "ParseConfig or RunBacktest" -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement `parse_config` and `run_backtest`**

Append to `engine/zero_credit_backtest.py`:

```python
def parse_config(config: dict) -> dict:
    """Flatten a JSON config into the kwargs needed by run_backtest."""
    structure = config["structure"]
    exit_cfg = config["exit"]
    return {
        "entry_time": config["entry"]["entry_time"],
        "time_exit": exit_cfg["time_exit"],
        "buy_premium_target_inr": float(structure["buy_premium_target_inr"]),
        "sell_premium_target_inr": float(structure["sell_premium_target_inr"]),
        "buy_lots": int(structure["buy_lots"]),
        "sell_lots": int(structure["sell_lots"]),
        "premium_match_tolerance_inr": float(structure["premium_match_tolerance_inr"]),
        "tp_target_inr": float(exit_cfg["tp_target_inr"]),
        "data_gap_force_exit_minutes": int(exit_cfg["data_gap_force_exit_minutes"]),
        "reference_capital": float(config["sizing"]["reference_capital"]),
    }


def _trading_days_from_df(df: pd.DataFrame) -> List[_date]:
    return sorted({pd.to_datetime(ts).date() for ts in df["datetime"].unique()})


def run_backtest(df: pd.DataFrame, config: dict) -> dict:
    """Run the strategy for every trading day in [backtest_start, backtest_end]."""
    p = parse_config(config)
    bt_start = pd.to_datetime(config["backtest_start"]).date()
    bt_end   = pd.to_datetime(config["backtest_end"]).date()

    trading_days = [d for d in _trading_days_from_df(df) if bt_start <= d <= bt_end]
    capital = p["reference_capital"]

    trades: List[ZeroCreditTrade] = []
    running_equity = capital

    for d in trading_days:
        ctx = DayContext(
            date=d,
            entry_time_str=p["entry_time"],
            time_exit_str=p["time_exit"],
            buy_premium_target_inr=p["buy_premium_target_inr"],
            sell_premium_target_inr=p["sell_premium_target_inr"],
            buy_lots=p["buy_lots"],
            sell_lots=p["sell_lots"],
            premium_match_tolerance_inr=p["premium_match_tolerance_inr"],
            tp_target_inr=p["tp_target_inr"],
            data_gap_force_exit_minutes=p["data_gap_force_exit_minutes"],
        )
        trade = run_one_day(df, ctx)
        running_equity += trade.pnl_inr
        trade.return_pct = trade.pnl_inr / capital if capital else 0.0
        trade.running_equity_inr = running_equity
        trades.append(trade)

    return {"trades": trades, "config": config}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_zero_credit.py -k "ParseConfig or RunBacktest" -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

Suggest:
```
git add engine/zero_credit_backtest.py tests/test_zero_credit.py
git commit -m "feat(zero_credit): multi-day backtest loop + parse_config"
```

---

### Task 9: Equity curve, summary metrics, CSV writers

**Files:**
- Modify: `engine/zero_credit_backtest.py`
- Test: `tests/test_zero_credit.py`

Per spec section 8.3, this is the trimmed metric set: no Sharpe / Sortino / annualized return.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_zero_credit.py`:

```python
import tempfile

from engine.zero_credit_backtest import (
    build_equity_curve,
    summarize_metrics,
    max_consecutive_losses,
    write_trades_csv,
    write_equity_csv,
    trades_to_dataframe,
    print_summary,
)


def _trade(date_, pnl_inr, exit_reason="TP", skip_reason=None,
           running_equity=200000.0, capital=200000.0):
    return ZeroCreditTrade(
        date=date_, entry_time="09:20",
        atm_strike=24500.0, spot_at_entry=24500.0,
        net_debit_pts=0.0, net_debit_inr=0.0, tp_target_inr=1000.0,
        exit_time="15:20" if skip_reason is None else "",
        exit_reason="" if skip_reason else exit_reason,
        pnl_pts=pnl_inr / 65 if pnl_inr else 0.0,
        pnl_inr=pnl_inr,
        return_pct=pnl_inr / capital if capital else 0.0,
        running_equity_inr=running_equity,
        skip_reason=skip_reason, legs={},
    )


class TestMaxConsecutiveLosses:
    def test_basic_run(self):
        assert max_consecutive_losses([10, -5, -8, -3, 7, -1]) == 3

    def test_no_losses(self):
        assert max_consecutive_losses([10, 5, 0, 7]) == 0

    def test_empty(self):
        assert max_consecutive_losses([]) == 0


class TestBuildEquityCurve:
    def test_one_row_per_trade_with_drawdown(self):
        trades = [
            _trade("2026-04-08",  500.0, running_equity=200500.0),
            _trade("2026-04-09", -1500.0, running_equity=199000.0),
            _trade("2026-04-10",  800.0, running_equity=199800.0),
        ]
        curve = build_equity_curve(trades, starting_capital=200000.0)
        assert len(curve) == 3
        assert curve.iloc[0]["equity_inr"] == 200500.0
        # Peak after row 1 = 200500. After row 2 equity=199000 → dd=1500.
        assert curve.iloc[1]["drawdown_inr"] == pytest.approx(1500.0)
        # Row 3 equity 199800; peak still 200500 → dd=700.
        assert curve.iloc[2]["drawdown_inr"] == pytest.approx(700.0)

    def test_skipped_days_carry_running_equity(self):
        trades = [
            _trade("2026-04-08",   500.0, running_equity=200500.0),
            _trade("2026-04-09",     0.0, skip_reason="no_entry_bar",
                   running_equity=200500.0),
            _trade("2026-04-10",  -200.0, running_equity=200300.0),
        ]
        curve = build_equity_curve(trades, starting_capital=200000.0)
        assert len(curve) == 3
        assert curve.iloc[1]["in_trade"] is False
        assert curve.iloc[1]["equity_inr"] == 200500.0


class TestSummarizeMetrics:
    def test_counts_and_pnl(self):
        trades = [
            _trade("2026-04-08",  1000.0, running_equity=201000.0),
            _trade("2026-04-09", -2000.0, running_equity=199000.0),
            _trade("2026-04-10",   500.0, running_equity=199500.0),
            _trade("2026-04-11",     0.0, skip_reason="no_entry_bar",
                   running_equity=199500.0),
        ]
        s = summarize_metrics(trades, starting_capital=200000.0)
        assert s["total_days_processed"] == 4
        assert s["trades_placed"] == 3
        assert s["trades_skipped"] == 1
        assert s["wins"] == 2
        assert s["losses"] == 1
        assert s["total_pnl_inr"] == pytest.approx(-500.0)
        assert s["best_trade_inr"] == 1000.0
        assert s["worst_trade_inr"] == -2000.0
        assert s["max_consecutive_losses"] == 1
        assert s["exit_reason_counts"]["TP"] == 3
        assert s["skip_reason_counts"]["no_entry_bar"] == 1
        # No Sharpe / Sortino keys (per spec).
        assert "sharpe" not in s
        assert "sortino" not in s


class TestCsvWriters:
    def test_trades_csv_has_per_leg_columns(self):
        trade = _trade("2026-04-08", 1000.0, running_equity=201000.0)
        trade.legs = {
            "ce_long":  LegFill(option_type="CE", side="BUY", lots=1,
                                premium_target_inr=100.0, strike=24500.0,
                                entry_price=98.5, exit_price=110.0),
            "pe_long":  LegFill(option_type="PE", side="BUY", lots=1,
                                premium_target_inr=100.0, strike=24500.0,
                                entry_price=99.5, exit_price=80.0),
            "ce_short": LegFill(option_type="CE", side="SELL", lots=2,
                                premium_target_inr=50.0, strike=24600.0,
                                entry_price=49.0, exit_price=30.0),
            "pe_short": LegFill(option_type="PE", side="SELL", lots=2,
                                premium_target_inr=50.0, strike=24400.0,
                                entry_price=51.0, exit_price=70.0),
        }
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            write_trades_csv([trade], f.name)
            df = pd.read_csv(f.name)
        assert "ce_long_strike" in df.columns
        assert "ce_long_entry" in df.columns
        assert "ce_long_exit" in df.columns
        assert df.iloc[0]["ce_long_strike"] == 24500.0

    def test_equity_csv_columns(self):
        trades = [
            _trade("2026-04-08", 500.0, running_equity=200500.0),
            _trade("2026-04-09", -200.0, running_equity=200300.0),
        ]
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            write_equity_csv(trades, starting_capital=200000.0, path=f.name)
            df = pd.read_csv(f.name)
        assert list(df.columns) == [
            "date", "equity_inr", "drawdown_inr", "drawdown_pct", "in_trade"
        ]
        assert len(df) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_zero_credit.py -k "MaxConsecutive or BuildEquity or SummarizeMetrics or CsvWriters" -v`
Expected: FAIL — multiple `ImportError`s

- [ ] **Step 3: Implement metrics, equity curve, and writers**

Append to `engine/zero_credit_backtest.py`:

```python
def _is_nan(x) -> bool:
    try:
        return x != x  # NaN != NaN
    except Exception:
        return False


def build_equity_curve(
    trades: List[ZeroCreditTrade],
    starting_capital: float,
) -> pd.DataFrame:
    """One row per trade attempt: date, equity_inr, drawdown_inr, drawdown_pct,
    in_trade. Skipped days carry forward the running_equity_inr unchanged."""
    if not trades:
        return pd.DataFrame(columns=["date", "equity_inr", "drawdown_inr",
                                     "drawdown_pct", "in_trade"])
    rows = []
    peak = starting_capital
    for t in trades:
        equity = t.running_equity_inr if not _is_nan(t.running_equity_inr) else starting_capital
        peak = max(peak, equity)
        dd_inr = peak - equity
        dd_pct = dd_inr / peak if peak else 0.0
        rows.append({
            "date": t.date,
            "equity_inr": equity,
            "drawdown_inr": dd_inr,
            "drawdown_pct": dd_pct,
            "in_trade": t.skip_reason is None,
        })
    return pd.DataFrame(rows)


def max_consecutive_losses(pnls: List[float]) -> int:
    """Length of the longest run where pnl < 0 strictly."""
    longest = 0
    current = 0
    for p in pnls:
        if p < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _count_by(items, keyfn):
    counts: Dict[str, int] = {}
    for it in items:
        k = keyfn(it)
        counts[k] = counts.get(k, 0) + 1
    return counts


def summarize_metrics(
    trades: List[ZeroCreditTrade],
    starting_capital: float,
) -> dict:
    """Trimmed summary per spec §8.3 — no Sharpe / Sortino / annualized return."""
    placed = [t for t in trades if t.skip_reason is None]
    pnls = [t.pnl_inr for t in placed]
    wins = [t for t in placed if t.pnl_inr > 0]
    losses = [t for t in placed if t.pnl_inr < 0]

    equity_curve = build_equity_curve(trades, starting_capital)
    if not equity_curve.empty:
        max_dd_inr = float(equity_curve["drawdown_inr"].max())
        max_dd_pct = float(equity_curve["drawdown_pct"].max())
    else:
        max_dd_inr = 0.0
        max_dd_pct = 0.0

    return {
        "total_days_processed": len(trades),
        "trades_placed": len(placed),
        "trades_skipped": len(trades) - len(placed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(placed) if placed else 0.0,
        "loss_rate": len(losses) / len(placed) if placed else 0.0,
        "pct_profitable_days": len(wins) / len(placed) if placed else 0.0,
        "mean_pnl_inr": statistics.fmean(pnls) if pnls else 0.0,
        "median_pnl_inr": statistics.median(pnls) if pnls else 0.0,
        "total_pnl_inr": sum(pnls),
        "total_return_pct": sum(pnls) / starting_capital if starting_capital else 0.0,
        "max_drawdown_inr": max_dd_inr,
        "max_drawdown_pct": max_dd_pct,
        "max_consecutive_losses": max_consecutive_losses(pnls),
        "best_trade_inr": max(pnls) if pnls else 0.0,
        "worst_trade_inr": min(pnls) if pnls else 0.0,
        "exit_reason_counts": _count_by(placed, lambda t: t.exit_reason),
        "skip_reason_counts": _count_by(
            [t for t in trades if t.skip_reason], lambda t: t.skip_reason
        ),
    }


def trades_to_dataframe(trades: List[ZeroCreditTrade]) -> pd.DataFrame:
    """Flatten trades (with per-leg strikes/prices) into a DataFrame."""
    if not trades:
        return pd.DataFrame()

    rows = []
    for t in trades:
        row = {k: v for k, v in asdict(t).items() if k != "legs"}
        for leg_key, leg in t.legs.items():
            row[f"{leg_key}_strike"] = leg.strike
            row[f"{leg_key}_entry"] = leg.entry_price
            row[f"{leg_key}_exit"] = leg.exit_price
        rows.append(row)
    return pd.DataFrame(rows)


def write_trades_csv(trades: List[ZeroCreditTrade], path) -> None:
    df = trades_to_dataframe(trades)
    df.to_csv(path, index=False)


def write_equity_csv(
    trades: List[ZeroCreditTrade],
    starting_capital: float,
    path,
) -> None:
    df = build_equity_curve(trades, starting_capital)
    df.to_csv(path, index=False)


def print_summary(summary: dict) -> None:
    s = summary
    lines = [
        f"Total days processed: {s['total_days_processed']}",
        f"Trades placed: {s['trades_placed']}",
        f"Trades skipped: {s['trades_skipped']}",
        f"Wins (P&L > 0): {s['wins']}  ({s['win_rate']*100:.2f}%)",
        f"Losses (P&L < 0): {s['losses']}  ({s['loss_rate']*100:.2f}%)",
        f"% profitable days: {s['pct_profitable_days']*100:.2f}%",
        f"Mean P&L (Rs): {s['mean_pnl_inr']:.2f}",
        f"Median P&L (Rs): {s['median_pnl_inr']:.2f}",
        f"Total P&L (Rs): {s['total_pnl_inr']:.2f}",
        f"Total return on reference capital: {s['total_return_pct']*100:.2f}%",
        f"Max drawdown (Rs / pct): {s['max_drawdown_inr']:.2f} / {s['max_drawdown_pct']*100:.2f}%",
        f"Max consecutive losing days: {s['max_consecutive_losses']}",
        f"Best day: Rs {s['best_trade_inr']:.2f}    Worst day: Rs {s['worst_trade_inr']:.2f}",
        f"Exit reason counts: {s['exit_reason_counts']}",
        f"Skip reason counts: {s['skip_reason_counts']}",
    ]
    for line in lines:
        print(line)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_zero_credit.py -k "MaxConsecutive or BuildEquity or SummarizeMetrics or CsvWriters" -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

Suggest:
```
git add engine/zero_credit_backtest.py tests/test_zero_credit.py
git commit -m "feat(zero_credit): equity curve, trimmed summary, CSV writers"
```

---

### Task 10: Top-level `run()` entrypoint with parquet I/O

**Files:**
- Modify: `engine/zero_credit_backtest.py`
- Test: `tests/test_zero_credit.py`

Loads parquet, normalizes datetime, applies date filter, runs backtest, writes both CSVs, prints summary, returns paths.

- [ ] **Step 1: Write failing test (smoke / integration)**

Append to `tests/test_zero_credit.py`:

```python
class TestRunEntrypoint:
    def test_writes_csvs_and_returns_paths(self, tmp_path):
        # Mock parquet by writing one to disk first.
        rows = []
        for day_str, atm in [("2026-04-08", 24500.0), ("2026-04-09", 24600.0)]:
            for ts_str, ce500, ce600, pe500, pe400 in [
                (f"{day_str}T09:20:00+05:30", 100.0, 50.0, 100.0, 50.0),
                (f"{day_str}T09:21:00+05:30", 100.0, 30.0, 100.0, 30.0),
                (f"{day_str}T15:20:00+05:30", 100.0, 30.0, 100.0, 30.0),
            ]:
                ce_p = {atm: ce500, atm + 100: ce600}
                pe_p = {atm: pe500, atm - 100: pe400}
                rows += _full_chain_at(
                    ts_str, atm_strike=atm,
                    ce_premiums=ce_p, pe_premiums=pe_p,
                )
        df = pd.DataFrame(rows)
        # Convert datetime to tz-aware datetime64 to match the real parquet shape.
        df["datetime"] = pd.to_datetime(df["datetime"])

        parquet_path = tmp_path / "fake.parquet"
        df.to_parquet(parquet_path)

        config = {
            "name": "zero_credit", "strategy_type": "zero_credit",
            "instruments": ["NIFTY"],
            "entry": {"entry_time": "09:20"},
            "structure": {
                "buy_premium_target_inr": 100, "sell_premium_target_inr": 50,
                "buy_lots": 1, "sell_lots": 2,
                "premium_match_tolerance_inr": 20,
            },
            "exit": {"tp_target_inr": 1000, "time_exit": "15:20",
                     "data_gap_force_exit_minutes": 30},
            "sizing": {"reference_capital": 200000},
            "backtest_start": "2026-04-08",
            "backtest_end":   "2026-04-09",
        }
        from engine.zero_credit_backtest import run
        result = run(
            config,
            options_path=str(parquet_path),
            output_dir=str(tmp_path / "out"),
        )

        assert "trades_csv" in result and "equity_csv" in result
        assert Path(result["trades_csv"]).exists()
        assert Path(result["equity_csv"]).exists()
        assert result["summary"]["trades_placed"] == 2
        assert result["summary"]["total_pnl_inr"] > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_zero_credit.py::TestRunEntrypoint -v`
Expected: FAIL — `ImportError: cannot import name 'run'`

- [ ] **Step 3: Implement `run()`**

Append to `engine/zero_credit_backtest.py`:

```python
def run(
    config: dict,
    options_path: str,
    output_dir: str,
) -> dict:
    """Top-level entrypoint. Loads parquet, runs backtest, writes CSVs, prints summary."""
    df = pd.read_parquet(options_path)
    df = df[df["underlying"] == "NIFTY"]

    # Normalize datetime column to ISO-8601 strings of the form
    # "YYYY-MM-DDTHH:MM:SS+05:30" so all downstream filters work uniformly.
    if pd.api.types.is_datetime64_any_dtype(df["datetime"]):
        df = df.assign(datetime=df["datetime"].dt.strftime("%Y-%m-%dT%H:%M:%S%z"))
        df["datetime"] = df["datetime"].str.replace(
            r"([+\-]\d{2})(\d{2})$", r"\1:\2", regex=True
        )

    bt_start_str = f"{config['backtest_start']}T00:00:00+05:30"
    bt_end_str   = f"{config['backtest_end']}T23:59:59+05:30"
    df = df[(df["datetime"] >= bt_start_str) & (df["datetime"] <= bt_end_str)]

    backtest_result = run_backtest(df, config)
    trades = backtest_result["trades"]

    capital = float(config["sizing"]["reference_capital"])
    summary = summarize_metrics(trades, capital)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    start_str = config["backtest_start"]
    end_str = config["backtest_end"]
    trades_path = out / f"zero_credit_trades_{start_str}_{end_str}.csv"
    equity_path = out / f"zero_credit_equity_{start_str}_{end_str}.csv"

    write_trades_csv(trades, trades_path)
    write_equity_csv(trades, capital, equity_path)

    print_summary(summary)
    return {
        "trades": trades, "summary": summary,
        "trades_csv": str(trades_path),
        "equity_csv": str(equity_path),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_zero_credit.py::TestRunEntrypoint -v`
Expected: 1 passed

- [ ] **Step 5: Run the full test file**

Run: `pytest tests/test_zero_credit.py -v`
Expected: all tests passing (~30+ tests across all classes).

- [ ] **Step 6: Commit**

Suggest:
```
git add engine/zero_credit_backtest.py tests/test_zero_credit.py
git commit -m "feat(zero_credit): top-level run() entrypoint"
```

---

### Task 11: Smoke-test the engine on real data

**Files:**
- (No file changes — verification step.)

- [ ] **Step 1: Run the engine on a one-week real-data slice**

Run from project root:
```
python3 -c "
import json
from engine.zero_credit_backtest import run
cfg = json.load(open('saved_strategies/zero_credit.json'))
cfg['backtest_start'] = '2026-04-01'
cfg['backtest_end']   = '2026-04-08'
result = run(cfg,
             options_path='data/options/nifty/NIFTY_OPTIONS_1m.parquet',
             output_dir='output/zero_credit')
print('PLACED:', result['summary']['trades_placed'])
print('SKIPPED:', result['summary']['trades_skipped'])
print('TOTAL P&L:', result['summary']['total_pnl_inr'])
print('TRADES_CSV:', result['trades_csv'])
"
```

Expected: a real summary block prints, `output/zero_credit/zero_credit_trades_*.csv` exists, exit reasons are some mix of `TP` and `TIME`, no exceptions.

- [ ] **Step 2: Spot-check the trades CSV manually**

Open `output/zero_credit/zero_credit_trades_2026-04-01_2026-04-08.csv` and verify:
- Each row has 4 leg strikes filled in (or `skip_reason` set).
- For placed rows, `ce_long_strike` and `pe_long_strike` are reasonably close to ATM.
- For placed rows, `ce_short_strike > ce_long_strike` (further OTM call).
- For placed rows, `pe_short_strike < pe_long_strike` (further OTM put).
- `net_debit_inr` is small (close to 0; that's the "zero credit" property).

If anything is glaringly wrong, stop and debug before moving on.

---

### Task 12: Streamlit UI runner

**Files:**
- Create: `ui/zero_credit_backtest_runner.py`

Mirrors `ui/debit_spread_backtest_runner.py` shape.

- [ ] **Step 1: Create the runner**

Create `ui/zero_credit_backtest_runner.py`:

```python
"""Streamlit form + runner for the Zero Credit 4-leg backtest."""
import json
import math
import os
from datetime import date

import pandas as pd
import streamlit as st

from engine.zero_credit_backtest import run


DEFAULT_CONFIG_PATH = "saved_strategies/zero_credit.json"
DEFAULT_OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"
DEFAULT_OUTPUT_DIR = "output/zero_credit"


def _load_default_config() -> dict:
    if os.path.exists(DEFAULT_CONFIG_PATH):
        with open(DEFAULT_CONFIG_PATH) as f:
            return json.load(f)
    raise FileNotFoundError(
        f"Missing config file at {DEFAULT_CONFIG_PATH}."
    )


def _fmt_money(val: float) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    return f"₹{val:,.0f}"


def _fmt_pct(val: float) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    return f"{val * 100:.2f}%"


def render_zero_credit_backtest() -> None:
    st.header("Zero Credit — 4-leg premium-targeted NIFTY")
    st.caption(
        "Daily NIFTY weekly-options strategy. At 09:20, buy 1×CE + 1×PE near "
        "₹100 and sell 2×CE + 2×PE near ₹50 (net premium ≈ 0). Exit at ₹1000 "
        "TP (configurable, intra-day 1-min check) or 15:20 close. No stop loss."
    )

    cfg = _load_default_config()

    col_a, col_b, col_c = st.columns(3)
    start_date = col_a.date_input(
        "Backtest start", value=date.fromisoformat(cfg["backtest_start"]),
        key="zc_start",
    )
    end_date = col_b.date_input(
        "Backtest end", value=date.fromisoformat(cfg["backtest_end"]),
        key="zc_end",
    )
    tp_target = col_c.number_input(
        "TP target (₹)",
        min_value=100, max_value=20000, step=100,
        value=int(cfg["exit"]["tp_target_inr"]),
        help="Take-profit target in rupees on combined unrealized P&L.",
        key="zc_tp",
    )

    col_d, col_e, col_f = st.columns(3)
    entry_time = col_d.text_input(
        "Entry time (HH:MM)", value=cfg["entry"]["entry_time"],
        key="zc_entry_time",
    )
    time_exit = col_e.text_input(
        "Time exit (HH:MM)", value=cfg["exit"]["time_exit"],
        key="zc_time_exit",
    )
    capital = col_f.number_input(
        "Reference capital (₹)",
        min_value=50000, step=50000,
        value=int(cfg["sizing"]["reference_capital"]),
        key="zc_capital",
    )

    col_g, col_h, col_i = st.columns(3)
    buy_target = col_g.number_input(
        "Buy premium target (₹)",
        min_value=10, max_value=500, step=5,
        value=int(cfg["structure"]["buy_premium_target_inr"]),
        key="zc_buy_target",
    )
    sell_target = col_h.number_input(
        "Sell premium target (₹)",
        min_value=5, max_value=300, step=5,
        value=int(cfg["structure"]["sell_premium_target_inr"]),
        key="zc_sell_target",
    )
    tolerance = col_i.number_input(
        "Premium match tolerance (₹)",
        min_value=1, max_value=100, step=1,
        value=int(cfg["structure"]["premium_match_tolerance_inr"]),
        key="zc_tolerance",
    )

    col_j, col_k, _ = st.columns(3)
    buy_lots = col_j.number_input(
        "Buy lots per leg",
        min_value=1, max_value=20, step=1,
        value=int(cfg["structure"]["buy_lots"]),
        key="zc_buy_lots",
    )
    sell_lots = col_k.number_input(
        "Sell lots per leg",
        min_value=1, max_value=20, step=1,
        value=int(cfg["structure"]["sell_lots"]),
        key="zc_sell_lots",
    )

    if st.button("Run backtest", type="primary", key="zc_run_button"):
        run_config = dict(cfg)
        run_config["backtest_start"] = start_date.isoformat()
        run_config["backtest_end"] = end_date.isoformat()
        run_config["entry"] = {"entry_time": entry_time}
        run_config["exit"] = {
            **cfg["exit"],
            "tp_target_inr": int(tp_target),
            "time_exit": time_exit,
        }
        run_config["structure"] = {
            **cfg["structure"],
            "buy_premium_target_inr": int(buy_target),
            "sell_premium_target_inr": int(sell_target),
            "buy_lots": int(buy_lots),
            "sell_lots": int(sell_lots),
            "premium_match_tolerance_inr": int(tolerance),
        }
        run_config["sizing"] = {**cfg["sizing"], "reference_capital": int(capital)}

        with st.spinner("Running backtest..."):
            result = run(
                run_config,
                options_path=DEFAULT_OPTIONS_PATH,
                output_dir=DEFAULT_OUTPUT_DIR,
            )

        summary = result["summary"]

        st.subheader("Summary")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Trades placed",
                  f"{summary['trades_placed']} / {summary['total_days_processed']}")
        m2.metric("Win rate", _fmt_pct(summary["win_rate"]))
        m3.metric("Total P&L", _fmt_money(summary["total_pnl_inr"]))
        m4.metric("Total return on capital", _fmt_pct(summary["total_return_pct"]))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Max drawdown", _fmt_money(summary["max_drawdown_inr"]),
                  delta=_fmt_pct(-summary["max_drawdown_pct"]),
                  delta_color="inverse")
        m6.metric("Max consec losses", str(summary["max_consecutive_losses"]))
        m7.metric("Best day", _fmt_money(summary["best_trade_inr"]))
        m8.metric("Worst day", _fmt_money(summary["worst_trade_inr"]))

        m9, m10, _, _ = st.columns(4)
        m9.metric("Mean P&L", _fmt_money(summary["mean_pnl_inr"]))
        m10.metric("Median P&L", _fmt_money(summary["median_pnl_inr"]))

        if summary["exit_reason_counts"]:
            st.write("**Exit reasons:**", summary["exit_reason_counts"])
        if summary["skip_reason_counts"]:
            st.write("**Skip reasons:**", summary["skip_reason_counts"])

        equity_df = pd.read_csv(result["equity_csv"])
        if not equity_df.empty:
            st.subheader("Equity curve")
            st.line_chart(equity_df.set_index("date")["equity_inr"])

            st.subheader("Drawdown")
            st.area_chart(equity_df.set_index("date")["drawdown_inr"])

        trades_df = pd.read_csv(result["trades_csv"])
        if not trades_df.empty:
            st.subheader("Trades")
            st.dataframe(trades_df, use_container_width=True)
        else:
            st.info("No trades generated in this window.")

        d1, d2 = st.columns(2)
        with open(result["trades_csv"], "rb") as f:
            d1.download_button(
                "Download trades CSV",
                data=f.read(),
                file_name=os.path.basename(result["trades_csv"]),
                mime="text/csv",
                key="zc_dl_trades",
            )
        with open(result["equity_csv"], "rb") as f:
            d2.download_button(
                "Download equity CSV",
                data=f.read(),
                file_name=os.path.basename(result["equity_csv"]),
                mime="text/csv",
                key="zc_dl_equity",
            )
```

- [ ] **Step 2: Smoke-test the import**

Run: `python3 -c "from ui.zero_credit_backtest_runner import render_zero_credit_backtest; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

Suggest:
```
git add ui/zero_credit_backtest_runner.py
git commit -m "feat(zero_credit): Streamlit UI runner"
```

---

### Task 13: Wire the runner into `app.py`

**Files:**
- Modify: `app.py`

`app.py` currently has 19 `st.tabs(...)` items. We add a 20th: "🧬 Zero Credit". Two edits: import the runner and add the tab.

- [ ] **Step 1: Add the import line**

Edit `app.py`. Find the existing line:
```python
from ui.debit_spread_backtest_runner import render_debit_spread_backtest
```
Insert immediately after it:
```python
from ui.zero_credit_backtest_runner import render_zero_credit_backtest
```

- [ ] **Step 2: Extend the `st.tabs([...])` call**

Find the line beginning with:
```python
tab_dashboard, tab_trades, tab_backtest, tab_forward, tab_dema_st, tab_st_ema, tab_straddle_vwap, tab_boom, tab_boom_st, tab_vwap_ema_rsi, tab_bb_reversal, tab_bb_reversal_pine, tab_bb_reversal_pine_exit, tab_prt, tab_ha_nr7, tab_ema5_fut, tab_gamma_blast, tab_st_low_band, tab_debit_spread = st.tabs([
```

Replace it with:
```python
tab_dashboard, tab_trades, tab_backtest, tab_forward, tab_dema_st, tab_st_ema, tab_straddle_vwap, tab_boom, tab_boom_st, tab_vwap_ema_rsi, tab_bb_reversal, tab_bb_reversal_pine, tab_bb_reversal_pine_exit, tab_prt, tab_ha_nr7, tab_ema5_fut, tab_gamma_blast, tab_st_low_band, tab_debit_spread, tab_zero_credit = st.tabs([
```

Inside the list-of-tab-labels (next several lines), find the trailing label `"🦋 Debit Spread",` and insert after it:
```python
    "🧬 Zero Credit",
```

- [ ] **Step 3: Add the tab body**

At the bottom of `app.py`, after:
```python
with tab_debit_spread:
    render_debit_spread_backtest()
```
Append:
```python

with tab_zero_credit:
    render_zero_credit_backtest()
```

- [ ] **Step 4: Smoke-test**

Run: `python3 -c "import ast; ast.parse(open('app.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 5: Manual UI verification**

Suggest the user start the app:
```
streamlit run app.py
```
Open the "🧬 Zero Credit" tab, click **Run backtest** with defaults, and verify:
- Summary block renders with non-zero "Trades placed".
- Equity curve and drawdown charts render.
- Trades table is non-empty.
- CSV download buttons work.

If anything is broken, stop and fix before declaring the task done.

- [ ] **Step 6: Commit**

Suggest:
```
git add app.py
git commit -m "feat(zero_credit): wire Streamlit tab into app.py"
```

---

### Task 14: Final sweep — full test suite + lint check

**Files:**
- (No file changes — verification step.)

- [ ] **Step 1: Run the full project test suite**

Run: `pytest -v`
Expected: all tests pass (existing + new). If any pre-existing test fails on `main`, note that and proceed; do not "fix" unrelated breakage.

- [ ] **Step 2: Visual review of the engine module**

Read `engine/zero_credit_backtest.py` end-to-end. Look for:
- Any `print()` left over from debugging.
- Any commented-out code.
- Any `TODO` / `FIXME` left in.

If anything is stale, remove it.

- [ ] **Step 3: Final commit (if any sweeps)**

Only if Step 2 produced changes, suggest:
```
git add engine/zero_credit_backtest.py
git commit -m "chore(zero_credit): post-implementation cleanup"
```

---

## Spec → Plan Coverage Map

| Spec section | Implemented in |
|---|---|
| §1 Overview | Task 2 (docstring) + entire engine |
| §2.1 Entry calendar | Task 8 (`_trading_days_from_df`, `run_backtest` loop) |
| §2.2 Entry time (09:20 open) | Task 7 (`_entry_slice`, `LegFill.entry_price`) |
| §2.3 Expiry filter `WEEK / 1` | Task 7 (`_entry_slice`), Task 7 (`_holding_slice`); test `test_filters_to_week_expiry_only` |
| §2.4 Strike selection (premium-based, tiebreakers, tolerance) | Task 3 (`pick_strike_by_premium`) |
| §2.5 Four legs | Task 7 (`LEG_DEFINITIONS` + run_one_day loop) |
| §2.6 Net debit | Task 5 (`compute_entry_economics`) |
| §3.1 TP scan (1-min) | Task 5 (`scan_for_tp_exit`), Task 7 (`run_one_day`) |
| §3.2 Time exit at 15:20 | Task 7 (`_time_exit_prices`) |
| §3.3 No SL | (no implementation — explicitly absent) |
| §3.4 Edge cases | Tasks 6, 7 (`build_bar_stream` carry-forward / force-exit; missing-bar walk-back); tests in 7 |
| §4 Daily equity curve | Task 9 (`build_equity_curve`) |
| §5 P&L | Task 7 (final block of `run_one_day`); Task 8 (`return_pct`) |
| §6 Sizing & capital | Task 7 (LegFill quantities), Task 8 (capital threading) |
| §7 Configuration | Task 1 (JSON), Task 8 (`parse_config`) |
| §8.1 Trades CSV | Task 9 (`trades_to_dataframe`, `write_trades_csv`) |
| §8.2 Equity CSV | Task 9 (`write_equity_csv`) |
| §8.3 Stdout summary | Task 9 (`summarize_metrics`, `print_summary`) — trimmed per user choice |
| §9 UI | Tasks 12, 13 |
| §10 Testing | Tests embedded in every task; integration in Task 10 + smoke in Task 11 |
| §11 File changes | Tasks 1, 2, 12, 13 cover all listed files |
| §12 Out of scope | (intentionally not implemented) |
| §13 Open items | Engine hard-codes `expiry_code=1`; if expiry-day rollover comes up in Task 11 smoke test, document the rule found and adjust |

---

## Self-Review Notes

1. **Spec coverage:** every spec section is mapped to a task above. The `expiry_code` open item is non-blocking — Task 11's smoke test will surface it on real data, and any adjustment is a small follow-up.
2. **Placeholder scan:** no `TBD`, no "implement later", no "similar to Task N". Each step has either complete code or an exact command.
3. **Type / name consistency:** `LegSpec`, `LegFill`, `ZeroCreditTrade`, `DayContext`, `PickResult`, `LEG_KEY_ORDER`, `LEG_DEFINITIONS`, `pick_strike_by_premium`, `resolve_atm_strike`, `compute_entry_economics`, `compute_mtm_inr`, `scan_for_tp_exit`, `build_bar_stream`, `run_one_day`, `parse_config`, `run_backtest`, `run`, `build_equity_curve`, `summarize_metrics`, `max_consecutive_losses`, `trades_to_dataframe`, `write_trades_csv`, `write_equity_csv`, `print_summary`. Each name appears consistently across its definition, usage, and tests.
4. **Skip reasons used:** `no_entry_bar`, `no_atm_row`, `no_strike_within_tolerance: <leg_key>`, `no_time_exit_bar`. Exit reasons: `TP`, `TIME`, `data_gap_force_exit`. These match the spec's vocabulary.
