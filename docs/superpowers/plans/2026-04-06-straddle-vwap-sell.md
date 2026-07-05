# ATM Straddle VWAP Sell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a backtest engine that sells NIFTY ATM straddles at VWAP level, with 2% TP / 3.5% SL on the combined straddle price.

**Architecture:** Single engine file (`engine/straddle_vwap_backtest.py`) that loads 1-min options data, constructs straddle OHLCV per minute, calculates VWAP on straddle close with combined volume, detects crossover entries, and monitors SL/TP/EOD exits. UI runner follows the existing pattern.

**Tech Stack:** Python, pandas, numpy, Streamlit, existing `engine/data_loader.py` and `config.py` utilities.

**Spec:** `docs/superpowers/specs/2026-04-06-straddle-vwap-sell-design.md`

---

### Task 1: Engine — Trade Dataclass and Scaffolding

**Files:**
- Create: `engine/straddle_vwap_backtest.py`

- [ ] **Step 1: Create the engine file with imports, dataclass, and empty engine class**

```python
"""
ATM Straddle VWAP Sell Backtest Engine.

Strategy:
  Sell ATM straddle (CE + PE) at nearest weekly expiry when straddle
  price crosses VWAP from either direction. Entry at VWAP(T-1) value.

  VWAP: calculated on straddle_close (CE close + PE close) with combined
  volume (CE vol + PE vol). Session reset daily, no bands.

  Exit:
    1. SL: straddle_close >= entry × 1.035 → exit at exact SL level
    2. TP: straddle_close <= entry × 0.98  → exit at exact TP level
    3. EOD: 14:30 → exit at straddle_close

  Time: Entry 11:00-14:30. VWAP builds from 09:15.
  Re-entry allowed after exit.
"""

import logging
import os
from dataclasses import asdict, dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

from config import (
    DATA_PATH,
    LOT_SIZE,
    STRIKE_ROUNDING,
    get_nearest_weekly_expiry,
)
from engine.data_loader import load_data

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------

@dataclass
class StraddleVwapTrade:
    date: str                    # "YYYY-MM-DD"
    strike: float
    expiry_date: str

    entry_time: str              # "HH:MM" when crossover detected
    entry_price: float           # VWAP(T-1) value (straddle combined)
    vwap_at_entry: float         # same as entry_price
    straddle_at_entry: float     # straddle_close at crossover candle
    ce_entry_price: float        # CE close at entry for reference
    pe_entry_price: float        # PE close at entry for reference
    qty: int

    tp_level: float              # entry × 0.98
    sl_level: float              # entry × 1.035

    exit_time: str
    exit_price: float            # exact SL/TP level or straddle_close at EOD
    exit_reason: str             # "TP" / "SL" / "EOD"

    pnl_points: float            # entry_price - exit_price (selling)
    pnl_pct: float
    pnl_inr: float               # pnl_points × qty


def trades_to_dataframe(trades: List[StraddleVwapTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
```

- [ ] **Step 2: Verify file is importable**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "from engine.straddle_vwap_backtest import StraddleVwapTrade, trades_to_dataframe; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

Suggested message: `feat(engine): add StraddleVwapTrade dataclass for straddle VWAP sell engine`

---

### Task 2: Engine — Data Loading and Straddle Construction

**Files:**
- Modify: `engine/straddle_vwap_backtest.py`

- [ ] **Step 1: Add the engine class with __init__ and data loading**

Append to `engine/straddle_vwap_backtest.py`:

```python
# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class StraddleVwapBacktestEngine:
    """ATM Straddle VWAP sell strategy backtest engine."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        tp_pct: float = 2.0,
        sl_pct: float = 3.5,
        entry_start: str = "11:00",
        force_exit_time: str = "14:30",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.entry_start = entry_start
        self.force_exit_time = force_exit_time
        self.instrument = "NIFTY"
        self.lot_size = LOT_SIZE.get("NIFTY", 75)

        self._options_1m: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _prepare_data(self):
        """Load options data."""
        logger.info("Loading options data...")
        options_path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        self._options_1m = load_data(
            options_path, self.start_date, self.end_date, "weekly"
        )
        logger.info(f"Options 1m: {len(self._options_1m):,} rows")

    def _build_straddle_series(self, day_options: pd.DataFrame) -> pd.DataFrame:
        """Build straddle OHLCV series for a single day.

        For each minute, finds the ATM strike (from spot column),
        gets CE and PE at that strike, and combines them.

        Returns DataFrame indexed by datetime with columns:
            straddle_close, straddle_open, straddle_volume,
            spot, strike, ce_close, pe_close
        """
        rounding = STRIKE_ROUNDING.get(self.instrument, 50)
        rows = []

        for dt, minute_data in day_options.groupby("datetime"):
            if minute_data.empty:
                continue

            # ATM strike from spot
            spot = minute_data.iloc[0]["spot"]
            atm_strike = round(spot / rounding) * rounding

            ce = minute_data[
                (minute_data["strike"] == atm_strike)
                & (minute_data["option_type"] == "CE")
            ]
            pe = minute_data[
                (minute_data["strike"] == atm_strike)
                & (minute_data["option_type"] == "PE")
            ]

            if ce.empty or pe.empty:
                continue

            ce_row = ce.iloc[0]
            pe_row = pe.iloc[0]

            rows.append({
                "datetime": dt,
                "straddle_close": round(ce_row["close"] + pe_row["close"], 2),
                "straddle_open": round(ce_row["open"] + pe_row["open"], 2),
                "straddle_volume": ce_row["volume"] + pe_row["volume"],
                "spot": spot,
                "strike": atm_strike,
                "ce_close": ce_row["close"],
                "pe_close": pe_row["close"],
                "time_str": dt.strftime("%H:%M") if hasattr(dt, "strftime") else str(dt)[11:16],
            })

        if not rows:
            return pd.DataFrame()

        return pd.DataFrame(rows).set_index("datetime")
```

- [ ] **Step 2: Verify import**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "from engine.straddle_vwap_backtest import StraddleVwapBacktestEngine; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

Suggested message: `feat(engine): add data loading and straddle construction for VWAP sell engine`

---

### Task 3: Engine — VWAP Calculation and Core Loop

**Files:**
- Modify: `engine/straddle_vwap_backtest.py`

- [ ] **Step 1: Add VWAP calculation and the run/process_day methods**

Add these methods to `StraddleVwapBacktestEngine`:

```python
    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[StraddleVwapTrade]:
        """Run backtest. Returns list of StraddleVwapTrade."""
        self._prepare_data()

        all_dates = sorted(self._options_1m["date"].unique())
        trades: List[StraddleVwapTrade] = []

        for day_idx, trading_date in enumerate(all_dates):
            if progress_callback:
                progress_callback(day_idx, len(all_dates), str(trading_date))

            day_trades = self._process_day(trading_date)
            trades.extend(day_trades)

        logger.info(f"Backtest complete: {len(trades)} trades")
        return trades

    def _process_day(self, trading_date) -> List[StraddleVwapTrade]:
        """Process a single trading day. Returns 0 or more trades."""
        day_options = self._options_1m[self._options_1m["date"] == trading_date]
        if day_options.empty:
            return []

        expiry_date = get_nearest_weekly_expiry(trading_date)
        if expiry_date is None:
            return []

        # Build straddle series for the day
        straddle = self._build_straddle_series(day_options)
        if straddle.empty or len(straddle) < 3:
            return []

        # Calculate VWAP on straddle close with combined volume
        straddle = straddle.copy()
        straddle["cum_pv"] = (straddle["straddle_close"] * straddle["straddle_volume"]).cumsum()
        straddle["cum_v"] = straddle["straddle_volume"].cumsum()
        straddle["vwap"] = straddle["cum_pv"] / straddle["cum_v"]
        # Handle zero volume: forward-fill VWAP
        straddle["vwap"] = straddle["vwap"].ffill()

        # Pre-group options by datetime for exit monitoring at fixed strike
        opt_by_dt = {dt: grp for dt, grp in day_options.groupby("datetime")}

        trades: List[StraddleVwapTrade] = []

        # State
        in_position = False
        entry_price = None
        entry_strike = None
        tp_level = None
        sl_level = None
        entry_time = None
        ce_entry = None
        pe_entry = None
        straddle_at_entry = None

        # Need previous candle values for crossover detection
        prev_straddle_close = None
        prev_vwap = None

        for idx, row in straddle.iterrows():
            t_str = row["time_str"]
            t_dt = idx
            sc = row["straddle_close"]
            vwap = row["vwap"]

            # ============ 1. EXIT CHECKS (at fixed entry strike) ============
            if in_position:
                # Get CE + PE close at the FIXED entry strike
                minute_opts = opt_by_dt.get(t_dt)
                if minute_opts is not None:
                    ce_at_strike = minute_opts[
                        (minute_opts["strike"] == entry_strike)
                        & (minute_opts["option_type"] == "CE")
                    ]
                    pe_at_strike = minute_opts[
                        (minute_opts["strike"] == entry_strike)
                        & (minute_opts["option_type"] == "PE")
                    ]
                    if not ce_at_strike.empty and not pe_at_strike.empty:
                        fixed_straddle = round(
                            ce_at_strike.iloc[0]["close"] + pe_at_strike.iloc[0]["close"], 2
                        )

                        # 1a. SL hit
                        if fixed_straddle >= sl_level:
                            trade = self._make_trade(
                                trading_date, entry_strike, expiry_date,
                                entry_time, entry_price, straddle_at_entry,
                                ce_entry, pe_entry, tp_level, sl_level,
                                t_str, round(sl_level, 2), "SL",
                            )
                            trades.append(trade)
                            in_position = False

                        # 1b. TP hit
                        elif fixed_straddle <= tp_level:
                            trade = self._make_trade(
                                trading_date, entry_strike, expiry_date,
                                entry_time, entry_price, straddle_at_entry,
                                ce_entry, pe_entry, tp_level, sl_level,
                                t_str, round(tp_level, 2), "TP",
                            )
                            trades.append(trade)
                            in_position = False

                        # 1c. EOD force exit
                        elif t_str >= self.force_exit_time:
                            trade = self._make_trade(
                                trading_date, entry_strike, expiry_date,
                                entry_time, entry_price, straddle_at_entry,
                                ce_entry, pe_entry, tp_level, sl_level,
                                t_str, round(fixed_straddle, 2), "EOD",
                            )
                            trades.append(trade)
                            in_position = False

                # Skip entry logic if still in position or past EOD
                if in_position:
                    prev_straddle_close = sc
                    prev_vwap = vwap
                    continue

            # ============ 2. ENTRY DETECTION ============
            if (not in_position
                    and prev_straddle_close is not None
                    and prev_vwap is not None
                    and t_str >= self.entry_start
                    and t_str < self.force_exit_time
                    and not pd.isna(vwap)
                    and not pd.isna(prev_vwap)):

                # Crossover: straddle_close crossed VWAP(T-1) from either direction
                cross_above = prev_straddle_close < prev_vwap and sc >= prev_vwap
                cross_below = prev_straddle_close > prev_vwap and sc <= prev_vwap
                # Also handle touching exactly
                crossed = cross_above or cross_below

                if crossed:
                    # Entry at VWAP(T-1) value (limit order)
                    entry_price = round(prev_vwap, 2)
                    rounding = STRIKE_ROUNDING.get(self.instrument, 50)
                    entry_strike = round(row["spot"] / rounding) * rounding
                    tp_level = round(entry_price * (1 - self.tp_pct / 100), 2)
                    sl_level = round(entry_price * (1 + self.sl_pct / 100), 2)
                    entry_time = t_str
                    ce_entry = row["ce_close"]
                    pe_entry = row["pe_close"]
                    straddle_at_entry = sc
                    in_position = True

                    logger.debug(
                        f"{trading_date} {t_str} SELL STRADDLE "
                        f"strike={entry_strike} at VWAP={entry_price} "
                        f"TP={tp_level} SL={sl_level}"
                    )

            prev_straddle_close = sc
            prev_vwap = vwap

        # Safety net: force close if still in position
        if in_position:
            last_row = straddle.iloc[-1]
            trade = self._make_trade(
                trading_date, entry_strike, expiry_date,
                entry_time, entry_price, straddle_at_entry,
                ce_entry, pe_entry, tp_level, sl_level,
                last_row["time_str"], round(last_row["straddle_close"], 2), "EOD",
            )
            trades.append(trade)

        return trades

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_trade(
        self, trading_date, strike, expiry_date,
        entry_time, entry_price, straddle_at_entry,
        ce_entry, pe_entry, tp_level, sl_level,
        exit_time, exit_price, exit_reason,
    ) -> StraddleVwapTrade:
        # Selling: profit when price drops
        pnl_points = round(entry_price - exit_price, 2)
        pnl_pct = round(pnl_points / entry_price * 100, 3) if entry_price else 0.0
        pnl_inr = round(pnl_points * self.lot_size, 2)
        return StraddleVwapTrade(
            date=str(trading_date),
            strike=strike,
            expiry_date=str(expiry_date),
            entry_time=entry_time,
            entry_price=entry_price,
            vwap_at_entry=entry_price,
            straddle_at_entry=straddle_at_entry,
            ce_entry_price=ce_entry,
            pe_entry_price=pe_entry,
            qty=self.lot_size,
            tp_level=tp_level,
            sl_level=sl_level,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl_points=pnl_points,
            pnl_pct=pnl_pct,
            pnl_inr=pnl_inr,
        )
```

- [ ] **Step 2: Verify the full engine is importable**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "from engine.straddle_vwap_backtest import StraddleVwapBacktestEngine; e = StraddleVwapBacktestEngine('2025-03-01','2025-03-05'); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

Suggested message: `feat(engine): implement VWAP calculation and core loop for straddle sell engine`

---

### Task 4: UI Runner — Streamlit Page

**Files:**
- Create: `ui/straddle_vwap_backtest_runner.py`

- [ ] **Step 1: Create the full UI runner**

```python
"""
ATM Straddle VWAP Sell Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.straddle_vwap_backtest import StraddleVwapBacktestEngine, trades_to_dataframe


def render_straddle_vwap_backtest():
    st.header("ATM Straddle VWAP Sell")
    st.caption(
        "Sell ATM straddle at VWAP crossover  |  "
        "TP: 2% drop  |  SL: 3.5% rise  |  11:00 - 14:30"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-20").date(), key="sv_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-12").date(), key="sv_end"
            )

        with col2:
            st.markdown("**Exit**")
            tp_pct = st.number_input(
                "TP (%)", value=2.0, step=0.5, min_value=0.1, key="sv_tp"
            )
            sl_pct = st.number_input(
                "SL (%)", value=3.5, step=0.5, min_value=0.1, key="sv_sl"
            )

    with st.expander("Time Windows", expanded=False):
        tc1, tc2 = st.columns(2)
        with tc1:
            entry_start = st.text_input(
                "Entry window start", value="11:00", key="sv_entry_start"
            )
        with tc2:
            force_exit = st.text_input(
                "Force exit time", value="14:30", key="sv_force_exit"
            )

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="sv_run"):
        engine = StraddleVwapBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            tp_pct=float(tp_pct),
            sl_pct=float(sl_pct),
            entry_start=entry_start,
            force_exit_time=force_exit,
        )

        progress_bar = st.progress(0.0)
        status_text = st.empty()

        def on_progress(i, total, date_str):
            progress_bar.progress(min((i + 1) / total, 1.0))
            status_text.text(f"Processing {date_str}  ({i + 1} / {total})")

        trades = engine.run(progress_callback=on_progress)

        progress_bar.empty()
        status_text.empty()

        if not trades:
            st.warning("No trades found for the selected parameters and date range.")
            return

        st.session_state["sv_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "sv_results" in st.session_state:
        _show_results(st.session_state["sv_results"])


# ----------------------------------------------------------------
# Results display
# ----------------------------------------------------------------

def _show_results(df: pd.DataFrame):
    st.divider()
    st.subheader("Results")

    total = len(df)
    wins = int((df["pnl_inr"] > 0).sum())
    losses = int((df["pnl_inr"] < 0).sum())
    win_rate = wins / total * 100 if total > 0 else 0
    total_pnl = df["pnl_inr"].sum()
    avg_pnl = df["pnl_inr"].mean()

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Trades", total)
    c2.metric("Wins", wins)
    c3.metric("Losses", losses)
    c4.metric("Win Rate", f"{win_rate:.1f}%")
    c5.metric("Total P&L", f"\u20b9{total_pnl:,.0f}")
    c6.metric("Avg P&L / Trade", f"\u20b9{avg_pnl:,.0f}")

    # Exit reason breakdown
    reasons = df["exit_reason"].value_counts()
    r1, r2, r3 = st.columns(3)
    r1.metric("SL exits", int(reasons.get("SL", 0)))
    r2.metric("TP exits", int(reasons.get("TP", 0)))
    r3.metric("EOD exits", int(reasons.get("EOD", 0)))

    # Avg P&L by exit reason
    st.markdown("**Avg P&L by Exit Reason:**")
    reason_stats = df.groupby("exit_reason")["pnl_inr"].agg(["count", "mean", "sum"])
    reason_stats.columns = ["Count", "Avg P&L", "Total P&L"]
    st.dataframe(
        reason_stats.style.format({"Avg P&L": "\u20b9{:,.0f}", "Total P&L": "\u20b9{:,.0f}"}),
    )

    # Equity curve
    st.divider()
    st.subheader("Equity Curve")
    equity = df["pnl_inr"].cumsum()
    chart_df = pd.DataFrame(
        {"Cumulative P&L (\u20b9)": equity.values},
        index=range(1, len(equity) + 1),
    )
    chart_df.index.name = "Trade #"
    st.line_chart(chart_df)

    # All trades table
    st.divider()
    st.subheader("All Trades")

    filter_reason = st.multiselect(
        "Filter by exit reason",
        options=["SL", "TP", "EOD"],
        key="sv_filter_reason",
    )

    filtered = df.copy()
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="straddle_vwap_sell_backtest.csv",
        mime="text/csv",
        key="sv_download",
    )
```

- [ ] **Step 2: Verify import**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "from ui.straddle_vwap_backtest_runner import render_straddle_vwap_backtest; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

Suggested message: `feat(ui): add Streamlit runner for straddle VWAP sell backtest`

---

### Task 5: Register Tab in app.py

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add import**

Add after the existing imports:

```python
from ui.straddle_vwap_backtest_runner import render_straddle_vwap_backtest
```

- [ ] **Step 2: Add the tab**

Update the `st.tabs()` call to include the new tab. Add `tab_straddle_vwap` variable and the tab label `"📉 Straddle VWAP"`.

- [ ] **Step 3: Add the render block**

```python
with tab_straddle_vwap:
    render_straddle_vwap_backtest()
```

- [ ] **Step 4: Verify syntax**

Run: `cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "import ast; ast.parse(open('app.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

- [ ] **Step 5: Commit**

Suggested message: `feat(app): register Straddle VWAP tab in main app`

---

### Task 6: Smoke Test

**Files:**
- No file changes. Verification only.

- [ ] **Step 1: Run engine on small date range**

```bash
cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy && python -c "
from engine.straddle_vwap_backtest import StraddleVwapBacktestEngine, trades_to_dataframe
engine = StraddleVwapBacktestEngine(start_date='2025-03-01', end_date='2025-03-15')
trades = engine.run()
print(f'Trades: {len(trades)}')
if trades:
    df = trades_to_dataframe(trades)
    print(df[['date','strike','entry_time','entry_price','exit_time','exit_price','exit_reason','pnl_inr']].to_string())
"
```

Expected: Prints trade table without errors.

- [ ] **Step 2: Verify P&L direction**

For TP exits: `pnl_inr` should be positive (we sold high, straddle dropped).
For SL exits: `pnl_inr` should be negative (we sold, straddle rose above SL).

- [ ] **Step 3: Verify VWAP crossover logic**

Check that entry_price (VWAP) is between the previous straddle_close and the current straddle_close (confirming a real crossover happened).

- [ ] **Step 4: Run Streamlit and verify UI**

Run: `streamlit run app.py`
Verify the "Straddle VWAP" tab renders and works.

- [ ] **Step 5: Commit any fixes**

Suggested message: `fix(engine): resolve issues found during straddle VWAP smoke test`
