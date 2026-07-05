# Debit Spread Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended for this user — see memory: "no subagents for plan execution"). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a calendar-driven NIFTY weekly options backtest that enters a 6-leg 1-3-2 broken-wing condor at 11:00 AM two trading days before each weekly expiry, exits at 1.5× net debit (intra-day 1-min check) or 15:25 close on expiry day.

**Architecture:** Single-file custom engine `engine/debit_spread_backtest.py` following the existing pattern (`gamma_blast_backtest.py`, `ha_nr7_backtest.py`). Pure helper functions for testability. Reads `data/options/nifty/NIFTY_OPTIONS_1m.parquet` directly. Outputs trades CSV + daily equity CSV. UI handler in `ui/backtest_runner.py`.

**Tech Stack:** Python 3, pandas, pytest, dataclasses. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-08-debit-spread-design.md`

**Git policy:** Per user's CLAUDE.md, the engineer must NOT run `git add`/`git commit` themselves. After each completed task, this plan provides a copy-paste commit message for the user to run manually.

---

## File Structure

| File | Status | Responsibility |
|------|--------|----------------|
| `saved_strategies/debit_spread.json` | NEW | Strategy config (entry rules, structure, exit, sizing) |
| `engine/debit_spread_backtest.py` | NEW | Engine: T−2 calendar, ATM lookup, leg fetcher, TP scanner, expiry exit, equity curve, metrics, CSV writers, `run(config)` entrypoint |
| `tests/test_debit_spread.py` | NEW | Unit + integration tests |
| `ui/backtest_runner.py` | MODIFY | Add Debit Spread strategy handler |
| `ui/strategy_form.py` | MODIFY | Add `debit_spread` to strategy-type list (if applicable) |

---

## Task 1: Strategy Config JSON

**Files:**
- Create: `saved_strategies/debit_spread.json`

- [ ] **Step 1: Write the config file**

```json
{
  "name": "debit_spread",
  "strategy_type": "debit_spread",
  "instruments": ["NIFTY"],
  "entry": {
    "days_before_expiry": 2,
    "entry_time": "11:00"
  },
  "structure": {
    "ce_legs": [
      { "side": "BUY",  "lots": 1, "strike_offset": -1 },
      { "side": "SELL", "lots": 3, "strike_offset":  4 },
      { "side": "BUY",  "lots": 2, "strike_offset":  5 }
    ],
    "pe_legs": [
      { "side": "BUY",  "lots": 1, "strike_offset":  1 },
      { "side": "SELL", "lots": 3, "strike_offset": -4 },
      { "side": "BUY",  "lots": 2, "strike_offset": -5 }
    ]
  },
  "exit": {
    "tp_multiple_of_max_loss": 1.5,
    "expiry_squareoff_time": "15:25",
    "data_gap_force_exit_minutes": 30
  },
  "sizing": {
    "sets_per_trade": 1,
    "reference_capital": 300000
  },
  "metrics": {
    "risk_free_rate": 0.06,
    "annualization_factor": 52
  },
  "backtest_start": "2025-01-01",
  "backtest_end":   "2026-05-05"
}
```

- [ ] **Step 2: Verify JSON parses**

Run: `python3 -c "import json; print(json.load(open('saved_strategies/debit_spread.json'))['name'])"`
Expected output: `debit_spread`

- [ ] **Step 3: Suggest commit**

Tell user:
> Commit message:
> ```
> chore: add debit_spread strategy config
> ```

---

## Task 2: Engine skeleton + dataclasses

**Files:**
- Create: `engine/debit_spread_backtest.py`
- Create: `tests/test_debit_spread.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_debit_spread.py
"""Tests for debit_spread backtest engine."""
from dataclasses import asdict

import pandas as pd
import pytest

from engine.debit_spread_backtest import (
    LegSpec,
    DebitSpreadTrade,
    trades_to_dataframe,
)


class TestDataclasses:
    def test_legspec_holds_offset_and_lots(self):
        spec = LegSpec(option_type="CE", side="BUY", lots=1, strike_offset=-1)
        assert spec.option_type == "CE"
        assert spec.side == "BUY"
        assert spec.lots == 1
        assert spec.strike_offset == -1

    def test_trade_to_dataframe_empty(self):
        df = trades_to_dataframe([])
        assert df.empty

    def test_trade_to_dataframe_one_row(self):
        trade = DebitSpreadTrade(
            expiry_date="2025-06-17",
            entry_date="2025-06-13",
            entry_time="11:00",
            atm_strike=24500.0,
            spot_at_entry=24512.5,
            net_debit_pts=80.0,
            net_debit_inr=5200.0,
            tp_target_inr=7800.0,
            exit_time="15:25",
            exit_reason="EXPIRY",
            pnl_pts=15.0,
            pnl_inr=975.0,
            return_pct=0.00325,
            running_equity_inr=300975.0,
            skip_reason=None,
            legs={},
        )
        df = trades_to_dataframe([trade])
        assert len(df) == 1
        assert df.iloc[0]["exit_reason"] == "EXPIRY"
```

- [ ] **Step 2: Run test, verify it fails**

Run: `pytest tests/test_debit_spread.py::TestDataclasses -v`
Expected: ImportError on `engine.debit_spread_backtest`.

- [ ] **Step 3: Create the engine module skeleton**

```python
# engine/debit_spread_backtest.py
"""
Debit Spread Backtest Engine — 1-3-2 Broken-Wing Condor on NIFTY weeklies.

Strategy:
  Calendar-driven. Two trading days before every NIFTY weekly expiry, at
  11:00 AM, enter a 6-leg combined CE+PE 1-3-2 ratio structure. Exit at
  1.5x net debit (intra-day 1-min check) or 15:25 close on expiry day.
  No stop loss. See docs/superpowers/specs/2026-05-08-debit-spread-design.md.
"""

import logging
from dataclasses import asdict, dataclass, field
from datetime import date as _date, datetime as _datetime, time as _time, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class LegSpec:
    """A single leg's static spec (independent of any specific trade)."""
    option_type: str       # "CE" | "PE"
    side: str              # "BUY" | "SELL"
    lots: int
    strike_offset: int


@dataclass
class LegFill:
    """Resolved leg at a specific trade: actual strike + entry/exit prices."""
    option_type: str
    side: str
    lots: int
    strike_offset: int
    strike: float
    entry_price: float
    exit_price: Optional[float] = None


@dataclass
class DebitSpreadTrade:
    expiry_date: str
    entry_date: str
    entry_time: str
    atm_strike: float
    spot_at_entry: float

    net_debit_pts: float
    net_debit_inr: float
    tp_target_inr: float

    exit_time: str
    exit_reason: str          # "TP" | "EXPIRY" | "data_gap_force_exit"
    pnl_pts: float
    pnl_inr: float
    return_pct: float
    running_equity_inr: float

    skip_reason: Optional[str]
    legs: Dict[str, LegFill] = field(default_factory=dict)


def trades_to_dataframe(trades: List[DebitSpreadTrade]) -> pd.DataFrame:
    """Flatten trades (including per-leg strikes/prices) into a DataFrame."""
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
```

- [ ] **Step 4: Run test, verify it passes**

Run: `pytest tests/test_debit_spread.py::TestDataclasses -v`
Expected: 3 passed.

- [ ] **Step 5: Suggest commit**

Tell user:
> Commit message:
> ```
> feat(debit_spread): scaffold engine module + dataclasses
> ```

---

## Task 3: T−2 trading-day computation

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_debit_spread.py`:

```python
from datetime import date

from engine.debit_spread_backtest import compute_entry_date


class TestComputeEntryDate:
    """T−2 trading days off the (possibly shifted) expiry date."""

    def test_regular_tuesday_expiry(self):
        # Tue 2025-06-17 → entry Fri 2025-06-13
        trading_days = sorted({
            date(2025, 6, 9), date(2025, 6, 10), date(2025, 6, 11),
            date(2025, 6, 12), date(2025, 6, 13),
            date(2025, 6, 16), date(2025, 6, 17),
        })
        assert compute_entry_date(date(2025, 6, 17), trading_days, 2) == date(2025, 6, 13)

    def test_holiday_in_between(self):
        # Expiry Tue 2025-08-19, holiday Mon 2025-08-18 → entry skips Mon → Fri 2025-08-15
        trading_days = sorted({
            date(2025, 8, 13), date(2025, 8, 14), date(2025, 8, 15),
            date(2025, 8, 19),  # 2025-08-18 missing (holiday)
        })
        assert compute_entry_date(date(2025, 8, 19), trading_days, 2) == date(2025, 8, 14)

    def test_shifted_monday_expiry(self):
        # Expiry shifted to Mon 2026-10-19 (Tuesday Dussehra) → entry Thu 2026-10-15
        trading_days = sorted({
            date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15),
            date(2026, 10, 16), date(2026, 10, 19),
        })
        assert compute_entry_date(date(2026, 10, 19), trading_days, 2) == date(2026, 10, 15)

    def test_expiry_not_in_trading_days_raises(self):
        trading_days = [date(2025, 6, 13), date(2025, 6, 16)]
        with pytest.raises(ValueError):
            compute_entry_date(date(2025, 6, 17), trading_days, 2)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestComputeEntryDate -v`
Expected: ImportError on `compute_entry_date`.

- [ ] **Step 3: Implement `compute_entry_date`**

Append to `engine/debit_spread_backtest.py`:

```python
def compute_entry_date(
    expiry_date: _date,
    trading_days: List[_date],
    days_before: int,
) -> _date:
    """Return the date that is `days_before` trading days before `expiry_date`.

    `trading_days` must contain `expiry_date` itself. We walk back through the
    sorted trading-day list. Holiday-shifted expiries are handled because the
    shifted date is what's passed in; we just need at least `days_before`
    earlier trading days available.
    """
    sorted_days = sorted(set(trading_days))
    if expiry_date not in sorted_days:
        raise ValueError(f"Expiry {expiry_date} not in trading_days list")
    idx = sorted_days.index(expiry_date)
    if idx - days_before < 0:
        raise ValueError(
            f"Not enough trading days before {expiry_date} "
            f"(need {days_before}, have {idx})"
        )
    return sorted_days[idx - days_before]
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_debit_spread.py::TestComputeEntryDate -v`
Expected: 4 passed.

- [ ] **Step 5: Suggest commit**

Tell user:
> Commit message:
> ```
> feat(debit_spread): add T-2 trading-day computation
> ```

---

## Task 4: ATM strike resolution at a timestamp

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Write the failing tests**

Append:

```python
from engine.debit_spread_backtest import resolve_atm_strike


class TestResolveAtmStrike:
    def test_picks_moneyness_atm_row(self):
        df = pd.DataFrame([
            {"strike": 24500.0, "moneyness": "ATM", "spot": 24512.0,
             "option_type": "CE", "expiry_code": 1},
            {"strike": 24450.0, "moneyness": "ITM", "spot": 24512.0,
             "option_type": "CE", "expiry_code": 1},
        ])
        atm, spot = resolve_atm_strike(df)
        assert atm == 24500.0
        assert spot == 24512.0

    def test_multiple_atm_picks_closest_to_spot(self):
        df = pd.DataFrame([
            {"strike": 24450.0, "moneyness": "ATM", "spot": 24512.0,
             "option_type": "CE", "expiry_code": 1},
            {"strike": 24500.0, "moneyness": "ATM", "spot": 24512.0,
             "option_type": "CE", "expiry_code": 1},
        ])
        atm, _ = resolve_atm_strike(df)
        assert atm == 24500.0  # |24500-24512|=12 vs |24450-24512|=62

    def test_no_atm_returns_none(self):
        df = pd.DataFrame([
            {"strike": 24450.0, "moneyness": "ITM", "spot": 24512.0,
             "option_type": "CE", "expiry_code": 1},
        ])
        atm, spot = resolve_atm_strike(df)
        assert atm is None
        assert spot is None
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestResolveAtmStrike -v`
Expected: ImportError.

- [ ] **Step 3: Implement `resolve_atm_strike`**

Append to engine:

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

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_debit_spread.py::TestResolveAtmStrike -v`
Expected: 3 passed.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): resolve ATM strike via moneyness tag
> ```

---

## Task 5: Leg fetcher (six rows at entry timestamp)

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
from engine.debit_spread_backtest import (
    fetch_legs_at,
    LEG_KEY_ORDER,
    DEFAULT_LEG_SPECS,
)


def _make_leg_row(strike, offset, opt_type, open_=10.0, close=11.0,
                  high=12.0, low=9.0, expiry_code=1, moneyness="OTM",
                  ts="2025-06-13T11:00:00+05:30"):
    return {
        "datetime": ts, "underlying": "NIFTY",
        "option_type": opt_type, "expiry_code": expiry_code,
        "strike": strike, "atm_strike": 24500.0,
        "strike_offset": offset, "moneyness": moneyness,
        "spot": 24512.0, "open": open_, "high": high, "low": low,
        "close": close, "volume": 1000, "oi": 5000, "iv": 15.0,
    }


class TestFetchLegsAt:
    def test_all_six_legs_present(self):
        rows = []
        # CE legs at offsets -1, +4, +5
        for off, mny, op in [(-1, "ITM", 120.0), (4, "OTM", 35.0), (5, "OTM", 22.0)]:
            rows.append(_make_leg_row(24500 + off*50, off, "CE",
                                      open_=op, moneyness=mny))
        # PE legs at offsets +1, -4, -5
        for off, mny, op in [(1, "ITM", 115.0), (-4, "OTM", 32.0), (-5, "OTM", 20.0)]:
            rows.append(_make_leg_row(24500 + off*50, off, "PE",
                                      open_=op, moneyness=mny))
        df = pd.DataFrame(rows)

        legs, missing = fetch_legs_at(df, DEFAULT_LEG_SPECS)
        assert missing == []
        assert set(legs.keys()) == set(LEG_KEY_ORDER)
        assert legs["ce_itm"].entry_price == 120.0
        assert legs["ce_short"].entry_price == 35.0
        assert legs["ce_far"].entry_price == 22.0
        assert legs["pe_itm"].entry_price == 115.0
        assert legs["pe_short"].entry_price == 32.0
        assert legs["pe_far"].entry_price == 20.0

    def test_missing_one_leg_reported(self):
        rows = []
        # only 5 of 6 — drop pe_far
        for off, mny, op in [(-1, "ITM", 120.0), (4, "OTM", 35.0), (5, "OTM", 22.0)]:
            rows.append(_make_leg_row(24500 + off*50, off, "CE", open_=op, moneyness=mny))
        for off, mny, op in [(1, "ITM", 115.0), (-4, "OTM", 32.0)]:
            rows.append(_make_leg_row(24500 + off*50, off, "PE", open_=op, moneyness=mny))
        df = pd.DataFrame(rows)

        legs, missing = fetch_legs_at(df, DEFAULT_LEG_SPECS)
        assert missing == ["pe_far"]

    def test_empty_slice_all_missing(self):
        df = pd.DataFrame()
        legs, missing = fetch_legs_at(df, DEFAULT_LEG_SPECS)
        assert set(missing) == set(LEG_KEY_ORDER)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestFetchLegsAt -v`
Expected: ImportError.

- [ ] **Step 3: Implement `fetch_legs_at` + leg-key constants**

Append to engine:

```python
LEG_KEY_ORDER = [
    "ce_itm", "ce_short", "ce_far",
    "pe_itm", "pe_short", "pe_far",
]

DEFAULT_LEG_SPECS: Dict[str, LegSpec] = {
    "ce_itm":   LegSpec(option_type="CE", side="BUY",  lots=1, strike_offset=-1),
    "ce_short": LegSpec(option_type="CE", side="SELL", lots=3, strike_offset=4),
    "ce_far":   LegSpec(option_type="CE", side="BUY",  lots=2, strike_offset=5),
    "pe_itm":   LegSpec(option_type="PE", side="BUY",  lots=1, strike_offset=1),
    "pe_short": LegSpec(option_type="PE", side="SELL", lots=3, strike_offset=-4),
    "pe_far":   LegSpec(option_type="PE", side="BUY",  lots=2, strike_offset=-5),
}


def fetch_legs_at(
    slice_df: pd.DataFrame,
    leg_specs: Dict[str, LegSpec],
) -> Tuple[Dict[str, LegFill], List[str]]:
    """Resolve each leg in `leg_specs` against `slice_df` (a single timestamp).

    Returns (legs, missing) where:
      - legs is keyed by leg_key (subset of leg_specs); each LegFill has its
        entry_price set to the row's `open`.
      - missing is the list of leg_keys we couldn't resolve.
    """
    legs: Dict[str, LegFill] = {}
    missing: List[str] = []
    if slice_df.empty:
        return {}, list(leg_specs.keys())

    for leg_key, spec in leg_specs.items():
        match = slice_df[
            (slice_df["option_type"] == spec.option_type)
            & (slice_df["strike_offset"] == spec.strike_offset)
        ]
        if match.empty:
            missing.append(leg_key)
            continue
        row = match.iloc[0]
        legs[leg_key] = LegFill(
            option_type=spec.option_type,
            side=spec.side,
            lots=spec.lots,
            strike_offset=spec.strike_offset,
            strike=float(row["strike"]),
            entry_price=float(row["open"]),
        )
    return legs, missing
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_debit_spread.py::TestFetchLegsAt -v`
Expected: 3 passed.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): fetch six legs by (option_type, strike_offset)
> ```

---

## Task 6: Net debit + TP target computation

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
from engine.debit_spread_backtest import (
    compute_entry_economics,
    LOT_SIZE_NIFTY,
)


def _legs_with_prices(prices: dict) -> Dict[str, "LegFill"]:
    from engine.debit_spread_backtest import LegFill, DEFAULT_LEG_SPECS
    legs = {}
    for k, spec in DEFAULT_LEG_SPECS.items():
        legs[k] = LegFill(
            option_type=spec.option_type, side=spec.side, lots=spec.lots,
            strike_offset=spec.strike_offset,
            strike=24500 + spec.strike_offset * 50,
            entry_price=prices[k],
        )
    return legs


class TestComputeEntryEconomics:
    def test_typical_debit_case(self):
        # spec § 2.5 example: net_debit_pts ≈ 80
        prices = {
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        }
        # entry_cost = -(1*120) + (3*35) + -(2*22) + -(1*115) + (3*32) + -(2*20)
        #            = -120 + 105 - 44 - 115 + 96 - 40 = -118
        # net_debit_pts = 118 ; net_debit_inr = 118 * 65 = 7670
        # tp_target_inr = 7670 * 1.5 = 11505
        legs = _legs_with_prices(prices)
        net_pts, net_inr, tp = compute_entry_economics(legs, tp_multiple=1.5)
        assert net_pts == pytest.approx(118.0)
        assert net_inr == pytest.approx(118.0 * LOT_SIZE_NIFTY)
        assert tp == pytest.approx(118.0 * LOT_SIZE_NIFTY * 1.5)

    def test_credit_case_clamps_tp_to_zero(self):
        # heavy credit: prices set so SELLs > BUYs
        prices = {
            "ce_itm": 10.0, "ce_short": 50.0, "ce_far": 5.0,
            "pe_itm": 10.0, "pe_short": 50.0, "pe_far": 5.0,
        }
        legs = _legs_with_prices(prices)
        net_pts, net_inr, tp = compute_entry_economics(legs, tp_multiple=1.5)
        assert net_pts < 0
        assert net_inr < 0
        assert tp == 0.0
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestComputeEntryEconomics -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Append to engine (near top, after imports — but constants stay near top of file):

```python
LOT_SIZE_NIFTY = 65  # Mirrors config.LOT_SIZE['NIFTY']; pinned for unit tests.


def _leg_signed_value(leg: LegFill, price: float) -> float:
    """+lots*price for BUY, -lots*price for SELL."""
    sign = 1 if leg.side == "BUY" else -1
    return sign * leg.lots * price


def compute_entry_economics(
    legs: Dict[str, LegFill],
    tp_multiple: float,
    lot_size: int = LOT_SIZE_NIFTY,
) -> Tuple[float, float, float]:
    """Return (net_debit_pts, net_debit_inr, tp_target_inr).

    With sign=+1 for BUY and sign=-1 for SELL, sum(sign*lots*price) equals
    (total paid for longs) - (total received from shorts) = net debit directly.
    Positive = debit (cash out); negative = credit (cash in).
    tp_target = max(0, net_debit_inr) * tp_multiple.
    """
    net_debit_pts = sum(_leg_signed_value(leg, leg.entry_price) for leg in legs.values())
    net_debit_inr = net_debit_pts * lot_size
    tp_target_inr = max(0.0, net_debit_inr) * tp_multiple
    return net_debit_pts, net_debit_inr, tp_target_inr
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_debit_spread.py::TestComputeEntryEconomics -v`
Expected: 2 passed.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): compute net debit and TP target
> ```

---

## Task 7: MTM at a single bar + intra-day TP scanner

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
from engine.debit_spread_backtest import (
    compute_mtm_inr,
    scan_for_tp_exit,
)


class TestComputeMtmInr:
    def test_mtm_zero_at_entry_prices(self):
        legs = _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })
        # At entry prices, MTM should be exactly 0 vs net_debit.
        net_pts, net_inr, _ = compute_entry_economics(legs, 1.5)
        # current price map = entry prices
        prices_now = {k: l.entry_price for k, l in legs.items()}
        mtm = compute_mtm_inr(legs, prices_now, net_debit_pts=net_pts)
        assert mtm == pytest.approx(0.0)

    def test_mtm_rises_when_long_legs_appreciate(self):
        legs = _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })
        net_pts, _, _ = compute_entry_economics(legs, 1.5)
        prices_now = {
            "ce_itm": 200.0, "ce_short": 35.0, "ce_far": 22.0,  # +80 on long ITM
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        }
        mtm = compute_mtm_inr(legs, prices_now, net_debit_pts=net_pts)
        # +1 lot * (+80 pts) = +80 pts ; * 65 = 5200
        assert mtm == pytest.approx(80.0 * LOT_SIZE_NIFTY)
```

```python
class TestScanForTpExit:
    def _bars(self, leg_keys, n_bars, fn_prices):
        """Build a list of {datetime: ts, leg_prices: {key: price}} bars."""
        bars = []
        for i in range(n_bars):
            ts = pd.Timestamp("2025-06-13T11:01:00+05:30") + pd.Timedelta(minutes=i)
            bars.append({"datetime": ts, "prices": fn_prices(i)})
        return bars

    def test_tp_fires_when_mtm_crosses_target(self):
        legs = _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })
        net_pts, net_inr, tp = compute_entry_economics(legs, 1.5)

        # bar 0: MTM ≈ 0 ; bar 1: pop CE ITM by huge amount ⇒ MTM well above tp
        def prices_at(i):
            base = {k: l.entry_price for k, l in legs.items()}
            if i >= 1:
                base["ce_itm"] += 500.0
            return base

        bars = self._bars(LEG_KEY_ORDER, n_bars=3, fn_prices=prices_at)
        result = scan_for_tp_exit(legs, bars, net_debit_pts=net_pts, tp_target_inr=tp)
        assert result is not None
        exit_ts, exit_prices, mtm_at_exit = result
        assert exit_ts == bars[1]["datetime"]
        assert mtm_at_exit >= tp

    def test_tp_never_fires_returns_none(self):
        legs = _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })
        net_pts, _, tp = compute_entry_economics(legs, 1.5)

        def flat_prices(i):
            return {k: l.entry_price for k, l in legs.items()}
        bars = self._bars(LEG_KEY_ORDER, n_bars=10, fn_prices=flat_prices)
        result = scan_for_tp_exit(legs, bars, net_debit_pts=net_pts, tp_target_inr=tp)
        assert result is None

    def test_credit_case_exits_at_first_positive_mtm(self):
        # construct a credit-entry leg set
        legs = _legs_with_prices({
            "ce_itm": 10.0, "ce_short": 50.0, "ce_far": 5.0,
            "pe_itm": 10.0, "pe_short": 50.0, "pe_far": 5.0,
        })
        net_pts, _, tp = compute_entry_economics(legs, 1.5)
        assert tp == 0.0  # credit case

        # bar 0: at-entry (mtm=0, NOT strictly > 0), bar 1: short PE drops a tick ⇒ mtm > 0
        def prices_at(i):
            base = {k: l.entry_price for k, l in legs.items()}
            if i >= 1:
                base["pe_short"] -= 1.0  # short price down → SELL leg gains
            return base

        bars = self._bars(LEG_KEY_ORDER, n_bars=3, fn_prices=prices_at)
        result = scan_for_tp_exit(legs, bars, net_debit_pts=net_pts, tp_target_inr=tp)
        assert result is not None
        exit_ts, _, mtm = result
        assert exit_ts == bars[1]["datetime"]
        assert mtm > 0
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestComputeMtmInr tests/test_debit_spread.py::TestScanForTpExit -v`
Expected: ImportError.

- [ ] **Step 3: Implement `compute_mtm_inr` and `scan_for_tp_exit`**

Append to engine:

```python
def compute_mtm_inr(
    legs: Dict[str, LegFill],
    current_prices: Dict[str, float],
    net_debit_pts: float,
    lot_size: int = LOT_SIZE_NIFTY,
) -> float:
    """Unrealized P&L vs entry given current per-leg prices.

    With sign=+1 for BUY and -1 for SELL: signed_sum(prices) is the position
    value at those prices. Since net_debit_pts = signed_sum(entry_prices) — i.e.,
    entry_cost_pts directly — we have:
        mtm_pts = signed_now - net_debit_pts
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

    `bars` is an iterable of dicts, each with:
      - 'datetime': pd.Timestamp
      - 'prices': Dict[leg_key, float]   (close of the bar for each leg)

    Credit case (tp_target == 0): exit at the first bar with mtm STRICTLY > 0.
    Debit case:                  exit at the first bar with mtm >= tp_target.

    Returns None if no bar satisfies the trigger.
    """
    is_credit_case = tp_target_inr == 0.0
    for bar in bars:
        prices = bar["prices"]
        if any(k not in prices for k in legs):
            continue  # incomplete bar — skip silently (data-gap handling elsewhere)
        mtm = compute_mtm_inr(legs, prices, net_debit_pts, lot_size)
        if is_credit_case:
            if mtm > 0:
                return bar["datetime"], dict(prices), mtm
        else:
            if mtm >= tp_target_inr:
                return bar["datetime"], dict(prices), mtm
    return None
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_debit_spread.py::TestComputeMtmInr tests/test_debit_spread.py::TestScanForTpExit -v`
Expected: 5 passed.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): MTM and intra-day TP scanner
> ```

---

## Task 8: Bar-stream builder from parquet slice

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

This task converts a wide-format parquet slice (rows = leg minutes) into the bar-stream format that `scan_for_tp_exit` expects (one dict per minute with all leg prices). It also handles ≤ 30-min carry-forward.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime
from engine.debit_spread_backtest import build_bar_stream


def _row(ts, opt_type, strike_offset, close):
    return {
        "datetime": ts, "underlying": "NIFTY",
        "option_type": opt_type, "expiry_code": 1,
        "strike": 24500 + strike_offset * 50, "atm_strike": 24500.0,
        "strike_offset": strike_offset, "moneyness": "OTM",
        "spot": 24500.0, "open": close, "high": close,
        "low": close, "close": close, "volume": 1, "oi": 1, "iv": 15.0,
    }


class TestBuildBarStream:
    def _legs(self):
        return _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })

    def test_two_clean_bars(self):
        legs = self._legs()
        rows = []
        for i, ts in enumerate([
            "2025-06-13T11:01:00+05:30", "2025-06-13T11:02:00+05:30"]):
            for k, leg in legs.items():
                rows.append(_row(ts, leg.option_type, leg.strike_offset, 100 + i))
        df = pd.DataFrame(rows)
        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        assert len(bars) == 2
        assert all(set(b["prices"]) == set(LEG_KEY_ORDER) for b in bars)
        assert bars[0]["prices"]["ce_itm"] == 100
        assert bars[1]["prices"]["ce_itm"] == 101

    def test_short_gap_carry_forwards(self):
        legs = self._legs()
        # bar at 11:01 has all legs; bar at 11:02 missing ce_itm; bar at 11:03 full
        rows = []
        for k, leg in legs.items():
            rows.append(_row("2025-06-13T11:01:00+05:30", leg.option_type,
                             leg.strike_offset, 100))
        for k, leg in legs.items():
            if k == "ce_itm":
                continue
            rows.append(_row("2025-06-13T11:02:00+05:30", leg.option_type,
                             leg.strike_offset, 101))
        for k, leg in legs.items():
            rows.append(_row("2025-06-13T11:03:00+05:30", leg.option_type,
                             leg.strike_offset, 102))
        df = pd.DataFrame(rows)
        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        assert len(bars) == 3
        # 11:02 ce_itm carried forward from 11:01 (=100); other legs at 101
        assert bars[1]["prices"]["ce_itm"] == 100
        assert bars[1]["prices"]["ce_short"] == 101

    def test_long_gap_emits_force_exit_marker(self):
        legs = self._legs()
        # 11:01 full; ce_itm absent for 31+ minutes (11:02..11:32 = 31 bars
        # without ce_itm). At 11:32 the gap is 31 min > 30 → force_exit fires.
        rows = []
        for k, leg in legs.items():
            rows.append(_row("2025-06-13T11:01:00+05:30", leg.option_type,
                             leg.strike_offset, 100))
        for m in range(2, 33):  # 11:02..11:32 inclusive (31 minutes)
            ts = f"2025-06-13T11:{m:02d}:00+05:30"
            for k, leg in legs.items():
                if k == "ce_itm":
                    continue
                rows.append(_row(ts, leg.option_type, leg.strike_offset, 101))
        df = pd.DataFrame(rows)

        bars = list(build_bar_stream(df, legs, max_gap_minutes=30))
        force_bars = [b for b in bars if b.get("force_exit")]
        assert len(force_bars) == 1, "expected exactly one force_exit marker"
        fb = force_bars[0]
        # Force exit yields the LAST FULLY-OBSERVED bar (11:01) with all 6 legs
        assert set(fb["prices"]) == set(LEG_KEY_ORDER)
        assert fb["prices"]["ce_itm"] == 100  # carried from 11:01
        # And iteration stops there
        assert bars[-1] is fb
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestBuildBarStream -v`
Expected: ImportError.

- [ ] **Step 3: Implement `build_bar_stream`**

Append to engine:

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
    not the trigger bar — that's the last MTM we can compute reliably. Iteration
    stops after emitting force_exit.
    """
    if df.empty:
        return

    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    leg_lookup = {(l.option_type, int(l.strike_offset)): k for k, l in legs.items()}
    df = df.sort_values("datetime")

    last_seen: Dict[str, Tuple[pd.Timestamp, float]] = {}
    last_full_bar: Optional[Tuple[pd.Timestamp, Dict[str, float]]] = None

    for ts, grp in df.groupby("datetime"):
        for _, row in grp.iterrows():
            key = leg_lookup.get((row["option_type"], int(row["strike_offset"])))
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
                # No full bar ever observed — nothing to MTM against.
                return
            last_ts, last_prices = last_full_bar
            yield {"datetime": last_ts, "prices": dict(last_prices), "force_exit": True}
            return

        last_full_bar = (ts, dict(prices))
        yield {"datetime": ts, "prices": prices, "force_exit": False}
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_debit_spread.py::TestBuildBarStream -v`
Expected: 3 passed.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): bar-stream builder with carry-forward
> ```

---

## Task 9: Single-week orchestrator

Combines T−2 entry, ATM resolution, leg fetch, economics, scan, expiry exit.

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
from engine.debit_spread_backtest import run_one_week, WeekContext


def _build_synthetic_week_df():
    """Build a tiny synthetic option dataset for a single week.

    Friday 2025-06-13 (entry day) and Tuesday 2025-06-17 (expiry day).
    11:00 entry. Premiums chosen so net is a debit and TP fires on expiry day.
    """
    rows = []

    # Friday 11:00:00 — six legs at known prices (debit case)
    entry_ts = "2025-06-13T11:00:00+05:30"
    legs_at_entry = [
        ("CE", -1, "ITM", 120.0),
        ("CE",  4, "OTM",  35.0),
        ("CE",  5, "OTM",  22.0),
        ("PE",  1, "ITM", 115.0),
        ("PE", -4, "OTM",  32.0),
        ("PE", -5, "OTM",  20.0),
    ]
    for opt, off, mny, op in legs_at_entry:
        rows.append({
            "datetime": entry_ts, "underlying": "NIFTY",
            "option_type": opt, "expiry_code": 1,
            "strike": 24500.0 + off * 50, "atm_strike": 24500.0,
            "strike_offset": off, "moneyness": mny,
            "spot": 24512.0, "open": op, "high": op, "low": op,
            "close": op, "volume": 1, "oi": 1, "iv": 15.0,
        })

    # Sprinkle bars from 11:01 Fri through 15:25 Tue.
    # Keep prices flat → TP never fires → exit at 15:25 expiry.
    for day in ["2025-06-13", "2025-06-16", "2025-06-17"]:
        for h in range(11, 16):
            for m in range(0, 60, 15):
                if day == "2025-06-13" and h == 11 and m == 0:
                    continue  # entry already added
                if day == "2025-06-17" and (h > 15 or (h == 15 and m > 25)):
                    continue
                ts = f"{day}T{h:02d}:{m:02d}:00+05:30"
                for opt, off, mny, op in legs_at_entry:
                    rows.append({
                        "datetime": ts, "underlying": "NIFTY",
                        "option_type": opt, "expiry_code": 1,
                        "strike": 24500.0 + off * 50, "atm_strike": 24500.0,
                        "strike_offset": off, "moneyness": mny,
                        "spot": 24512.0, "open": op, "high": op,
                        "low": op, "close": op, "volume": 1, "oi": 1, "iv": 15.0,
                    })
    return pd.DataFrame(rows)


class TestRunOneWeek:
    def test_flat_market_exits_at_expiry_with_zero_pnl(self):
        df = _build_synthetic_week_df()
        ctx = WeekContext(
            expiry_date=date(2025, 6, 17),
            entry_date=date(2025, 6, 13),
            entry_time_str="11:00",
            expiry_squareoff_time_str="15:25",
            tp_multiple=1.5,
            data_gap_force_exit_minutes=30,
            leg_specs=DEFAULT_LEG_SPECS,
        )
        trade = run_one_week(df, ctx)
        assert trade is not None
        assert trade.skip_reason is None
        assert trade.exit_reason == "EXPIRY"
        assert trade.pnl_inr == pytest.approx(0.0, abs=1e-6)

    def test_skip_when_entry_bar_missing(self):
        df = _build_synthetic_week_df()
        df = df[df["datetime"] != "2025-06-13T11:00:00+05:30"]
        ctx = WeekContext(
            expiry_date=date(2025, 6, 17),
            entry_date=date(2025, 6, 13),
            entry_time_str="11:00",
            expiry_squareoff_time_str="15:25",
            tp_multiple=1.5,
            data_gap_force_exit_minutes=30,
            leg_specs=DEFAULT_LEG_SPECS,
        )
        trade = run_one_week(df, ctx)
        assert trade is not None
        assert trade.skip_reason is not None
        assert "no_entry_bar" in trade.skip_reason or "missing_strike" in trade.skip_reason
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestRunOneWeek -v`
Expected: ImportError.

- [ ] **Step 3: Implement `WeekContext` and `run_one_week`**

Append to engine:

```python
@dataclass
class WeekContext:
    expiry_date: _date
    entry_date: _date
    entry_time_str: str                       # "HH:MM"
    expiry_squareoff_time_str: str            # "HH:MM"
    tp_multiple: float
    data_gap_force_exit_minutes: int
    leg_specs: Dict[str, LegSpec]
    lot_size: int = LOT_SIZE_NIFTY


def _make_skip_trade(ctx: WeekContext, reason: str) -> DebitSpreadTrade:
    return DebitSpreadTrade(
        expiry_date=ctx.expiry_date.isoformat(),
        entry_date=ctx.entry_date.isoformat(),
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
        running_equity_inr=float("nan"),  # filled in by caller
        skip_reason=reason,
        legs={},
    )


def _entry_slice(df: pd.DataFrame, ctx: WeekContext) -> pd.DataFrame:
    """1-min slice at entry timestamp on entry_date for expiry_code==1."""
    target_ts = f"{ctx.entry_date.isoformat()}T{ctx.entry_time_str}:00+05:30"
    sub = df[
        (df["datetime"] == target_ts)
        & (df["expiry_code"] == 1)
    ]
    return sub


def _holding_slice(df: pd.DataFrame, ctx: WeekContext, legs: Dict[str, LegFill]) -> pd.DataFrame:
    """All bars after entry up through expiry squareoff timestamp, restricted
    to the locked strikes. Vectorized for performance — no df.apply over rows.
    """
    entry_ts = f"{ctx.entry_date.isoformat()}T{ctx.entry_time_str}:00+05:30"
    expiry_ts = f"{ctx.expiry_date.isoformat()}T{ctx.expiry_squareoff_time_str}:00+05:30"

    # First narrow by time + expiry_code — keeps subsequent ops fast.
    sub = df[
        (df["datetime"] > entry_ts)
        & (df["datetime"] <= expiry_ts)
        & (df["expiry_code"] == 1)
    ]
    if sub.empty:
        return sub

    locked_pairs = {(l.option_type, int(l.strike_offset)) for l in legs.values()}
    pairs = list(zip(sub["option_type"].astype(str), sub["strike_offset"].astype(int)))
    mask = [p in locked_pairs for p in pairs]
    return sub[mask].sort_values("datetime")


def _expiry_squareoff_prices(
    holding_df: pd.DataFrame,
    ctx: WeekContext,
    legs: Dict[str, LegFill],
) -> Optional[Tuple[pd.Timestamp, Dict[str, float]]]:
    """Find the latest available bar on expiry_date at-or-before expiry_squareoff_time
    that has ALL six legs. Return (ts, leg_prices) or None."""
    expiry_iso = ctx.expiry_date.isoformat()
    deadline_ts = pd.Timestamp(f"{expiry_iso}T{ctx.expiry_squareoff_time_str}:00+05:30")
    expiry_bars = holding_df[
        (pd.to_datetime(holding_df["datetime"]) <= deadline_ts)
        & (pd.to_datetime(holding_df["datetime"]).dt.date == ctx.expiry_date)
    ].sort_values("datetime", ascending=False)

    leg_lookup = {(l.option_type, int(l.strike_offset)): k for k, l in legs.items()}
    seen_per_minute: Dict[pd.Timestamp, Dict[str, float]] = {}
    for ts, grp in expiry_bars.groupby("datetime"):
        prices = {}
        for _, row in grp.iterrows():
            k = leg_lookup.get((row["option_type"], int(row["strike_offset"])))
            if k:
                prices[k] = float(row["close"])
        if set(prices.keys()) == set(legs.keys()):
            return pd.Timestamp(ts), prices
    return None


def run_one_week(df: pd.DataFrame, ctx: WeekContext) -> DebitSpreadTrade:
    """Run the strategy for one expiry week against `df` (NIFTY options 1-min)."""
    entry_slice = _entry_slice(df, ctx)
    if entry_slice.empty:
        return _make_skip_trade(ctx, "no_entry_bar")

    atm_strike, spot_at_entry = resolve_atm_strike(entry_slice)
    if atm_strike is None:
        return _make_skip_trade(ctx, "no_atm_row")

    legs, missing = fetch_legs_at(entry_slice, ctx.leg_specs)
    if missing:
        return _make_skip_trade(ctx, f"missing_strike: {','.join(missing)}")

    net_pts, net_inr, tp = compute_entry_economics(
        legs, ctx.tp_multiple, lot_size=ctx.lot_size
    )

    holding_df = _holding_slice(df, ctx, legs)
    bars = list(build_bar_stream(
        holding_df, legs, max_gap_minutes=ctx.data_gap_force_exit_minutes
    ))

    # Exclude the squareoff bar from intra-day TP scan (we'll force exit there
    # if no earlier TP).
    expiry_ts = pd.Timestamp(f"{ctx.expiry_date.isoformat()}T{ctx.expiry_squareoff_time_str}:00+05:30")
    pre_squareoff_bars = [b for b in bars if pd.Timestamp(b["datetime"]) < expiry_ts and not b.get("force_exit")]

    tp_result = scan_for_tp_exit(
        legs, pre_squareoff_bars, net_pts, tp, lot_size=ctx.lot_size
    )

    if tp_result is not None:
        exit_ts, exit_prices, mtm = tp_result
        exit_reason = "TP"
    else:
        # Check for force-exit before squareoff
        force_bar = next((b for b in bars if b.get("force_exit")), None)
        if force_bar is not None:
            exit_ts = pd.Timestamp(force_bar["datetime"])
            exit_prices = force_bar["prices"]
            exit_reason = "data_gap_force_exit"
        else:
            squareoff = _expiry_squareoff_prices(holding_df, ctx, legs)
            if squareoff is None:
                return _make_skip_trade(ctx, "no_squareoff_bar_on_expiry")
            exit_ts, exit_prices = squareoff
            exit_reason = "EXPIRY"

    # Apply exit prices and compute PnL.  With our sign convention,
    # net_debit_pts == entry_cost_pts, so pnl = exit_signed - net_debit.
    for k, leg in legs.items():
        leg.exit_price = exit_prices[k]
    exit_value_pts = sum(_leg_signed_value(l, l.exit_price) for l in legs.values())
    pnl_pts = exit_value_pts - net_pts
    pnl_inr = pnl_pts * ctx.lot_size

    return DebitSpreadTrade(
        expiry_date=ctx.expiry_date.isoformat(),
        entry_date=ctx.entry_date.isoformat(),
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
        return_pct=0.0,                    # filled in by caller after equity update
        running_equity_inr=0.0,            # filled in by caller
        skip_reason=None,
        legs=legs,
    )
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_debit_spread.py::TestRunOneWeek -v`
Expected: 2 passed.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): single-week orchestrator
> ```

---

## Task 10: Multi-week loop + `run(config)` entrypoint

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
from engine.debit_spread_backtest import (
    run_backtest,
    parse_config,
)


class TestRunBacktest:
    def test_three_weeks_synthetic(self, tmp_path):
        # Build a 3-week synthetic dataset and run end-to-end.
        weeks = [
            (date(2025, 6, 17), date(2025, 6, 13)),
            (date(2025, 6, 24), date(2025, 6, 20)),
            (date(2025, 7, 1),  date(2025, 6, 27)),
        ]
        all_rows = []
        for expiry_d, _ in weeks:
            entry_iso = (expiry_d - timedelta(days=4)).isoformat()
            # 11:00 entry on the Friday 4 days before expiry-Tuesday
            for opt, off, mny, op in [
                ("CE", -1, "ITM", 120.0), ("CE", 4, "OTM", 35.0), ("CE", 5, "OTM", 22.0),
                ("PE", 1, "ITM", 115.0), ("PE", -4, "OTM", 32.0), ("PE", -5, "OTM", 20.0),
            ]:
                all_rows.append({
                    "datetime": f"{entry_iso}T11:00:00+05:30", "underlying": "NIFTY",
                    "option_type": opt, "expiry_code": 1,
                    "strike": 24500.0 + off * 50, "atm_strike": 24500.0,
                    "strike_offset": off, "moneyness": mny,
                    "spot": 24512.0, "open": op, "high": op, "low": op,
                    "close": op, "volume": 1, "oi": 1, "iv": 15.0,
                })
                # 15:25 expiry squareoff bars (flat → zero pnl)
                all_rows.append({
                    "datetime": f"{expiry_d.isoformat()}T15:25:00+05:30",
                    "underlying": "NIFTY", "option_type": opt, "expiry_code": 1,
                    "strike": 24500.0 + off * 50, "atm_strike": 24500.0,
                    "strike_offset": off, "moneyness": mny,
                    "spot": 24512.0, "open": op, "high": op, "low": op,
                    "close": op, "volume": 1, "oi": 1, "iv": 15.0,
                })
        df = pd.DataFrame(all_rows)

        config = {
            "name": "debit_spread", "strategy_type": "debit_spread",
            "instruments": ["NIFTY"],
            "entry": {"days_before_expiry": 2, "entry_time": "11:00"},
            "structure": {
                "ce_legs": [
                    {"side": "BUY", "lots": 1, "strike_offset": -1},
                    {"side": "SELL", "lots": 3, "strike_offset": 4},
                    {"side": "BUY", "lots": 2, "strike_offset": 5},
                ],
                "pe_legs": [
                    {"side": "BUY", "lots": 1, "strike_offset": 1},
                    {"side": "SELL", "lots": 3, "strike_offset": -4},
                    {"side": "BUY", "lots": 2, "strike_offset": -5},
                ],
            },
            "exit": {
                "tp_multiple_of_max_loss": 1.5,
                "expiry_squareoff_time": "15:25",
                "data_gap_force_exit_minutes": 30,
            },
            "sizing": {"sets_per_trade": 1, "reference_capital": 300000},
            "metrics": {"risk_free_rate": 0.06, "annualization_factor": 52},
            "backtest_start": "2025-06-01",
            "backtest_end":   "2025-07-31",
        }
        result = run_backtest(df, config, expiry_dates=[w[0] for w in weeks])
        assert "trades" in result
        assert len(result["trades"]) == 3
        assert all(t.skip_reason is None for t in result["trades"])
        # Flat market → zero pnl per week
        assert all(t.pnl_inr == pytest.approx(0.0) for t in result["trades"])
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestRunBacktest -v`
Expected: ImportError.

- [ ] **Step 3: Implement `parse_config` and `run_backtest`**

Append to engine:

```python
def parse_config(config: dict) -> Dict[str, LegSpec]:
    """Convert config['structure'] into a dict of leg_key -> LegSpec.

    Leg keys are derived deterministically:
      ce_legs[0] -> ce_itm,  ce_legs[1] -> ce_short, ce_legs[2] -> ce_far
      pe_legs[0] -> pe_itm,  pe_legs[1] -> pe_short, pe_legs[2] -> pe_far
    The order in the JSON must therefore match this ITM/short/far convention.
    """
    keys_ce = ["ce_itm", "ce_short", "ce_far"]
    keys_pe = ["pe_itm", "pe_short", "pe_far"]

    structure = config["structure"]
    legs: Dict[str, LegSpec] = {}
    for k, leg_dict in zip(keys_ce, structure["ce_legs"]):
        legs[k] = LegSpec(
            option_type="CE", side=leg_dict["side"],
            lots=int(leg_dict["lots"]),
            strike_offset=int(leg_dict["strike_offset"]),
        )
    for k, leg_dict in zip(keys_pe, structure["pe_legs"]):
        legs[k] = LegSpec(
            option_type="PE", side=leg_dict["side"],
            lots=int(leg_dict["lots"]),
            strike_offset=int(leg_dict["strike_offset"]),
        )
    return legs


def _trading_days_from_df(df: pd.DataFrame) -> List[_date]:
    return sorted({pd.to_datetime(ts).date() for ts in df["datetime"].unique()})


def run_backtest(
    df: pd.DataFrame,
    config: dict,
    expiry_dates: List[_date],
) -> dict:
    """Run the strategy for every expiry in `expiry_dates` against `df`.

    Returns a dict with:
        trades:         List[DebitSpreadTrade]
        config:         the resolved config (echo)
    Caller is responsible for building equity curve / metrics / writing CSVs.
    """
    leg_specs = parse_config(config)
    trading_days = _trading_days_from_df(df)
    days_before = int(config["entry"]["days_before_expiry"])
    entry_time = config["entry"]["entry_time"]
    squareoff_time = config["exit"]["expiry_squareoff_time"]
    tp_mult = float(config["exit"]["tp_multiple_of_max_loss"])
    gap_minutes = int(config["exit"]["data_gap_force_exit_minutes"])
    capital = float(config["sizing"]["reference_capital"])

    backtest_start = pd.to_datetime(config["backtest_start"]).date()
    backtest_end   = pd.to_datetime(config["backtest_end"]).date()

    trades: List[DebitSpreadTrade] = []
    running_equity = capital

    def _append_skip(reason: str, expiry: _date) -> None:
        skip = _make_skip_trade(
            WeekContext(
                expiry_date=expiry, entry_date=expiry,
                entry_time_str=entry_time,
                expiry_squareoff_time_str=squareoff_time,
                tp_multiple=tp_mult, data_gap_force_exit_minutes=gap_minutes,
                leg_specs=leg_specs,
            ),
            reason,
        )
        skip.running_equity_inr = running_equity
        trades.append(skip)

    for expiry in sorted(expiry_dates):
        if expiry < backtest_start or expiry > backtest_end:
            continue
        if expiry not in trading_days:
            _append_skip(f"expiry_not_in_data: {expiry}", expiry)
            continue
        try:
            entry_d = compute_entry_date(expiry, trading_days, days_before)
        except ValueError as e:
            _append_skip(f"compute_entry_date_error: {e}", expiry)
            continue

        ctx = WeekContext(
            expiry_date=expiry, entry_date=entry_d,
            entry_time_str=entry_time,
            expiry_squareoff_time_str=squareoff_time,
            tp_multiple=tp_mult, data_gap_force_exit_minutes=gap_minutes,
            leg_specs=leg_specs,
        )
        trade = run_one_week(df, ctx)
        running_equity += trade.pnl_inr
        trade.return_pct = trade.pnl_inr / capital if capital else 0.0
        trade.running_equity_inr = running_equity
        trades.append(trade)

    return {"trades": trades, "config": config}
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_debit_spread.py::TestRunBacktest -v`
Expected: 1 passed.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): multi-week run loop + config parser
> ```

---

## Task 11: Daily equity curve + drawdown

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
from engine.debit_spread_backtest import build_equity_curve


class TestBuildEquityCurve:
    def test_three_trades_step_function(self):
        trades = [
            DebitSpreadTrade(
                expiry_date="2025-06-17", entry_date="2025-06-13",
                entry_time="11:00", atm_strike=24500.0, spot_at_entry=24500.0,
                net_debit_pts=80.0, net_debit_inr=5200.0, tp_target_inr=7800.0,
                exit_time="15:25", exit_reason="EXPIRY",
                pnl_pts=10.0, pnl_inr=650.0, return_pct=0.00216,
                running_equity_inr=300650.0, skip_reason=None, legs={},
            ),
            DebitSpreadTrade(
                expiry_date="2025-06-24", entry_date="2025-06-20",
                entry_time="11:00", atm_strike=24500.0, spot_at_entry=24500.0,
                net_debit_pts=80.0, net_debit_inr=5200.0, tp_target_inr=7800.0,
                exit_time="15:25", exit_reason="EXPIRY",
                pnl_pts=-20.0, pnl_inr=-1300.0, return_pct=-0.00433,
                running_equity_inr=299350.0, skip_reason=None, legs={},
            ),
            DebitSpreadTrade(
                expiry_date="2025-07-01", entry_date="2025-06-27",
                entry_time="11:00", atm_strike=24500.0, spot_at_entry=24500.0,
                net_debit_pts=80.0, net_debit_inr=5200.0, tp_target_inr=7800.0,
                exit_time="15:25", exit_reason="EXPIRY",
                pnl_pts=30.0, pnl_inr=1950.0, return_pct=0.0065,
                running_equity_inr=301300.0, skip_reason=None, legs={},
            ),
        ]
        curve = build_equity_curve(trades, starting_capital=300_000.0)
        assert curve.iloc[0]["equity_inr"] == 300650.0
        assert curve.iloc[1]["equity_inr"] == 299350.0
        assert curve.iloc[2]["equity_inr"] == 301300.0
        # Drawdown computed off running peak
        assert curve.iloc[1]["drawdown_inr"] == pytest.approx(300650.0 - 299350.0)
        assert curve.iloc[2]["drawdown_inr"] == pytest.approx(0.0)

    def test_skipped_trades_carry_equity_flat(self):
        trades = [
            DebitSpreadTrade(
                expiry_date="2025-06-17", entry_date="2025-06-13",
                entry_time="11:00", atm_strike=float("nan"),
                spot_at_entry=float("nan"),
                net_debit_pts=float("nan"), net_debit_inr=float("nan"),
                tp_target_inr=float("nan"),
                exit_time="", exit_reason="",
                pnl_pts=0.0, pnl_inr=0.0, return_pct=0.0,
                running_equity_inr=300000.0,
                skip_reason="missing_strike: ce_far",
                legs={},
            ),
        ]
        curve = build_equity_curve(trades, starting_capital=300_000.0)
        assert curve.iloc[0]["equity_inr"] == 300000.0
        assert curve.iloc[0]["drawdown_inr"] == 0.0
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestBuildEquityCurve -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Append to engine:

```python
def build_equity_curve(
    trades: List[DebitSpreadTrade],
    starting_capital: float,
) -> pd.DataFrame:
    """One row per trade attempt (in chronological order):
        date, equity_inr, drawdown_inr, drawdown_pct, in_trade.
    Skipped attempts contribute a flat equity point (no change).
    """
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
            "date": t.expiry_date,
            "equity_inr": equity,
            "drawdown_inr": dd_inr,
            "drawdown_pct": dd_pct,
            "in_trade": t.skip_reason is None,
        })
    return pd.DataFrame(rows)


def _is_nan(x) -> bool:
    try:
        return x != x  # NaN != NaN
    except Exception:
        return False
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_debit_spread.py::TestBuildEquityCurve -v`
Expected: 2 passed.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): equity curve and drawdown
> ```

---

## Task 12: Sharpe / Sortino / max-consecutive-losses

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
import math
from engine.debit_spread_backtest import (
    compute_sharpe,
    compute_sortino,
    max_consecutive_losses,
    summarize_metrics,
)


class TestSharpeSortino:
    def test_sharpe_positive_returns(self):
        # Five weekly returns, mean 0.01, std ≈ 0.005
        returns = [0.01, 0.005, 0.015, 0.01, 0.005]
        s = compute_sharpe(returns, risk_free_rate=0.06, periods_per_year=52)
        # weekly_rfr = 0.06/52 ≈ 0.001154
        # mean = 0.009 ; sd (sample) ≈ 0.00418
        # sharpe ≈ (0.009 - 0.001154) / 0.00418 * sqrt(52)
        expected = (0.009 - 0.06/52) / 0.0041833 * math.sqrt(52)
        assert s == pytest.approx(expected, rel=0.01)

    def test_sharpe_zero_stdev_returns_nan(self):
        returns = [0.01, 0.01, 0.01]
        s = compute_sharpe(returns, risk_free_rate=0.06, periods_per_year=52)
        assert math.isnan(s)

    def test_sortino_only_downside(self):
        returns = [0.02, -0.01, 0.03, -0.02, 0.01]
        sortino = compute_sortino(returns, risk_free_rate=0.06, periods_per_year=52)
        assert not math.isnan(sortino)


class TestMaxConsecutiveLosses:
    def test_no_losses(self):
        assert max_consecutive_losses([100.0, 50.0, 200.0]) == 0

    def test_basic_streak(self):
        # losses are pnl < 0 ; zero counts as neither
        assert max_consecutive_losses([-1.0, -2.0, 1.0, -1.0]) == 2

    def test_skipped_pnl_zero_breaks_streak(self):
        # 0 is not a loss → breaks
        assert max_consecutive_losses([-1.0, 0.0, -1.0, -1.0]) == 2
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestSharpeSortino tests/test_debit_spread.py::TestMaxConsecutiveLosses -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Append to engine:

```python
import math
import statistics


def compute_sharpe(
    returns: List[float],
    risk_free_rate: float,
    periods_per_year: int,
) -> float:
    """Annualized Sharpe = (mean_return - period_rfr) / stdev_return * sqrt(periods)."""
    if len(returns) < 2:
        return float("nan")
    period_rfr = risk_free_rate / periods_per_year
    mean_r = statistics.fmean(returns)
    sd = statistics.stdev(returns)
    if sd == 0:
        return float("nan")
    return (mean_r - period_rfr) / sd * math.sqrt(periods_per_year)


def compute_sortino(
    returns: List[float],
    risk_free_rate: float,
    periods_per_year: int,
) -> float:
    """Sortino uses downside deviation = stdev of min(0, r - period_rfr)."""
    if len(returns) < 2:
        return float("nan")
    period_rfr = risk_free_rate / periods_per_year
    mean_r = statistics.fmean(returns)
    downside = [min(0.0, r - period_rfr) for r in returns]
    if all(d == 0 for d in downside):
        return float("nan")
    sd_down = statistics.stdev(downside)
    if sd_down == 0:
        return float("nan")
    return (mean_r - period_rfr) / sd_down * math.sqrt(periods_per_year)


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


def summarize_metrics(
    trades: List[DebitSpreadTrade],
    starting_capital: float,
    risk_free_rate: float,
    periods_per_year: int,
) -> dict:
    """Compute every metric we report. Skipped trades contribute pnl=0,
    return=0 (preserves time-series length for ratios)."""
    pnls = [t.pnl_inr for t in trades]
    returns = [t.return_pct for t in trades]
    placed = [t for t in trades if t.skip_reason is None]
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
        "total_weeks_processed": len(trades),
        "trades_placed": len(placed),
        "trades_skipped": len(trades) - len(placed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(placed) if placed else 0.0,
        "loss_rate": len(losses) / len(placed) if placed else 0.0,
        "pct_profitable_weeks": len(wins) / len(placed) if placed else 0.0,
        "mean_pnl_inr": statistics.fmean(pnls) if pnls else 0.0,
        "median_pnl_inr": statistics.median(pnls) if pnls else 0.0,
        "total_pnl_inr": sum(pnls),
        "total_return_pct": sum(pnls) / starting_capital if starting_capital else 0.0,
        "max_drawdown_inr": max_dd_inr,
        "max_drawdown_pct": max_dd_pct,
        "max_consecutive_losses": max_consecutive_losses(pnls),
        "sharpe": compute_sharpe(returns, risk_free_rate, periods_per_year),
        "sortino": compute_sortino(returns, risk_free_rate, periods_per_year),
        "best_trade_inr": max(pnls) if pnls else 0.0,
        "worst_trade_inr": min(pnls) if pnls else 0.0,
        "exit_reason_counts": _count_by(placed, lambda t: t.exit_reason),
        "skip_reason_counts": _count_by(
            [t for t in trades if t.skip_reason], lambda t: t.skip_reason
        ),
    }


def _count_by(items, keyfn):
    counts: Dict[str, int] = {}
    for it in items:
        k = keyfn(it)
        counts[k] = counts.get(k, 0) + 1
    return counts
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_debit_spread.py::TestSharpeSortino tests/test_debit_spread.py::TestMaxConsecutiveLosses -v`
Expected: 6 passed.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): metrics (Sharpe, Sortino, drawdown, max-consec)
> ```

---

## Task 13: CSV writers + stdout summary printer

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
from engine.debit_spread_backtest import (
    write_trades_csv,
    write_equity_csv,
    print_summary,
)


class TestWriters:
    def test_trades_csv_has_per_leg_columns(self, tmp_path):
        legs = _legs_with_prices({
            "ce_itm": 120.0, "ce_short": 35.0, "ce_far": 22.0,
            "pe_itm": 115.0, "pe_short": 32.0, "pe_far": 20.0,
        })
        for leg in legs.values():
            leg.exit_price = leg.entry_price
        trade = DebitSpreadTrade(
            expiry_date="2025-06-17", entry_date="2025-06-13",
            entry_time="11:00", atm_strike=24500.0, spot_at_entry=24512.0,
            net_debit_pts=118.0, net_debit_inr=7670.0, tp_target_inr=11505.0,
            exit_time="15:25", exit_reason="EXPIRY",
            pnl_pts=0.0, pnl_inr=0.0, return_pct=0.0,
            running_equity_inr=300000.0, skip_reason=None, legs=legs,
        )
        path = tmp_path / "trades.csv"
        write_trades_csv([trade], path)
        df = pd.read_csv(path)
        for k in LEG_KEY_ORDER:
            assert f"{k}_strike" in df.columns
            assert f"{k}_entry" in df.columns
            assert f"{k}_exit" in df.columns
        assert df.iloc[0]["exit_reason"] == "EXPIRY"

    def test_equity_csv_columns(self, tmp_path):
        trades = [
            DebitSpreadTrade(
                expiry_date="2025-06-17", entry_date="2025-06-13",
                entry_time="11:00", atm_strike=24500.0, spot_at_entry=24500.0,
                net_debit_pts=80.0, net_debit_inr=5200.0, tp_target_inr=7800.0,
                exit_time="15:25", exit_reason="EXPIRY",
                pnl_pts=10.0, pnl_inr=650.0, return_pct=0.00216,
                running_equity_inr=300650.0, skip_reason=None, legs={},
            ),
        ]
        path = tmp_path / "equity.csv"
        write_equity_csv(trades, starting_capital=300000.0, path=path)
        df = pd.read_csv(path)
        assert {"date", "equity_inr", "drawdown_inr", "drawdown_pct", "in_trade"}.issubset(df.columns)


class TestPrintSummary:
    def test_summary_prints_required_lines(self, capsys):
        summary = {
            "total_weeks_processed": 10, "trades_placed": 9,
            "trades_skipped": 1, "wins": 6, "losses": 3,
            "win_rate": 6/9, "loss_rate": 3/9, "pct_profitable_weeks": 6/9,
            "mean_pnl_inr": 500.0, "median_pnl_inr": 200.0,
            "total_pnl_inr": 4500.0, "total_return_pct": 0.015,
            "max_drawdown_inr": 1500.0, "max_drawdown_pct": 0.005,
            "max_consecutive_losses": 2,
            "sharpe": 1.7, "sortino": 2.4,
            "best_trade_inr": 2000.0, "worst_trade_inr": -800.0,
            "exit_reason_counts": {"TP": 4, "EXPIRY": 5},
            "skip_reason_counts": {"missing_strike: ce_far": 1},
        }
        print_summary(summary)
        out = capsys.readouterr().out
        assert "Total weeks processed: 10" in out
        assert "Trades placed: 9" in out
        assert "Sharpe" in out
        assert "Sortino" in out
        assert "Max drawdown" in out
        assert "TP" in out and "EXPIRY" in out
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestWriters tests/test_debit_spread.py::TestPrintSummary -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Append to engine:

```python
from pathlib import Path


def write_trades_csv(trades: List[DebitSpreadTrade], path) -> None:
    df = trades_to_dataframe(trades)
    df.to_csv(path, index=False)


def write_equity_csv(trades: List[DebitSpreadTrade], starting_capital: float, path) -> None:
    df = build_equity_curve(trades, starting_capital)
    df.to_csv(path, index=False)


def print_summary(summary: dict) -> None:
    s = summary
    lines = [
        f"Total weeks processed: {s['total_weeks_processed']}",
        f"Trades placed: {s['trades_placed']}",
        f"Trades skipped: {s['trades_skipped']}",
        f"Wins (P&L > 0): {s['wins']}  ({s['win_rate']*100:.2f}%)",
        f"Losses (P&L < 0): {s['losses']}  ({s['loss_rate']*100:.2f}%)",
        f"% profitable weeks: {s['pct_profitable_weeks']*100:.2f}%",
        f"Mean P&L (Rs): {s['mean_pnl_inr']:.2f}",
        f"Median P&L (Rs): {s['median_pnl_inr']:.2f}",
        f"Total P&L (Rs): {s['total_pnl_inr']:.2f}",
        f"Total return on reference capital: {s['total_return_pct']*100:.2f}%",
        f"Max drawdown (Rs / pct): {s['max_drawdown_inr']:.2f} / {s['max_drawdown_pct']*100:.2f}%",
        f"Max consecutive losing weeks: {s['max_consecutive_losses']}",
        f"Sharpe (weekly, ann. sqrt(52)): {s['sharpe']:.4f}",
        f"Sortino (weekly, ann. sqrt(52)): {s['sortino']:.4f}",
        f"Best trade: Rs {s['best_trade_inr']:.2f}    Worst trade: Rs {s['worst_trade_inr']:.2f}",
        f"Exit reason counts: {s['exit_reason_counts']}",
        f"Skip reason counts: {s['skip_reason_counts']}",
    ]
    for line in lines:
        print(line)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pytest tests/test_debit_spread.py::TestWriters tests/test_debit_spread.py::TestPrintSummary -v`
Expected: 3 passed.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): CSV writers and summary printer
> ```

---

## Task 14: Top-level `run(config_path)` entrypoint

**Files:**
- Modify: `engine/debit_spread_backtest.py` (append)
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Write the failing test**

```python
from engine.debit_spread_backtest import run


class TestEntrypoint:
    def test_smoke_run_with_real_data(self, tmp_path):
        # Smoke test against real parquet — runs a 1-month slice.
        import os
        cfg = {
            "name": "debit_spread", "strategy_type": "debit_spread",
            "instruments": ["NIFTY"],
            "entry": {"days_before_expiry": 2, "entry_time": "11:00"},
            "structure": {
                "ce_legs": [
                    {"side": "BUY", "lots": 1, "strike_offset": -1},
                    {"side": "SELL", "lots": 3, "strike_offset": 4},
                    {"side": "BUY", "lots": 2, "strike_offset": 5},
                ],
                "pe_legs": [
                    {"side": "BUY", "lots": 1, "strike_offset": 1},
                    {"side": "SELL", "lots": 3, "strike_offset": -4},
                    {"side": "BUY", "lots": 2, "strike_offset": -5},
                ],
            },
            "exit": {
                "tp_multiple_of_max_loss": 1.5,
                "expiry_squareoff_time": "15:25",
                "data_gap_force_exit_minutes": 30,
            },
            "sizing": {"sets_per_trade": 1, "reference_capital": 300000},
            "metrics": {"risk_free_rate": 0.06, "annualization_factor": 52},
            "backtest_start": "2025-06-01",
            "backtest_end":   "2025-06-30",
        }
        result = run(
            cfg,
            options_path="data/options/nifty/NIFTY_OPTIONS_1m.parquet",
            output_dir=str(tmp_path),
        )
        assert "trades" in result
        assert "summary" in result
        # Files exist
        assert any(p.suffix == ".csv" and "trades" in p.name for p in tmp_path.iterdir())
        assert any(p.suffix == ".csv" and "equity" in p.name for p in tmp_path.iterdir())
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `pytest tests/test_debit_spread.py::TestEntrypoint -v`
Expected: ImportError on `run`.

- [ ] **Step 3: Implement `run`**

Append to engine:

```python
def _load_expiry_dates() -> List[_date]:
    """Pull NIFTY weekly expiries from config.py without coupling tests."""
    from config import NIFTY_WEEKLY_EXPIRY_DATES
    return list(NIFTY_WEEKLY_EXPIRY_DATES)


def run(
    config: dict,
    options_path: str,
    output_dir: str,
) -> dict:
    """Top-level entrypoint. Loads parquet, runs backtest, writes CSVs, prints summary."""
    df = pd.read_parquet(options_path)
    df = df[df["underlying"] == "NIFTY"]

    bt_start = pd.to_datetime(config["backtest_start"])
    bt_end   = pd.to_datetime(config["backtest_end"]) + pd.Timedelta(days=1)
    df = df[(pd.to_datetime(df["datetime"]) >= bt_start) &
            (pd.to_datetime(df["datetime"]) < bt_end)]

    expiry_dates = _load_expiry_dates()

    backtest_result = run_backtest(df, config, expiry_dates)
    trades = backtest_result["trades"]

    capital = float(config["sizing"]["reference_capital"])
    rfr = float(config["metrics"]["risk_free_rate"])
    annualization = int(config["metrics"]["annualization_factor"])

    summary = summarize_metrics(trades, capital, rfr, annualization)

    # Output files
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    start_str = config["backtest_start"]
    end_str = config["backtest_end"]
    trades_path = out / f"debit_spread_trades_{start_str}_{end_str}.csv"
    equity_path = out / f"debit_spread_equity_{start_str}_{end_str}.csv"

    write_trades_csv(trades, trades_path)
    write_equity_csv(trades, capital, equity_path)

    print_summary(summary)
    return {
        "trades": trades, "summary": summary,
        "trades_csv": str(trades_path),
        "equity_csv": str(equity_path),
    }
```

- [ ] **Step 4: Run test, verify it passes**

Run: `pytest tests/test_debit_spread.py::TestEntrypoint -v`
Expected: 1 passed (may be slow — the parquet read is ~17M rows; the slice cuts it to ~1 month).

If the test exceeds 60s, add `@pytest.mark.slow` and skip it by default; document in the test.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): top-level run() entrypoint with parquet I/O
> ```

---

## Task 15: UI integration — add Debit Spread to backtest_runner

**Files:**
- Modify: `ui/backtest_runner.py`
- Modify: `ui/strategy_form.py` (only if a strategy-type enum exists; otherwise skip)

- [ ] **Step 1: Read the existing pattern for `gamma_blast` and copy its handler**

Examine:

```bash
grep -n "gamma_blast\|strategy_type" ui/backtest_runner.py | head -40
grep -n "gamma_blast\|strategy_type" ui/strategy_form.py | head -40
```

Record the exact location where `gamma_blast` is registered and replicate the pattern for `debit_spread`. Use the same:
- form-field rendering style
- output-panel rendering style
- file-download buttons

- [ ] **Step 2: Add the Debit Spread handler**

Insert (in the same place gamma_blast is wired up):

```python
elif strategy_type == "debit_spread":
    import streamlit as st
    from engine import debit_spread_backtest as dsb
    import json

    cfg_path = "saved_strategies/debit_spread.json"
    with open(cfg_path) as f:
        cfg = json.load(f)

    # Form overrides
    cfg["backtest_start"] = str(st.session_state.get("ds_start", cfg["backtest_start"]))
    cfg["backtest_end"]   = str(st.session_state.get("ds_end",   cfg["backtest_end"]))
    cfg["exit"]["tp_multiple_of_max_loss"] = float(
        st.session_state.get("ds_tp_mult", cfg["exit"]["tp_multiple_of_max_loss"])
    )

    output_dir = "output/debit_spread"
    result = dsb.run(
        cfg,
        options_path="data/options/nifty/NIFTY_OPTIONS_1m.parquet",
        output_dir=output_dir,
    )

    # Render summary
    st.subheader("Debit Spread Summary")
    summary = result["summary"]
    st.json(summary)

    # Render equity curve
    eq = pd.read_csv(result["equity_csv"])
    st.line_chart(eq.set_index("date")["equity_inr"])

    # Render trades table
    trades_df = pd.read_csv(result["trades_csv"])
    st.dataframe(trades_df)

    # Download buttons
    st.download_button("Download trades CSV",
                       data=open(result["trades_csv"], "rb").read(),
                       file_name=result["trades_csv"].split("/")[-1])
    st.download_button("Download equity CSV",
                       data=open(result["equity_csv"], "rb").read(),
                       file_name=result["equity_csv"].split("/")[-1])
```

(Adjust to match the surrounding code style — the snippet above is illustrative; the actual `backtest_runner.py` may use a different routing pattern. Match it exactly.)

- [ ] **Step 3: Add Debit Spread to the strategy dropdown**

Locate where strategy types are listed in `ui/strategy_form.py` (or wherever the dropdown options live) and add `"debit_spread"` next to `"gamma_blast"`.

- [ ] **Step 4: Smoke-test the UI**

Run:

```bash
streamlit run app.py
```

Manually:
- Pick "Debit Spread" from the dropdown.
- Click Run.
- Confirm: summary block renders, equity chart renders, trades table renders, both download buttons work.
- Inspect a downloaded trades CSV to confirm columns match Task 13's spec.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> feat(debit_spread): wire into Streamlit backtest runner
> ```

---

## Task 16: Real-data integration test (golden output)

**Files:**
- Modify: `tests/test_debit_spread.py` (append)

- [ ] **Step 1: Add a real-data integration test**

```python
class TestRealDataIntegration:
    @pytest.mark.slow
    def test_one_known_week(self):
        """Run a single known-good expiry week end-to-end against real parquet.

        We don't pin exact P&L (could change with parquet updates), but we
        assert the trade is non-skipped, has all 6 legs filled, and exit_reason
        is one of {TP, EXPIRY, data_gap_force_exit}.
        """
        df = pd.read_parquet("data/options/nifty/NIFTY_OPTIONS_1m.parquet")
        df = df[df["underlying"] == "NIFTY"]
        df["dt"] = pd.to_datetime(df["datetime"])
        df = df[(df["dt"] >= "2025-06-09") & (df["dt"] < "2025-06-18")]

        expiry = date(2025, 6, 17)  # Tuesday
        trading_days = sorted({pd.to_datetime(ts).date() for ts in df["datetime"].unique()})
        ctx = WeekContext(
            expiry_date=expiry,
            entry_date=compute_entry_date(expiry, trading_days, 2),
            entry_time_str="11:00",
            expiry_squareoff_time_str="15:25",
            tp_multiple=1.5,
            data_gap_force_exit_minutes=30,
            leg_specs=DEFAULT_LEG_SPECS,
        )
        trade = run_one_week(df.drop(columns=["dt"]), ctx)
        assert trade.skip_reason is None, f"week skipped: {trade.skip_reason}"
        assert trade.exit_reason in {"TP", "EXPIRY", "data_gap_force_exit"}
        assert len(trade.legs) == 6
        for leg in trade.legs.values():
            assert leg.exit_price is not None
```

- [ ] **Step 2: Mark slow tests in `pytest.ini` (if not already configured)**

Check `pytest.ini` / `pyproject.toml`. If `slow` marker isn't declared, add:

```ini
[pytest]
markers =
    slow: marks tests as slow (deselect with '-m "not slow"')
```

- [ ] **Step 3: Run the integration test**

Run: `pytest tests/test_debit_spread.py::TestRealDataIntegration -v`
Expected: 1 passed.

- [ ] **Step 4: Run the full suite**

Run: `pytest tests/test_debit_spread.py -v`
Expected: all tests pass.

- [ ] **Step 5: Suggest commit**

> Commit message:
> ```
> test(debit_spread): real-data integration test for known week
> ```

---

## Task 17: End-to-end backtest run on full data range

**Files:** none (verification only)

- [ ] **Step 1: Run the full backtest from CLI**

Create a one-shot Python invocation:

```bash
python3 -c "
import json
from engine.debit_spread_backtest import run

with open('saved_strategies/debit_spread.json') as f:
    cfg = json.load(f)

run(cfg,
    options_path='data/options/nifty/NIFTY_OPTIONS_1m.parquet',
    output_dir='output/debit_spread')
"
```

- [ ] **Step 2: Inspect outputs**

```bash
ls output/debit_spread/
head -5 output/debit_spread/debit_spread_trades_*.csv
head -5 output/debit_spread/debit_spread_equity_*.csv
```

Verify:
- Trades CSV row count ≈ number of weekly expiries in `[2025-01-01, 2026-05-05]` (~70).
- Equity CSV has same row count.
- Stdout summary printed all required lines: total trades, win rate, mean/median, drawdown, Sharpe, Sortino, max consecutive losses.

- [ ] **Step 3: Sanity-check a few weeks against the trades CSV**

Pick 3 random rows. For each:
- Verify `entry_date` is exactly 2 trading days before `expiry_date`.
- Verify `atm_strike` is a multiple of 50.
- Verify the 6 leg strikes are at `atm_strike` + {−50, +200, +250} (CE side) and `atm_strike` + {+50, −200, −250} (PE side).
- Recompute `pnl_inr` by hand from `(*_entry, *_exit)` columns and confirm it matches the CSV.

- [ ] **Step 4: Suggest commit**

No file changes here — just an end-to-end smoke run. If everything looks good:

> Commit message (if any artifacts to commit, e.g. the output dir was added to .gitignore):
> ```
> chore: ignore debit_spread output dir
> ```

---

## Self-Review Checklist (run after writing the plan)

- [x] **Spec coverage:** every numbered section in the spec maps to at least one task.
  - §2 entry construction → Tasks 3-6
  - §3 exit logic → Tasks 7-9
  - §4 MTM & equity curve → Tasks 7, 11
  - §5 P&L math → Task 9
  - §6 sizing → covered via `LOT_SIZE_NIFTY` constant + config (Tasks 1, 6)
  - §7 config → Task 1
  - §8 outputs → Tasks 13, 14
  - §9 UI → Task 15
  - §10 testing plan → tests across all tasks + Task 16
- [x] **No placeholders:** every step has actual code or exact commands.
- [x] **Type consistency:**
  - `LegSpec` / `LegFill` / `DebitSpreadTrade` defined in Task 2, used consistently in Tasks 3-14.
  - `LEG_KEY_ORDER` and `DEFAULT_LEG_SPECS` introduced in Task 5, referenced correctly in later tasks.
  - `compute_entry_economics`, `scan_for_tp_exit`, `compute_mtm_inr` — signatures match call sites in Tasks 9, 10.
  - `run_one_week` returns `DebitSpreadTrade` with `running_equity_inr=0.0` placeholder; `run_backtest` (Task 10) is responsible for filling it. Verified consistent.
- [x] **Bite-sized steps:** every step is one action.
- [x] **TDD:** every code task starts with a failing test.
- [x] **No `git commit` commands:** all commit steps tell the engineer to ask the user to commit. ✓ matches user's CLAUDE.md.
