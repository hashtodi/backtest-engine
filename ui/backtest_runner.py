"""
Backtest Runner tab: orchestrates the form, runs backtests, shows results.

Uses strategy_form.py for all form rendering.
Handles: form validation, strategy building, backtest execution,
         result display, strategy saving, and file downloads.
"""

import streamlit as st
from datetime import date, time as dt_time
from pathlib import Path
from typing import Dict

import config
from engine.data_loader import load_data, calculate_indicators
from engine.backtest import BacktestEngine
from engine import reporter
from ui.form_config import INDICATOR_PARAMS, NEEDS_VALUE, NEEDS_OTHER, auto_name, get_available_columns
from ui.strategy_store import save_strategy, get_output_dir, list_saved_strategies, load_saved_strategy
from ui.strategy_form import (
    init_state,
    render_identity,
    render_indicators,
    render_conditions,
    render_entry,
    render_risk,
    render_session,
)


def render_backtest():
    """Render the full Run Backtest tab."""
    init_state()

    # ---- Show last backtest results + downloads (survives st.rerun) ----
    if "bt_last_results" in st.session_state:
        _show_results_and_downloads()
        st.divider()

    # ---- Load saved strategy selector ----
    saved = list_saved_strategies()
    if saved:
        names = ["— New strategy —"] + [s["name"] for s in saved]
        slugs = [""] + [s["slug"] for s in saved]
        choice = st.selectbox("Load saved strategy", names, key="bt_load_select")
        idx = names.index(choice)
        if idx > 0 and st.button("Load", key="bt_load_btn"):
            _load_strategy_into_form(slugs[idx])
            st.rerun()

    # ---- Form sections ----
    render_identity()
    st.divider()
    render_indicators()
    st.divider()
    render_conditions()
    st.divider()
    render_entry()
    st.divider()
    render_risk()
    st.divider()
    render_session()

    st.divider()

    # ---- Action buttons: Save + Run ----
    c_save, c_run = st.columns(2)
    with c_save:
        if st.button("Save Strategy", width="stretch"):
            strategy = _build_strategy()
            slug = save_strategy(strategy)
            st.success(f"Saved as `{slug}`")
    with c_run:
        run_clicked = st.button("Run Backtest", type="primary", width="stretch")

    if not run_clicked:
        return

    # ---- Validation ----
    names = [auto_name(ind) for ind in st.session_state.bt_indicators]
    if len(names) != len(set(names)):
        st.error("Fix duplicate indicators before running.")
        return

    if st.session_state.get("bt_entry_type") == "Staggered":
        total_cap = sum(l["capital_pct"] for l in st.session_state.bt_entry_levels)
        if abs(total_cap - 100.0) > 0.1:
            st.error(f"Fix capital allocation first ({total_cap:.1f}% ≠ 100%)")
            return

    # Build strategy config and persist
    strategy = _build_strategy()
    st.session_state.last_strategy = strategy
    instruments = strategy["instruments"]
    initial_capital = strategy["initial_capital"]

    if not instruments:
        st.error("Select at least one instrument.")
        return

    _run_backtest(strategy, instruments, initial_capital)


# ============================================
# RUN BACKTEST
# ============================================
def _run_backtest(strategy: dict, instruments: list, initial_capital: int):
    """Execute backtest, save all output files, rerun to refresh."""
    start = strategy["backtest_start"]
    end = strategy["backtest_end"]
    results = {}
    out_dir = get_output_dir(strategy)

    with st.spinner("Running backtest..."):
        for inst in instruments:
            data_path = config.DATA_PATH.get(inst)
            if not data_path:
                st.error(f"No data path configured for {inst}")
                continue

            lot_size = config.LOT_SIZE.get(inst, 1)
            progress = st.progress(0, text=f"Loading {inst} data...")

            # 1. Load data
            df = load_data(data_path, start, end)
            progress.progress(25, text=f"Calculating indicators for {inst}...")

            # 2. Calculate indicators
            df = calculate_indicators(df, strategy["indicators"])
            progress.progress(50, text=f"Running backtest for {inst}...")

            # 3. Run backtest (output_dir for detailed logs)
            engine = BacktestEngine(inst, df, strategy, lot_size, output_dir=str(out_dir))
            trades = engine.run()
            progress.progress(75, text=f"Generating report for {inst}...")

            # 4. Generate report + save CSV to output folder
            report = reporter.generate_report(
                trades, inst, lot_size, initial_capital, start, end,
            )
            progress.progress(100, text=f"{inst} done!")

            if report:
                csv_path = str(out_dir / f"results_{inst}.csv")
                reporter.save_csv(report, csv_path)
                # Also save to root for Dashboard/Trade Explorer compatibility
                reporter.save_csv(report, f"backtest_results_{inst}.csv")
                results[inst] = report
            else:
                st.warning(f"No trades generated for {inst}")

    # Save trade log and summary to output folder
    if results:
        reporter.write_trade_log(results, strategy, str(out_dir / "trades.log"))
        reporter.write_summary(results, strategy, str(out_dir / "summary.md"))

        # Store results + output path in session state for display
        st.session_state.bt_last_results = results
        st.session_state.bt_output_dir = str(out_dir)

    st.rerun()


# ============================================
# SHOW RESULTS + DOWNLOAD BUTTONS
# ============================================
def _show_results_and_downloads():
    """Display results metrics and file download buttons."""
    results = st.session_state.bt_last_results
    out_dir = st.session_state.get("bt_output_dir", "")

    # Metrics per instrument
    for inst, report in results.items():
        _display_results(report, inst)

    st.success("Backtest complete! Dashboard and Trade Explorer are updated.")

    # Download buttons for output files
    if out_dir:
        out_path = Path(out_dir)
        st.markdown("**Download output files:**")
        cols = st.columns(4)
        col_idx = 0

        # CSV files
        for csv in sorted(out_path.glob("results_*.csv")):
            with cols[col_idx % 4]:
                data = csv.read_bytes()
                st.download_button(f"CSV: {csv.stem}", data, csv.name, mime="text/csv")
            col_idx += 1

        # Trade log
        trades_log = out_path / "trades.log"
        if trades_log.exists():
            with cols[col_idx % 4]:
                st.download_button("Trade Log", trades_log.read_bytes(),
                                   "trades.log", mime="text/plain")
            col_idx += 1

        # Summary
        summary_md = out_path / "summary.md"
        if summary_md.exists():
            with cols[col_idx % 4]:
                st.download_button("Summary", summary_md.read_bytes(),
                                   "summary.md", mime="text/markdown")
            col_idx += 1

        # Detailed logs
        for dlog in sorted(out_path.glob("detailed_*.log")):
            with cols[col_idx % 4]:
                st.download_button(f"Log: {dlog.stem}", dlog.read_bytes(),
                                   dlog.name, mime="text/plain")
            col_idx += 1

    if st.button("Clear results"):
        del st.session_state["bt_last_results"]
        if "bt_output_dir" in st.session_state:
            del st.session_state["bt_output_dir"]
        st.rerun()


def _display_results(report: dict, instrument: str):
    """Show inline metrics for one instrument."""
    r = report
    st.subheader(f"{instrument} Results")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("P&L", f"₹{r['total_money_pnl']:,.0f}")
    c2.metric("Return", f"{r['return_pct']:.1f}%")
    c3.metric("Win Rate", f"{r['win_rate']:.1f}%")
    c4.metric("Trades", r["total_trades"])
    c5.metric("Avg P&L", f"₹{r['avg_money_pnl']:,.0f}")


# ============================================
# LOAD SAVED STRATEGY INTO FORM
# ============================================

def _clear_dynamic_widget_keys():
    """
    Remove stale widget keys for dynamic form items.

    When loading a saved strategy, old widget keys (from the previous form render)
    linger in session state. Streamlit uses those stale values instead of the new
    index/value params, causing loaded strategies to show default data.
    """
    stale_prefixes = (
        "ind_type_", "ind_ind_",                                # indicator widgets
        "cond_ind_", "cond_cmp_", "cond_val_", "cond_other_",  # condition widgets
        "lvl_pct_", "lvl_cap_",                                 # entry level widgets
        "rm_ind_", "rm_cond_", "rm_lvl_",                       # remove buttons
    )
    for k in list(st.session_state.keys()):
        if any(k.startswith(p) for p in stale_prefixes):
            del st.session_state[k]


def _load_strategy_into_form(slug: str):
    """
    Populate form session state from a saved strategy JSON.

    Sets both the data structures (bt_indicators, bt_conditions, etc.)
    AND the individual widget keys (ind_type_*, cond_ind_*, etc.)
    so Streamlit selectboxes/inputs reflect the loaded values.
    """
    strategy = load_saved_strategy(slug)
    if not strategy:
        return

    # Clear stale widget keys from previous form render
    _clear_dynamic_widget_keys()

    # Identity
    st.session_state.bt_name = strategy.get("name", "")
    st.session_state.bt_desc = strategy.get("description", "")

    # Direction
    st.session_state.bt_direction = strategy.get("direction", "sell")

    # --- Indicators ---
    indicators = []
    for cfg in strategy.get("indicators", []):
        ind = {"id": f"ind_{len(indicators)}", "type": cfg["type"]}
        for k, v in cfg.items():
            if k not in ("type", "name"):
                ind[k] = v
        indicators.append(ind)
    st.session_state.bt_indicators = indicators or [
        {"id": "ind_0", "type": "RSI", "period": 14}
    ]

    # Set widget keys for each indicator (type selectbox + param inputs)
    for ind in st.session_state.bt_indicators:
        uid = ind["id"]
        st.session_state[f"ind_type_{uid}"] = ind["type"]
        for p in INDICATOR_PARAMS[ind["type"]]:
            if p["key"] in ind:
                # Cast to correct type (int/float) so Streamlit widgets don't complain
                st.session_state[f"ind_{uid}_{p['key']}"] = p["type"](ind[p["key"]])

    # Update counter so _next_id("ind") won't collide with loaded IDs
    st.session_state["_counter_ind"] = len(st.session_state.bt_indicators)

    # --- Signal conditions ---
    available = get_available_columns(st.session_state.bt_indicators)

    conditions = []
    for sc in strategy.get("signal_conditions", []):
        cond = {
            "id": f"cond_{len(conditions)}",
            "indicator_col": sc.get("indicator", ""),
            "compare": sc.get("compare", "crosses_above"),
        }
        if "value" in sc:
            cond["value"] = sc["value"]
        if "other" in sc:
            cond["other"] = sc["other"]
        conditions.append(cond)
    st.session_state.bt_conditions = conditions or [
        {"id": "cond_0", "compare": "crosses_above", "value": 70.0}
    ]

    # Set widget keys for each condition (indicator, compare, value/other)
    for cond in st.session_state.bt_conditions:
        uid = cond["id"]
        # Indicator selectbox — only set if the value is valid
        if cond.get("indicator_col") and cond["indicator_col"] in available:
            st.session_state[f"cond_ind_{uid}"] = cond["indicator_col"]
        # Compare selectbox
        st.session_state[f"cond_cmp_{uid}"] = cond["compare"]
        # Threshold value (for crosses_above, crosses_below, above, below)
        if "value" in cond:
            st.session_state[f"cond_val_{uid}"] = float(cond["value"])
        # Other indicator (for crosses_above_indicator, crosses_below_indicator)
        if cond.get("other") and cond["other"] in available:
            st.session_state[f"cond_other_{uid}"] = cond["other"]

    # Update counter so _next_id("cond") won't collide
    st.session_state["_counter_cond"] = len(st.session_state.bt_conditions)

    # Signal logic
    st.session_state.bt_logic = strategy.get("signal_logic", "AND")

    # --- Entry levels ---
    levels = strategy.get("entry_levels", [])
    if len(levels) == 1 and levels[0].get("pct_above_base", 0) == 0:
        st.session_state.bt_entry_type = "Direct"
    else:
        st.session_state.bt_entry_type = "Staggered"
        st.session_state.bt_entry_levels = [
            {"id": f"lvl_{i}", "pct": lvl["pct_above_base"], "capital_pct": lvl["capital_pct"]}
            for i, lvl in enumerate(levels)
        ]
        # Set widget keys for each entry level
        for lvl in st.session_state.bt_entry_levels:
            uid = lvl["id"]
            st.session_state[f"lvl_pct_{uid}"] = float(lvl["pct"])
            st.session_state[f"lvl_cap_{uid}"] = float(lvl["capital_pct"])

        # Update counter so _next_id("lvl") won't collide
        st.session_state["_counter_lvl"] = len(st.session_state.bt_entry_levels)

    # --- Risk ---
    sl = strategy.get("stop_loss_pct", 20)
    tp = strategy.get("target_pct", 10)
    st.session_state.bt_sl_on = sl < 9999
    if sl < 9999:
        st.session_state.bt_sl_pct = float(sl)
    st.session_state.bt_tp_on = tp < 9999
    if tp < 9999:
        st.session_state.bt_tp_pct = float(tp)

    # --- Session ---
    from datetime import datetime
    ts = strategy.get("trading_start", "09:30")
    te = strategy.get("trading_end", "14:30")
    st.session_state.bt_start_time = datetime.strptime(ts, "%H:%M").time()
    st.session_state.bt_end_time = datetime.strptime(te, "%H:%M").time()

    bs = strategy.get("backtest_start", "2025-01-01")
    be = strategy.get("backtest_end", "2025-12-31")
    st.session_state.bt_start_date = datetime.strptime(bs, "%Y-%m-%d").date()
    st.session_state.bt_end_date = datetime.strptime(be, "%Y-%m-%d").date()

    st.session_state.bt_instruments = strategy.get("instruments", list(config.DATA_PATH.keys()))
    st.session_state.bt_capital = strategy.get("initial_capital", 200000)
    mtd = strategy.get("max_trades_per_day")
    st.session_state.bt_max_trades = mtd if mtd else 0


# ============================================
# BUILD STRATEGY DICT FROM FORM STATE
# ============================================
def _build_strategy() -> Dict:
    """Collect all form inputs into a strategy config dict."""
    # -- Indicators --
    ind_configs = []
    for ind in st.session_state.bt_indicators:
        cfg = {"type": ind["type"], "name": auto_name(ind)}
        for p in INDICATOR_PARAMS[ind["type"]]:
            cfg[p["key"]] = ind.get(p["key"], p["default"])
        ind_configs.append(cfg)

    # -- Signal conditions --
    sig_conditions = []
    for cond in st.session_state.bt_conditions:
        sc = {"indicator": cond.get("indicator_col", ""), "compare": cond["compare"]}
        if cond["compare"] in NEEDS_VALUE:
            sc["value"] = cond.get("value", 70.0)
        if cond["compare"] in NEEDS_OTHER:
            sc["other"] = cond.get("other", "")
        sig_conditions.append(sc)

    # -- Entry levels --
    if st.session_state.get("bt_entry_type") == "Direct":
        entry_levels = [{"pct_above_base": 0, "capital_pct": 100.0}]
    else:
        entry_levels = [
            {"pct_above_base": lvl["pct"], "capital_pct": lvl["capital_pct"]}
            for lvl in st.session_state.bt_entry_levels
        ]

    # -- SL/TP: disabled = 9999 (effectively never triggers, only EOD exit) --
    sl = st.session_state.get("bt_sl_pct", 20.0) if st.session_state.get("bt_sl_on") else 9999
    tp = st.session_state.get("bt_tp_pct", 10.0) if st.session_state.get("bt_tp_on") else 9999

    start_t = st.session_state.get("bt_start_time", dt_time(9, 30))
    end_t = st.session_state.get("bt_end_time", dt_time(14, 30))
    start_d = st.session_state.get("bt_start_date", date(2025, 1, 1))
    end_d = st.session_state.get("bt_end_date", date(2025, 12, 31))
    max_trades = st.session_state.get("bt_max_trades", 0)

    # Use user-provided name/description, fall back to a sensible default
    name = st.session_state.get("bt_name", "").strip()
    if not name:
        name = f"Custom {st.session_state.get('bt_direction', 'sell').title()}"
    desc = st.session_state.get("bt_desc", "").strip() or "Custom strategy from UI"

    return {
        "name": name,
        "description": desc,
        "indicators": ind_configs,
        "signal_conditions": sig_conditions,
        "signal_logic": st.session_state.get("bt_logic", "AND"),
        "direction": st.session_state.get("bt_direction", "sell"),
        "entry_levels": entry_levels,
        "stop_loss_pct": sl,
        "target_pct": tp,
        "trading_start": start_t.strftime("%H:%M"),
        "trading_end": end_t.strftime("%H:%M"),
        "max_trades_per_day": max_trades if max_trades > 0 else None,
        "instruments": st.session_state.get("bt_instruments", list(config.DATA_PATH.keys())),
        "backtest_start": start_d.strftime("%Y-%m-%d"),
        "backtest_end": end_d.strftime("%Y-%m-%d"),
        "initial_capital": st.session_state.get("bt_capital", 200000),
    }
