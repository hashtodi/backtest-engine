# SENSEX Dual Short-Premium Backtest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backtest the two-part SENSEX short-premium strategy (09:45 0.25Δ strangle + post-11:45 ATM range breakout) on 1-minute data, producing a per-leg trades CSV and a summary + skip ledger.

**Architecture:** A day-by-day engine built from small pure functions — weekly-DTE gate, offset-based strike selection, spot range, spot breakout detection, and a per-leg SL/target/re-entry state machine. Contracts are **locked by absolute strike at selection time**; all SL/target/re-entry monitoring reads only that locked contract's own 1-minute OHLC. A thin CLI runner wires config paths, runs the engine over the full data window, writes the CSV, and prints the summary.

**Tech Stack:** Python 3, pandas, pytest. Reuses `engine/data_loader.load_data`, `engine/expiry_calendar`, and `config.py` constants.

## Global Constraints

- Instrument: `SENSEX`. Lot size **20** (`config.LOT_SIZE['SENSEX']`). Strike step **100** (`config.STRIKE_ROUNDING['SENSEX']`).
- Options: `expiry_type == "WEEK"`, `expiry_code == 1` (via `load_data(..., "weekly")`).
- Delta→offset: **0.5Δ = strike_offset 0** (ATM); **0.25Δ CE = +6** (ATM+600); **0.25Δ PE = −6** (ATM−600).
- SL = **1.25 × original entry cost** (fires when contract 1-min **high ≥ SL**). Target = **0.10 × original entry cost** (fires when contract 1-min **low ≤ Target**). Same-bar SL+Target → **SL wins**.
- Re-entry: **max 1 per leg**, arms **only after an SL**, fires when the same locked contract's 1-min **low ≤ original entry cost**, evaluated from the **bar after** the SL exit; re-sell at original cost; same SL/Target.
- **No slippage.** Entries fill at bar close; SL at exactly 1.25×cost; Target at exactly 0.10×cost; re-entry at exactly cost; EOD at the 15:28 close.
- Trade only when `days_to_weekly_expiry("SENSEX", day) ∈ {0,1,2,3}` (calendar days). On DTE 0, sell the expiring `code=1` contract — no roll.
- Range window: **spot** high/low over **09:45→11:45 inclusive**; breakout monitoring **11:46→15:28**. Break = strict `>` (high) / `<` (low). Both sides can fire; an engulfing bar fires both.
- Hard square-off **15:28**; no entries/re-entries after 15:28.
- P&L (short leg) = `entry − exit` points; `pnl_inr = pnl_points × 20`.
- Backtest window: **2023-05-15 → 2026-04-23** (full data range).
- Timezone: never call `.values`/`.to_numpy()` on the tz-aware `datetime` column (it shifts to UTC). Use the `date` / `time_only` helper columns from `load_data`, and for spot parse with `pd.to_datetime(df['datetime'])` then derive `.dt.date` / `.dt.time`.

---

### Task 1: Weekly-DTE helpers in `expiry_calendar.py`

**Files:**
- Modify: `engine/expiry_calendar.py` (append two functions + a private source helper)
- Test: `tests/test_sensex_dual_short.py` (new file — DTE tests)

**Interfaces:**
- Produces:
  - `get_weekly_expiry(instrument: str, trading_date) -> Optional[datetime.date]` — nearest weekly expiry on/after `trading_date`.
  - `days_to_weekly_expiry(instrument: str, trading_date) -> Optional[int]` — calendar days to that expiry, or `None` if none found.
  - For SENSEX these read `config.SENSEX_WEEKLY_EXPIRY_DATES` (complete list); for other instruments they read `get_weekly_expiries(instrument)` (JSON).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sensex_dual_short.py
from datetime import date
from engine.expiry_calendar import get_weekly_expiry, days_to_weekly_expiry


def test_weekly_dte_friday_regime_2023():
    # 2023-06-02 is a Friday SENSEX weekly expiry (config list).
    assert get_weekly_expiry("SENSEX", date(2023, 6, 2)) == date(2023, 6, 2)
    assert days_to_weekly_expiry("SENSEX", date(2023, 6, 2)) == 0        # expiry day
    assert days_to_weekly_expiry("SENSEX", date(2023, 6, 1)) == 1        # Thursday before
    assert days_to_weekly_expiry("SENSEX", date(2023, 5, 30)) == 3       # Tuesday before


def test_weekly_dte_thursday_regime_2025():
    # 2025-09-04 (Thu) is a weekly expiry.
    assert days_to_weekly_expiry("SENSEX", date(2025, 9, 4)) == 0
    assert days_to_weekly_expiry("SENSEX", date(2025, 9, 1)) == 3        # Monday before
    # The day after an expiry looks 6-7 days out -> outside {0,1,2,3} (skipped).
    assert days_to_weekly_expiry("SENSEX", date(2025, 9, 5)) == 6


def test_weekly_expiry_none_when_past_end():
    assert get_weekly_expiry("SENSEX", date(2099, 1, 1)) is None
    assert days_to_weekly_expiry("SENSEX", date(2099, 1, 1)) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sensex_dual_short.py -k weekly -v`
Expected: FAIL with `ImportError: cannot import name 'get_weekly_expiry'`.

- [ ] **Step 3: Implement the helpers**

Append to `engine/expiry_calendar.py`:

```python
def _weekly_dates(instrument: str) -> List[date]:
    """SENSEX weekly dates come from config (complete 2023->2026 list);
    other instruments come from the JSON calendar."""
    if instrument.lower() == "sensex":
        from config import SENSEX_WEEKLY_EXPIRY_DATES  # lazy import; avoids load-order issues
        return sorted(SENSEX_WEEKLY_EXPIRY_DATES)
    return get_weekly_expiries(instrument)


def get_weekly_expiry(instrument: str, trading_date) -> Optional[date]:
    """Nearest weekly expiry on or after trading_date, or None."""
    trading_date = _to_date(trading_date)
    for exp in _weekly_dates(instrument):
        if exp >= trading_date:
            return exp
    return None


def days_to_weekly_expiry(instrument: str, trading_date) -> Optional[int]:
    """Calendar days from trading_date to the nearest weekly expiry, or None."""
    trading_date = _to_date(trading_date)
    exp = get_weekly_expiry(instrument, trading_date)
    if exp is None:
        return None
    return (exp - trading_date).days
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sensex_dual_short.py -k weekly -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/expiry_calendar.py tests/test_sensex_dual_short.py
git commit -m "feat: add weekly-DTE helpers (SENSEX sourced from config list)"
```

---

### Task 2: Offset-based strike selection

**Files:**
- Create: `engine/sensex_dual_short_backtest.py`
- Test: `tests/test_sensex_dual_short.py` (add selection tests)

**Interfaces:**
- Produces:
  - `pick_row(slice_df: pd.DataFrame, option_type: str, offset: int) -> Optional[pd.Series]` — the single row matching `(option_type, strike_offset)`, else `None`.
  - `select_locked_strikes(slice_0945: pd.DataFrame) -> dict` — returns
    `{"p1_ce": row|None, "p1_pe": row|None, "p2_ce": row|None, "p2_pe": row|None}`
    where `p1_ce`=(CE,+6), `p1_pe`=(PE,−6), `p2_ce`=(CE,0), `p2_pe`=(PE,0). Each value is a `pd.Series` (has `strike`, `close`, `option_type`) or `None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sensex_dual_short.py  (append)
import pandas as pd
from engine.sensex_dual_short_backtest import pick_row, select_locked_strikes


def _slice_0945():
    # ATM=78300, step 100. Rows for offsets -6,0,+6 for both CE and PE.
    rows = []
    for off in (-6, 0, 6):
        strike = 78300 + off * 100
        for ot, close in (("CE", 100 + off), ("PE", 100 - off)):
            rows.append({"option_type": ot, "strike_offset": off,
                         "strike": float(strike), "close": float(close)})
    return pd.DataFrame(rows)


def test_pick_row_hit_and_miss():
    s = _slice_0945()
    assert pick_row(s, "CE", 6)["strike"] == 78900.0
    assert pick_row(s, "PE", -6)["strike"] == 77700.0
    assert pick_row(s, "CE", 3) is None          # offset absent -> None


def test_select_locked_strikes_maps_offsets():
    picks = select_locked_strikes(_slice_0945())
    assert picks["p1_ce"]["strike"] == 78900.0   # ATM+600 CE
    assert picks["p1_pe"]["strike"] == 77700.0   # ATM-600 PE
    assert picks["p2_ce"]["strike"] == 78300.0   # ATM CE
    assert picks["p2_pe"]["strike"] == 78300.0   # ATM PE


def test_select_locked_strikes_missing_offset_is_none():
    s = _slice_0945()
    s = s[s["strike_offset"] != 6]               # drop the +6 rows
    picks = select_locked_strikes(s)
    assert picks["p1_ce"] is None                # ATM+600 CE unavailable
    assert picks["p1_pe"]["strike"] == 77700.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sensex_dual_short.py -k "pick_row or select_locked" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.sensex_dual_short_backtest'`.

- [ ] **Step 3: Implement selection**

Create `engine/sensex_dual_short_backtest.py`:

```python
"""
SENSEX dual short-premium backtest.

Part 1: 09:45 short strangle (sell 0.25d CE=ATM+600 and 0.25d PE=ATM-600).
Part 2: post-11:45 range breakout on spot; sell the 09:45-recorded ATM PUT on a
        high break / ATM CALL on a low break.

Shared per-leg management: SL=1.25x cost, target=0.10x cost, one re-entry at cost.
Strikes are LOCKED at selection time; SL/target/re-entry monitor only that
locked contract's own OHLC. No slippage. See
docs/superpowers/specs/2026-07-01-sensex-dual-short-backtest-design.md.
"""
from typing import Optional
import pandas as pd

INSTRUMENT = "SENSEX"

# Delta -> strike_offset mapping (SENSEX step 100).
OFF_P1_CE = 6    # 0.25d CE = ATM + 600
OFF_P1_PE = -6   # 0.25d PE = ATM - 600
OFF_ATM = 0      # 0.5d  = ATM


def pick_row(slice_df: pd.DataFrame, option_type: str, offset: int) -> Optional[pd.Series]:
    """Return the single row matching (option_type, strike_offset), else None."""
    rows = slice_df[(slice_df["option_type"] == option_type)
                    & (slice_df["strike_offset"] == offset)]
    return rows.iloc[0] if len(rows) else None


def select_locked_strikes(slice_0945: pd.DataFrame) -> dict:
    """Pick the four legs' contracts from the 09:45 slice by strike_offset."""
    return {
        "p1_ce": pick_row(slice_0945, "CE", OFF_P1_CE),
        "p1_pe": pick_row(slice_0945, "PE", OFF_P1_PE),
        "p2_ce": pick_row(slice_0945, "CE", OFF_ATM),
        "p2_pe": pick_row(slice_0945, "PE", OFF_ATM),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sensex_dual_short.py -k "pick_row or select_locked" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/sensex_dual_short_backtest.py tests/test_sensex_dual_short.py
git commit -m "feat: offset-based strike selection for SENSEX dual-short"
```

---

### Task 3: Spot range computation

**Files:**
- Modify: `engine/sensex_dual_short_backtest.py`
- Test: `tests/test_sensex_dual_short.py`

**Interfaces:**
- Produces: `compute_range(spot_day: pd.DataFrame) -> tuple[float, float]` — `(range_high, range_low)` over `time_only` in `[09:45, 11:45]` inclusive, using spot `high`/`low`. Returns `(nan, nan)` if the window is empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sensex_dual_short.py  (append)
from datetime import time as dtime
import numpy as np
from engine.sensex_dual_short_backtest import compute_range


def _spot_day(rows):
    # rows: list of (hh, mm, high, low)
    return pd.DataFrame([
        {"time_only": dtime(hh, mm), "high": float(h), "low": float(l)}
        for (hh, mm, h, l) in rows
    ])


def test_compute_range_inclusive_window():
    sd = _spot_day([
        (9, 44, 999, 1),      # before window -> ignored
        (9, 45, 100, 90),     # window start (inclusive)
        (10, 30, 120, 80),
        (11, 45, 110, 70),    # window end (inclusive)
        (11, 46, 500, 5),     # after window -> ignored
    ])
    hi, lo = compute_range(sd)
    assert hi == 120.0 and lo == 70.0


def test_compute_range_empty_window_is_nan():
    sd = _spot_day([(12, 0, 100, 90)])
    hi, lo = compute_range(sd)
    assert np.isnan(hi) and np.isnan(lo)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sensex_dual_short.py -k compute_range -v`
Expected: FAIL with `ImportError`/`AttributeError` (function not defined).

- [ ] **Step 3: Implement `compute_range`**

Append to `engine/sensex_dual_short_backtest.py`:

```python
from datetime import time as _time

RANGE_START = _time(9, 45)
RANGE_END = _time(11, 45)


def compute_range(spot_day: pd.DataFrame) -> tuple:
    """(range_high, range_low) over 09:45-11:45 inclusive; (nan, nan) if empty."""
    w = spot_day[(spot_day["time_only"] >= RANGE_START)
                 & (spot_day["time_only"] <= RANGE_END)]
    if w.empty:
        return float("nan"), float("nan")
    return float(w["high"].max()), float(w["low"].min())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sensex_dual_short.py -k compute_range -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/sensex_dual_short_backtest.py tests/test_sensex_dual_short.py
git commit -m "feat: 09:45-11:45 spot range computation"
```

---

### Task 4: Breakout detection

**Files:**
- Modify: `engine/sensex_dual_short_backtest.py`
- Test: `tests/test_sensex_dual_short.py`

**Interfaces:**
- Produces: `detect_breakouts(spot_day: pd.DataFrame, range_high: float, range_low: float) -> tuple`
  — returns `(put_dt, call_dt)`, each a value from `spot_day["datetime"]` or `None`.
  `put_dt` = first bar (time in `(11:45, 15:28]`) with `high > range_high` → **sell recorded PUT**.
  `call_dt` = first bar with `low < range_low` → **sell recorded CALL**. Each side detected independently; a single engulfing bar sets both.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sensex_dual_short.py  (append)
from engine.sensex_dual_short_backtest import detect_breakouts


def _spot_day_bt(rows):
    # rows: list of (hh, mm, high, low); datetime is a simple label int for identity
    out = []
    for i, (hh, mm, h, l) in enumerate(rows):
        out.append({"datetime": f"{hh:02d}:{mm:02d}", "time_only": dtime(hh, mm),
                    "high": float(h), "low": float(l)})
    return pd.DataFrame(out)


def test_breakout_high_then_low_whipsaw():
    sd = _spot_day_bt([
        (11, 45, 105, 95),    # in-range window edge, not monitored (<= 11:45)
        (12, 0, 106, 96),     # no break
        (12, 30, 111, 96),    # HIGH break -> put
        (14, 10, 104, 89),    # LOW break  -> call
    ])
    put_dt, call_dt = detect_breakouts(sd, range_high=110, range_low=90)
    assert put_dt == "12:30"
    assert call_dt == "14:10"


def test_breakout_engulfing_bar_fires_both():
    sd = _spot_day_bt([(12, 0, 111, 89)])   # one bar breaks both
    put_dt, call_dt = detect_breakouts(sd, range_high=110, range_low=90)
    assert put_dt == "12:00" and call_dt == "12:00"


def test_breakout_none_when_inside_range():
    sd = _spot_day_bt([(12, 0, 109, 91), (13, 0, 108, 92)])
    assert detect_breakouts(sd, 110, 90) == (None, None)


def test_breakout_ignores_1145_and_after_1528():
    sd = _spot_day_bt([(11, 45, 200, 1), (15, 29, 200, 1)])  # both outside monitor window
    assert detect_breakouts(sd, 110, 90) == (None, None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sensex_dual_short.py -k breakout -v`
Expected: FAIL (function not defined).

- [ ] **Step 3: Implement `detect_breakouts`**

Append to `engine/sensex_dual_short_backtest.py`:

```python
MONITOR_START = _time(11, 45)   # strictly after 11:45
SQUARE_OFF = _time(15, 28)


def detect_breakouts(spot_day: pd.DataFrame, range_high: float, range_low: float) -> tuple:
    """First high-break (-> PUT) and first low-break (-> CALL) datetimes after 11:45."""
    mon = spot_day[(spot_day["time_only"] > MONITOR_START)
                   & (spot_day["time_only"] <= SQUARE_OFF)].sort_values("time_only")
    put_dt = None
    call_dt = None
    for _, b in mon.iterrows():
        if put_dt is None and b["high"] > range_high:
            put_dt = b["datetime"]
        if call_dt is None and b["low"] < range_low:
            call_dt = b["datetime"]
        if put_dt is not None and call_dt is not None:
            break
    return put_dt, call_dt
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sensex_dual_short.py -k breakout -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/sensex_dual_short_backtest.py tests/test_sensex_dual_short.py
git commit -m "feat: post-11:45 spot breakout detection"
```

---

### Task 5: Per-leg SL/target/re-entry state machine

**Files:**
- Modify: `engine/sensex_dual_short_backtest.py`
- Test: `tests/test_sensex_dual_short.py`

**Interfaces:**
- Produces: `simulate_leg(highs, lows, closes, entry_idx, entry_cost, eod_idx) -> list[dict]`
  — `highs/lows/closes` are equal-length sequences of the locked contract's 1-min OHLC for the day (index 0 = first bar of the day's series for that contract). `entry_idx` = index of the entry bar (leg filled at `entry_cost`; monitoring starts at `entry_idx+1`). `eod_idx` = last index to consider (the 15:28 / last-available bar). Returns a list of 1 or 2 round-trip dicts:
  `{"entry_kind": "INITIAL"|"REENTRY", "entry_idx": int, "entry_price": float, "exit_idx": int, "exit_price": float, "exit_reason": "SL"|"TARGET"|"EOD", "pnl_points": float}`.
  Re-entry (max 1) arms only after an INITIAL `SL`, and fires on the first `lows[i] <= entry_cost` for `i` in `(sl_exit_idx, eod_idx]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sensex_dual_short.py  (append)
from engine.sensex_dual_short_backtest import simulate_leg

# entry_cost=100 -> SL=125, TGT=10. Index 0 is entry bar (filled at close=100).

def test_leg_target_hit_no_reentry():
    highs = [100, 110, 110, 110]
    lows  = [100,  90,  50,   9]   # bar 3 low 9 <= 10 -> TARGET
    closes= [100, 100, 100, 100]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=3)
    assert len(trips) == 1
    assert trips[0]["exit_reason"] == "TARGET"
    assert trips[0]["exit_price"] == 10.0
    assert trips[0]["pnl_points"] == 90.0        # 100 - 10


def test_leg_sl_then_reentry_then_target():
    #        0    1(SL)  2   3(re) 4(tgt)
    highs = [100, 130,  120, 120,  120]
    lows  = [100, 120,  118, 100,    9]   # bar1 high130>=125 -> SL; bar3 low100<=100 -> re-entry; bar4 low9 -> TARGET
    closes= [100, 100,  100, 100,  100]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=4)
    assert [t["entry_kind"] for t in trips] == ["INITIAL", "REENTRY"]
    assert trips[0]["exit_reason"] == "SL" and trips[0]["exit_price"] == 125.0
    assert trips[0]["pnl_points"] == -25.0        # 100 - 125
    assert trips[1]["entry_price"] == 100.0       # re-sold at original cost
    assert trips[1]["exit_reason"] == "TARGET" and trips[1]["pnl_points"] == 90.0


def test_leg_sl_then_reentry_then_sl_no_third():
    highs = [100, 130, 120, 130, 130]   # bar1 SL, bar3 SL again (after re-entry)
    lows  = [100, 120, 100, 120, 120]   # bar2 low100 -> re-entry
    closes= [100, 100, 100, 100, 100]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=4)
    assert len(trips) == 2
    assert trips[0]["exit_reason"] == "SL"
    assert trips[1]["exit_reason"] == "SL"
    assert sum(t["pnl_points"] for t in trips) == -50.0


def test_leg_same_bar_sl_and_target_sl_wins():
    highs = [100, 130]     # high 130 >= 125 (SL)
    lows  = [100, 9]       # low 9 <= 10 (TGT) same bar -> SL wins
    closes= [100, 100]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=1)
    assert trips[0]["exit_reason"] == "SL"


def test_leg_eod_when_nothing_hits():
    highs = [100, 110, 110]
    lows  = [100,  90,  90]
    closes= [100, 100,  77]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=2)
    assert len(trips) == 1
    assert trips[0]["exit_reason"] == "EOD"
    assert trips[0]["exit_price"] == 77.0
    assert trips[0]["pnl_points"] == 23.0         # 100 - 77


def test_leg_sl_but_never_returns_to_cost_no_reentry():
    highs = [100, 130, 128, 129]   # SL at bar1, stays elevated
    lows  = [100, 126, 127, 128]   # never <= 100
    closes= [100, 127, 127, 128]
    trips = simulate_leg(highs, lows, closes, entry_idx=0, entry_cost=100, eod_idx=3)
    assert len(trips) == 1 and trips[0]["exit_reason"] == "SL"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_sensex_dual_short.py -k leg_ -v`
Expected: FAIL (function not defined).

- [ ] **Step 3: Implement the state machine**

Append to `engine/sensex_dual_short_backtest.py`:

```python
SL_MULT = 1.25
TGT_MULT = 0.10


def _run_one(highs, lows, closes, entry_idx, scan_start, entry_price,
             sl_level, tgt_level, eod_idx):
    """Scan [scan_start, eod_idx] for SL (high>=sl) then TGT (low<=tgt); SL-first.
    Returns a round-trip dict (exit reason SL/TARGET/EOD)."""
    for i in range(scan_start, eod_idx + 1):
        if highs[i] >= sl_level:                       # SL-first tie-break
            return _trip("INITIAL", entry_idx, entry_price, i, sl_level, "SL")
        if lows[i] <= tgt_level:
            return _trip("INITIAL", entry_idx, entry_price, i, tgt_level, "TARGET")
    return _trip("INITIAL", entry_idx, entry_price, eod_idx, closes[eod_idx], "EOD")


def _trip(kind, entry_idx, entry_price, exit_idx, exit_price, reason):
    return {"entry_kind": kind, "entry_idx": entry_idx, "entry_price": float(entry_price),
            "exit_idx": exit_idx, "exit_price": float(exit_price), "exit_reason": reason,
            "pnl_points": float(entry_price) - float(exit_price)}


def simulate_leg(highs, lows, closes, entry_idx, entry_cost, eod_idx):
    """Short-leg SL/target/re-entry state machine. See interface docstring in plan."""
    sl_level = SL_MULT * entry_cost
    tgt_level = TGT_MULT * entry_cost

    trips = []
    initial = _run_one(highs, lows, closes, entry_idx, entry_idx + 1,
                       entry_cost, sl_level, tgt_level, eod_idx)
    trips.append(initial)

    # Re-entry only after an SL exit, and only if there is room before EOD.
    if initial["exit_reason"] == "SL" and initial["exit_idx"] < eod_idx:
        re_idx = None
        for i in range(initial["exit_idx"] + 1, eod_idx + 1):
            if lows[i] <= entry_cost:                  # premium back down to cost
                re_idx = i
                break
        if re_idx is not None:
            re = _run_one(highs, lows, closes, re_idx, re_idx + 1,
                          entry_cost, sl_level, tgt_level, eod_idx)
            re["entry_kind"] = "REENTRY"
            trips.append(re)
    return trips
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_sensex_dual_short.py -k leg_ -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/sensex_dual_short_backtest.py tests/test_sensex_dual_short.py
git commit -m "feat: per-leg SL/target/re-entry state machine"
```

---

### Task 6: Spot loader

**Files:**
- Modify: `engine/sensex_dual_short_backtest.py`
- Test: `tests/test_sensex_dual_short.py`

**Interfaces:**
- Produces: `load_spot(spot_path: str, start_date: str, end_date: str) -> pd.DataFrame`
  — reads the spot parquet, parses `datetime` tz-aware, filters `[start_date, end_date]` inclusive, adds `date` and `time_only` columns, sorts by datetime. (Separate from `data_loader.load_data`, which is options-specific and filters expiry columns spot lacks.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sensex_dual_short.py  (append)
import os
from datetime import date as ddate, time as dtime
from engine.sensex_dual_short_backtest import load_spot
from config import SPOT_DATA_PATH

SPOT_PATH = SPOT_DATA_PATH["SENSEX"]


def test_load_spot_has_helper_cols_and_ist_time():
    sp = load_spot(SPOT_PATH, "2025-09-04", "2025-09-04")   # one Thursday expiry day
    assert {"date", "time_only", "high", "low", "close"}.issubset(sp.columns)
    assert sp["date"].nunique() == 1 and sp["date"].iloc[0] == ddate(2025, 9, 4)
    # IST wall-clock preserved: a 09:45 bar exists (not shifted to UTC 04:15).
    assert (sp["time_only"] == dtime(9, 45)).any()
    assert sp["time_only"].min() >= dtime(9, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sensex_dual_short.py -k load_spot -v`
Expected: FAIL (function not defined).

- [ ] **Step 3: Implement `load_spot`**

Append to `engine/sensex_dual_short_backtest.py`:

```python
def load_spot(spot_path: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Load spot 1-min parquet, filter date range, add date/time_only (IST wall-clock)."""
    df = pd.read_parquet(spot_path)
    df["datetime"] = pd.to_datetime(df["datetime"])            # tz-aware +05:30
    start = pd.to_datetime(start_date).tz_localize("Asia/Kolkata")
    end = pd.to_datetime(end_date).tz_localize("Asia/Kolkata") + pd.Timedelta(days=1)
    df = df[(df["datetime"] >= start) & (df["datetime"] < end)].copy()
    df = df.sort_values("datetime").reset_index(drop=True)
    df["date"] = df["datetime"].dt.date                        # local (IST) date
    df["time_only"] = df["datetime"].dt.time                   # local (IST) wall time
    return df
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sensex_dual_short.py -k load_spot -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/sensex_dual_short_backtest.py tests/test_sensex_dual_short.py
git commit -m "feat: spot 1-min loader with IST helper columns"
```

---

### Task 7: Day processor

**Files:**
- Modify: `engine/sensex_dual_short_backtest.py`
- Test: `tests/test_sensex_dual_short.py`

**Interfaces:**
- Consumes: `select_locked_strikes`, `compute_range`, `detect_breakouts`, `simulate_leg`, and `days_to_weekly_expiry`.
- Produces:
  - `contract_day_bars(day_opts, strike, option_type) -> pd.DataFrame` — that contract's rows sorted by datetime (has `time_only`, `high`, `low`, `close`, `datetime`).
  - `run_leg_from_frame(bars, entry_dt, part, side, strike) -> tuple[list[dict], Optional[str]]` — locate `entry_dt`'s bar, compute `eod_idx` (last bar with `time_only <= 15:28`), call `simulate_leg`, and expand each round-trip into a fully-populated trade dict (date/times/strike/levels/pnl_inr). Relabels `EOD` → `EOD_LAST_AVAILABLE` when the eod bar's `time_only != 15:28`. Returns `([], reason)` when the entry bar or eod bar is unavailable.
  - `process_day(day_opts, spot_day, trading_date, dte) -> tuple[list[dict], dict]` — returns `(trades, skips)` where `trades` is a list of trade dicts and `skips` is `{reason: count}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sensex_dual_short.py  (append)
from datetime import datetime as dt
from engine.sensex_dual_short_backtest import process_day

IST = "Asia/Kolkata"


def _mk_opt_row(t, ot, off, strike, o, h, l, c):
    return {"datetime": pd.Timestamp(t, tz=IST), "time_only": t.time(),
            "date": t.date(), "option_type": ot, "strike_offset": off,
            "strike": float(strike), "open": o, "high": h, "low": l, "close": c,
            "atm_strike": 78300.0}


def _mk_spot_row(t, o, h, l, c):
    return {"datetime": pd.Timestamp(t, tz=IST), "time_only": t.time(),
            "date": t.date(), "open": o, "high": h, "low": l, "close": c}


def test_process_day_part1_target_and_skip_ledger():
    d = ddate(2025, 9, 4)
    # Build a minimal day: only P1 PE (offset -6, strike 77700) is present.
    # P1 CE (+6), and ATM (0) rows are MISSING -> skips.
    opts, spot = [], []
    times = [dt(2025, 9, 4, 9, 45), dt(2025, 9, 4, 9, 46), dt(2025, 9, 4, 15, 28)]
    # P1 PE priced 100 at entry, decays to 9 (<=10 target) at 09:46.
    pe_prices = {times[0]: (100, 100, 100, 100),
                 times[1]: (100, 100, 9, 50),
                 times[2]: (50, 50, 50, 50)}
    for t in times:
        o, h, l, c = pe_prices[t]
        opts.append(_mk_opt_row(t, "PE", -6, 77700, o, h, l, c))
        spot.append(_mk_spot_row(t, 78300, 78300, 78300, 78300))
    day_opts = pd.DataFrame(opts)
    spot_day = pd.DataFrame(spot)

    trades, skips = process_day(day_opts, spot_day, d, dte=0)

    # One P1 PE leg, target hit, +90 points * 20 = +1800.
    pe_trades = [t for t in trades if t["part"] == "P1" and t["side"] == "PE"]
    assert len(pe_trades) == 1
    assert pe_trades[0]["exit_reason"] == "TARGET"
    assert pe_trades[0]["pnl_inr"] == 1800.0
    # Missing P1 CE and missing ATM (disables both P2 legs) show in skip ledger.
    assert skips.get("P1_CE_UNAVAILABLE_0945") == 1
    assert skips.get("P2_ATM_NOT_RECORDABLE_0945") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sensex_dual_short.py -k process_day -v`
Expected: FAIL (function not defined).

- [ ] **Step 3: Implement the day processor**

Append to `engine/sensex_dual_short_backtest.py`:

```python
from config import LOT_SIZE

LOT = LOT_SIZE[INSTRUMENT]
ENTRY_TIME = _time(9, 45)


def contract_day_bars(day_opts: pd.DataFrame, strike: float, option_type: str) -> pd.DataFrame:
    """That locked contract's rows for the day, sorted by datetime."""
    c = day_opts[(day_opts["strike"] == strike)
                 & (day_opts["option_type"] == option_type)]
    return c.sort_values("datetime").reset_index(drop=True)


def _bump(skips: dict, reason: str) -> None:
    skips[reason] = skips.get(reason, 0) + 1


def run_leg_from_frame(bars: pd.DataFrame, entry_dt, part, side, strike):
    """Expand a locked-contract frame into trade dicts. Returns (trades, skip_reason|None)."""
    if bars.empty:
        return [], "NO_CONTRACT_BARS"
    idx = bars.index[bars["datetime"] == entry_dt]
    if len(idx) == 0:
        return [], "ENTRY_BAR_MISSING"
    entry_pos = int(idx[0])

    eod_mask = bars["time_only"] <= SQUARE_OFF
    if not eod_mask.any():
        return [], "NO_EOD_BAR"
    eod_pos = int(bars.index[eod_mask][-1])
    if eod_pos <= entry_pos:            # entered at/after square-off -> no room
        eod_pos = entry_pos

    highs = bars["high"].to_numpy()
    lows = bars["low"].to_numpy()
    closes = bars["close"].to_numpy()
    entry_cost = float(bars["close"].iloc[entry_pos])

    trips = simulate_leg(highs, lows, closes, entry_pos, entry_cost, eod_pos)

    eod_is_square_off = bars["time_only"].iloc[eod_pos] == SQUARE_OFF
    trade_date = bars["date"].iloc[entry_pos]
    sl_level = SL_MULT * entry_cost
    tgt_level = TGT_MULT * entry_cost
    trades = []
    for leg_id_suffix, tr in enumerate(trips):
        reason = tr["exit_reason"]
        if reason == "EOD" and not eod_is_square_off:
            reason = "EOD_LAST_AVAILABLE"
        trades.append({
            "date": trade_date, "part": part, "side": side, "strike": strike,
            "leg_id": f"{trade_date}_{part}_{side}_{strike:.0f}",
            "entry_kind": tr["entry_kind"],
            "entry_time": bars["datetime"].iloc[tr["entry_idx"]],
            "entry_cost": tr["entry_price"],
            "sl_level": sl_level, "target_level": tgt_level,
            "exit_time": bars["datetime"].iloc[tr["exit_idx"]],
            "exit_price": tr["exit_price"], "exit_reason": reason,
            "pnl_points": tr["pnl_points"], "pnl_inr": tr["pnl_points"] * LOT,
        })
    return trades, None


def process_day(day_opts: pd.DataFrame, spot_day: pd.DataFrame, trading_date, dte):
    """Run both parts for one trading day. Returns (trades, skips)."""
    trades, skips = [], {}

    slice_0945 = day_opts[day_opts["time_only"] == ENTRY_TIME]
    if slice_0945.empty:
        _bump(skips, "NO_0945_BAR")
        return trades, skips

    picks = select_locked_strikes(slice_0945)

    # --- Part 1: enter now at 09:45 close ---
    entry_dt_0945 = slice_0945["datetime"].iloc[0]
    for side, key, miss in (("CE", "p1_ce", "P1_CE_UNAVAILABLE_0945"),
                            ("PE", "p1_pe", "P1_PE_UNAVAILABLE_0945")):
        row = picks[key]
        if row is None:
            _bump(skips, miss)
            continue
        bars = contract_day_bars(day_opts, float(row["strike"]), side)
        leg_trades, skip = run_leg_from_frame(bars, entry_dt_0945, "P1", side, float(row["strike"]))
        if skip:
            _bump(skips, miss)
        trades.extend(leg_trades)

    # --- Part 2: record ATM strikes, then breakout after 11:45 ---
    if picks["p2_ce"] is None or picks["p2_pe"] is None:
        _bump(skips, "P2_ATM_NOT_RECORDABLE_0945")
        return trades, skips

    range_high, range_low = compute_range(spot_day)
    if pd.isna(range_high) or pd.isna(range_low):
        _bump(skips, "NO_RANGE_WINDOW")
        return trades, skips

    put_dt, call_dt = detect_breakouts(spot_day, range_high, range_low)

    # High break -> sell recorded ATM PUT.
    if put_dt is not None:
        strike = float(picks["p2_pe"]["strike"])
        bars = contract_day_bars(day_opts, strike, "PE")
        leg_trades, skip = run_leg_from_frame(bars, put_dt, "P2", "PE", strike)
        if skip:
            _bump(skips, "P2_PUT_UNAVAILABLE_BREAKOUT")
        trades.extend(leg_trades)

    # Low break -> sell recorded ATM CALL.
    if call_dt is not None:
        strike = float(picks["p2_ce"]["strike"])
        bars = contract_day_bars(day_opts, strike, "CE")
        leg_trades, skip = run_leg_from_frame(bars, call_dt, "P2", "CE", strike)
        if skip:
            _bump(skips, "P2_CALL_UNAVAILABLE_BREAKOUT")
        trades.extend(leg_trades)

    return trades, skips
```

Note: `run_leg_from_frame` matches the entry bar by exact `datetime` equality. `put_dt`/`call_dt` come from `spot_day["datetime"]`; the options contract must have a row at that same timestamp. If the spot and options minute grids differ at that minute, the entry bar won't match → the leg is skipped and counted (`P2_*_UNAVAILABLE_BREAKOUT`). This is the intended "missing strike at the needed minute → skip" behaviour.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sensex_dual_short.py -k process_day -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/sensex_dual_short_backtest.py tests/test_sensex_dual_short.py
git commit -m "feat: per-day processor for SENSEX dual-short (both parts + skip ledger)"
```

---

### Task 8: Engine class + run loop

**Files:**
- Modify: `engine/sensex_dual_short_backtest.py`
- Test: `tests/test_sensex_dual_short.py`

**Interfaces:**
- Consumes: `load_data` (options), `load_spot`, `days_to_weekly_expiry`, `process_day`.
- Produces: class `SensexDualShortBacktest(start_date, end_date, options_path=None, spot_path=None)` with:
  - `.run() -> None` — loops trading days present in both feeds; for each with `dte ∈ {0,1,2,3}` calls `process_day`; accumulates `self.trades` (list of dicts) and `self.skips` (dict), plus `self.non_trading_days` (count of `dte ∉ {0,1,2,3}` days).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sensex_dual_short.py  (append)
from engine.sensex_dual_short_backtest import SensexDualShortBacktest


def test_engine_runs_small_window_and_gates_dte():
    # One expiry-week window: 2025-09-01 (Mon, DTE 3) .. 2025-09-05 (Fri, DTE 6 -> skipped).
    eng = SensexDualShortBacktest("2025-09-01", "2025-09-05")
    eng.run()
    # Trades exist and every trade carries required columns.
    assert isinstance(eng.trades, list)
    if eng.trades:
        t = eng.trades[0]
        assert {"date", "part", "side", "strike", "entry_kind", "entry_cost",
                "sl_level", "target_level", "exit_reason", "pnl_inr"}.issubset(t)
    # 2025-09-05 (DTE 6) must not produce trades.
    assert all(str(t["date"]) != "2025-09-05" for t in eng.trades)
    assert eng.non_trading_days >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sensex_dual_short.py -k engine_runs -v`
Expected: FAIL (class not defined).

- [ ] **Step 3: Implement the engine**

Append to `engine/sensex_dual_short_backtest.py` (add imports at top of file: `import os`, `import logging`, `from engine.data_loader import load_data`, `from engine.expiry_calendar import days_to_weekly_expiry`, `from config import DATA_PATH, SPOT_DATA_PATH`):

```python
logger = logging.getLogger(__name__)


class SensexDualShortBacktest:
    def __init__(self, start_date, end_date, options_path=None, spot_path=None):
        self.start_date = start_date
        self.end_date = end_date
        self.options_path = options_path or DATA_PATH[INSTRUMENT]
        self.spot_path = spot_path or SPOT_DATA_PATH[INSTRUMENT]
        self.trades = []
        self.skips = {}
        self.non_trading_days = 0

    def run(self):
        opts = load_data(self.options_path, self.start_date, self.end_date, "weekly")
        spot = load_spot(self.spot_path, self.start_date, self.end_date)

        opts_by_day = dict(tuple(opts.groupby("date")))
        spot_by_day = dict(tuple(spot.groupby("date")))

        for d in sorted(set(opts_by_day) & set(spot_by_day)):
            dte = days_to_weekly_expiry(INSTRUMENT, d)
            if dte is None or dte not in (0, 1, 2, 3):
                self.non_trading_days += 1
                continue
            day_trades, day_skips = process_day(opts_by_day[d], spot_by_day[d], d, dte)
            for t in day_trades:
                t["dte"] = dte
            self.trades.extend(day_trades)
            for reason, n in day_skips.items():
                self.skips[reason] = self.skips.get(reason, 0) + n

        logger.info("Backtest complete: %d trades, %d non-trading days, skips=%s",
                    len(self.trades), self.non_trading_days, self.skips)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sensex_dual_short.py -k engine_runs -v`
Expected: PASS. (Takes a few seconds — reads the parquet files.)

- [ ] **Step 5: Commit**

```bash
git add engine/sensex_dual_short_backtest.py tests/test_sensex_dual_short.py
git commit -m "feat: SENSEX dual-short engine run loop with DTE gate"
```

---

### Task 9: Summary / skip-ledger reporting

**Files:**
- Modify: `engine/sensex_dual_short_backtest.py`
- Test: `tests/test_sensex_dual_short.py`

**Interfaces:**
- Produces: `summarize(trades: list[dict], skips: dict, non_trading_days: int) -> dict`
  — returns totals: `total_pnl_inr`, `p1_pnl_inr`, `p2_pnl_inr`, `n_legs` (distinct `leg_id`),
  `n_round_trips` (len trades), `n_reentries` (round-trips with `entry_kind=="REENTRY"`),
  `win_rate` (fraction of distinct legs with net `pnl_inr > 0`), `avg_win_inr`, `avg_loss_inr`,
  `exit_reason_counts`, `skips`, `non_trading_days`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sensex_dual_short.py  (append)
from engine.sensex_dual_short_backtest import summarize


def test_summarize_aggregates_by_leg():
    trades = [
        # leg A (P1 CE): SL -25*20=-500 then REENTRY target +90*20=+1800 -> net +1300 (win)
        {"leg_id": "A", "part": "P1", "entry_kind": "INITIAL", "exit_reason": "SL",
         "pnl_points": -25, "pnl_inr": -500.0},
        {"leg_id": "A", "part": "P1", "entry_kind": "REENTRY", "exit_reason": "TARGET",
         "pnl_points": 90, "pnl_inr": 1800.0},
        # leg B (P2 PE): single SL -500 (loss)
        {"leg_id": "B", "part": "P2", "entry_kind": "INITIAL", "exit_reason": "SL",
         "pnl_points": -25, "pnl_inr": -500.0},
    ]
    s = summarize(trades, {"P1_CE_UNAVAILABLE_0945": 2}, non_trading_days=10)
    assert s["total_pnl_inr"] == 800.0
    assert s["p1_pnl_inr"] == 1300.0 and s["p2_pnl_inr"] == -500.0
    assert s["n_legs"] == 2 and s["n_round_trips"] == 3 and s["n_reentries"] == 1
    assert s["win_rate"] == 0.5          # leg A win, leg B loss
    assert s["exit_reason_counts"]["SL"] == 2
    assert s["skips"]["P1_CE_UNAVAILABLE_0945"] == 2
    assert s["non_trading_days"] == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sensex_dual_short.py -k summarize -v`
Expected: FAIL (function not defined).

- [ ] **Step 3: Implement `summarize`**

Append to `engine/sensex_dual_short_backtest.py`:

```python
def summarize(trades, skips, non_trading_days):
    total = sum(t["pnl_inr"] for t in trades)
    p1 = sum(t["pnl_inr"] for t in trades if t["part"] == "P1")
    p2 = sum(t["pnl_inr"] for t in trades if t["part"] == "P2")

    leg_net = {}
    for t in trades:
        leg_net[t["leg_id"]] = leg_net.get(t["leg_id"], 0.0) + t["pnl_inr"]
    nets = list(leg_net.values())
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]

    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    return {
        "total_pnl_inr": total,
        "p1_pnl_inr": p1,
        "p2_pnl_inr": p2,
        "n_legs": len(nets),
        "n_round_trips": len(trades),
        "n_reentries": sum(1 for t in trades if t["entry_kind"] == "REENTRY"),
        "win_rate": (len(wins) / len(nets)) if nets else 0.0,
        "avg_win_inr": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss_inr": (sum(losses) / len(losses)) if losses else 0.0,
        "exit_reason_counts": reasons,
        "skips": dict(skips),
        "non_trading_days": non_trading_days,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sensex_dual_short.py -k summarize -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/sensex_dual_short_backtest.py tests/test_sensex_dual_short.py
git commit -m "feat: summary + skip-ledger aggregation"
```

---

### Task 10: CLI runner

**Files:**
- Create: `run_sensex_dual_short.py` (repo root)
- (No new unit test; verified by a smoke run.)

**Interfaces:**
- Consumes: `SensexDualShortBacktest`, `summarize`.
- Produces: `sensex_dual_short_trades.csv` + a printed summary. CLI args `--start`, `--end`, `--out` with defaults for the full window.

- [ ] **Step 1: Create the runner**

```python
# run_sensex_dual_short.py
"""CLI runner for the SENSEX dual short-premium backtest.

Usage:
    python run_sensex_dual_short.py                       # full window
    python run_sensex_dual_short.py --start 2025-09-01 --end 2025-09-30
"""
import argparse
import logging
import pandas as pd

from engine.sensex_dual_short_backtest import SensexDualShortBacktest, summarize

DEFAULT_START = "2023-05-15"
DEFAULT_END = "2026-04-23"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--out", default="sensex_dual_short_trades.csv")
    args = ap.parse_args()

    eng = SensexDualShortBacktest(args.start, args.end)
    eng.run()

    df = pd.DataFrame(eng.trades)
    df.to_csv(args.out, index=False)

    s = summarize(eng.trades, eng.skips, eng.non_trading_days)
    print("\n===== SENSEX Dual Short-Premium Backtest =====")
    print(f"Window: {args.start} -> {args.end}")
    print(f"Total P&L:  Rs {s['total_pnl_inr']:,.0f}")
    print(f"  Part 1:   Rs {s['p1_pnl_inr']:,.0f}")
    print(f"  Part 2:   Rs {s['p2_pnl_inr']:,.0f}")
    print(f"Legs: {s['n_legs']} | round-trips: {s['n_round_trips']} | re-entries: {s['n_reentries']}")
    print(f"Win rate (per leg): {s['win_rate']*100:.1f}%")
    print(f"Avg win: Rs {s['avg_win_inr']:,.0f} | Avg loss: Rs {s['avg_loss_inr']:,.0f}")
    print(f"Exit reasons: {s['exit_reason_counts']}")
    print(f"Non-trading days (DTE not 0-3): {s['non_trading_days']}")
    print(f"Skip ledger: {s['skips']}")
    print(f"\nTrades written to {args.out} ({len(df)} rows)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-run on a small window**

Run: `python run_sensex_dual_short.py --start 2025-09-01 --end 2025-09-30 --out /tmp/smoke.csv`
Expected: prints a summary block with a non-zero leg count and writes `/tmp/smoke.csv`. No traceback.

- [ ] **Step 3: Inspect the CSV columns**

Run: `python3 -c "import pandas as pd; df=pd.read_csv('/tmp/smoke.csv'); print(df.columns.tolist()); print(df.head(8).to_string())"`
Expected: columns include `date, dte, leg_id, part, side, strike, entry_kind, entry_time, entry_cost, sl_level, target_level, exit_time, exit_price, exit_reason, pnl_points, pnl_inr`.

- [ ] **Step 4: Commit**

```bash
git add run_sensex_dual_short.py
git commit -m "feat: CLI runner for SENSEX dual-short backtest"
```

---

### Task 11: Full run + hand-verification (correctness gate)

**Files:**
- (No code changes; produces `sensex_dual_short_trades.csv` and a short verification note.)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Run the full backtest**

Run: `python run_sensex_dual_short.py`
Expected: completes without error; prints the summary; writes `sensex_dual_short_trades.csv`. Sanity-check the print: total legs is in the thousands (≈ 4 legs/day × trading days), non-trading days ≈ one per week.

- [ ] **Step 2: Verify DTE 0 sells the expiring contract (no roll)**

Run: `python3 -c "import pandas as pd; from engine.data_loader import load_data; d=load_data('data/options/sensex/SENSEX_OPTIONS_1m.parquet','2025-09-04','2025-09-04','weekly'); print(d[['strike','option_type','expiry_code','strike_offset']].head()); print('rows:', len(d))"`
Expected: on the 2025-09-04 expiry day, `code=1 WEEK` rows exist and represent that day's expiring contract (there ARE rows at 09:45). Confirms the seller trades the expiring weekly.

- [ ] **Step 3: Hand-verify one Part-1 leg**

Pick one day+leg from the CSV (e.g. a P1 PE with a TARGET exit). Independently pull that contract's bars and confirm entry_cost = its 09:45 close, target = 0.10×entry_cost, exit bar = first minute its low ≤ target, pnl_points = entry_cost − exit_price.

Run (substitute DATE / STRIKE from the chosen CSV row):
```bash
python3 -c "
import pandas as pd
from engine.data_loader import load_data
d = load_data('data/options/sensex/SENSEX_OPTIONS_1m.parquet','DATE','DATE','weekly')
c = d[(d.strike==STRIKE)&(d.option_type=='PE')].sort_values('datetime')
print(c[['time_only','open','high','low','close']].head(30).to_string())
"
```
Expected: the engine's `entry_cost`, `target_level`, `exit_time`, `exit_price`, and `pnl_points` for that CSV row match the manual read exactly.

- [ ] **Step 4: Hand-verify one Part-2 breakout leg**

Pick a day with a P2 trade. Independently compute the 09:45–11:45 spot high/low, find the first post-11:45 breakout minute, confirm the engine's P2 `entry_time` equals that minute and `entry_cost` equals the recorded ATM strike's close at that minute.

Run (substitute DATE):
```bash
python3 -c "
import pandas as pd
from engine.sensex_dual_short_backtest import load_spot, compute_range, detect_breakouts
sp = load_spot('data/spot/sensex/SENSEX_1m.parquet','DATE','DATE')
hi, lo = compute_range(sp)
print('range', hi, lo)
print('breakouts (put_dt, call_dt):', detect_breakouts(sp, hi, lo))
"
```
Expected: matches the P2 rows in the CSV for that date.

- [ ] **Step 5: Run the whole test suite**

Run: `pytest tests/test_sensex_dual_short.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit the results artifact (optional)**

```bash
git add sensex_dual_short_trades.csv
git commit -m "chore: SENSEX dual-short backtest results (full window)"
```

---

## Deferred (not in this plan)

Streamlit UI tab + `saved_strategies/sensex_dual_short.json` wiring — do after the numbers are trusted (per spec §12).

## Self-Review

- **Spec coverage:** §1 overview → Tasks 2,7; delta mapping → Task 2; §3 data/tz → Tasks 6,8 + Global Constraints; §4 components → all tasks; §5 pipeline → Task 7; §5 DTE gate → Tasks 1,8; §6 strike locking → Tasks 2,7 (`contract_day_bars` monitors the locked absolute strike only); §7 leg manager + re-entry → Task 5; §8 skips/edge cases → Task 7 (skip ledger, `EOD_LAST_AVAILABLE` relabel, entry-bar-missing skips); §9 assumptions (no slippage, DTE, lot 20) → Global Constraints + Tasks 5,8; §10 tests → every task + Task 11; §11 outputs → Tasks 9,10; §12 sequencing → task order.
- **Placeholder scan:** none — every code/test step has concrete content; `DATE`/`STRIKE` in Task 11 are explicitly "substitute from the chosen CSV row" verification placeholders, not code gaps.
- **Type consistency:** trade dicts carry the same keys across Tasks 7/9/10; `simulate_leg` round-trip dict keys (`entry_kind, entry_idx, entry_price, exit_idx, exit_price, exit_reason, pnl_points`) are consumed unchanged in `run_leg_from_frame`; `select_locked_strikes` keys (`p1_ce/p1_pe/p2_ce/p2_pe`) used identically in `process_day`; `SQUARE_OFF`/`RANGE_*`/`MONITOR_START` time constants defined once and reused.
