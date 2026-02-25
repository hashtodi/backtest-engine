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
from ui.form_config import INDICATOR_PARAMS, NEEDS_VALUE, NEEDS_OTHER, NEEDS_PRICE_FIELD, auto_name, get_available_columns
from ui.strategy_store import (
    save_strategy, get_output_dir, list_saved_strategies,
    load_saved_strategy, render_strategy_description,
)
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

    # ---- Strategy selector: load saved or start new ----
    saved = list_saved_strategies()
    if saved:
        names = [s["name"] for s in saved]
        slugs = [s["slug"] for s in saved]

        col_sel, col_new = st.columns([3, 1])
        with col_sel:
            idx = st.selectbox(
                "Load saved strategy",
                range(len(names)),
                format_func=lambda i: names[i],
                index=None,
                placeholder="Select a strategy...",
                key="bt_load_select",
            )
        with col_new:
            st.markdown("<br>", unsafe_allow_html=True)
            new_clicked = st.button("New Strategy", key="bt_new_btn")

        # Auto-load when selection changes (track via bt_loaded_slug)
        if idx is not None:
            slug = slugs[idx]
            if st.session_state.get("bt_loaded_slug") != slug:
                _load_strategy_into_form(slug)
                st.session_state.bt_loaded_slug = slug
                st.rerun()
            # Show strategy description for context
            strategy_data = load_saved_strategy(slug)
            if strategy_data:
                render_strategy_description(strategy_data)

        # "New Strategy" resets the form to defaults
        if new_clicked:
            _reset_form_to_defaults()
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

    # ---- Check if form is complete enough to save/run ----
    has_name = bool(st.session_state.get("bt_name", "").strip())
    has_indicators = len(st.session_state.bt_indicators) > 0
    has_conditions = len(st.session_state.bt_conditions) > 0
    start_d = st.session_state.get("bt_start_date")
    end_d = st.session_state.get("bt_end_date")
    dates_valid = start_d and end_d and start_d < end_d
    form_ready = has_name and has_indicators and has_conditions and dates_valid

    if not form_ready:
        missing = []
        if not has_name:
            missing.append("a strategy name")
        if not has_indicators:
            missing.append("at least 1 indicator")
        if not has_conditions:
            missing.append("at least 1 signal condition")
        if not dates_valid:
            missing.append("valid date range (start < end)")
        st.warning(f"Add {', '.join(missing)} to enable Save / Run.")

    # ---- Action buttons: Save + Run ----
    c_save, c_run = st.columns(2)
    with c_save:
        if st.button("Save Strategy", width="stretch", disabled=not form_ready):
            strategy = _build_strategy()
            slug = save_strategy(strategy)
            st.success(f"Saved as `{slug}`")
    with c_run:
        run_clicked = st.button("Run Backtest", type="primary",
                                width="stretch", disabled=not form_ready)

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

    # Build strategy config
    strategy = _build_strategy()
    instruments = strategy["instruments"]
    initial_capital = strategy["initial_capital"]

    if not instruments:
        st.error("Select at least one instrument.")
        return

    if not dates_valid:
        st.error("Start date must be before end date.")
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
        "ind_type_", "ind_ps_", "ind_ind_",                     # indicator widgets
        "cond_ind_", "cond_cmp_", "cond_val_", "cond_other_",  # condition widgets
        "lvl_pct_", "lvl_cap_",                                 # entry level widgets
        "rm_ind_", "rm_cond_", "rm_lvl_",                       # remove buttons
    )
    for k in list(st.session_state.keys()):
        if any(k.startswith(p) for p in stale_prefixes):
            del st.session_state[k]


def _reset_form_to_defaults():
    """
    Reset the form to defaults for creating a new strategy.

    Clears all dynamic widget keys and removes all bt_* keys from
    session state so init_state() will re-populate fresh defaults.
    Preserves backtest results so the user can still view them.
    """
    _clear_dynamic_widget_keys()

    # Keys to keep even during reset (backtest results, output dir)
    keep = {"bt_last_results", "bt_output_dir"}

    # Remove all bt_* state keys so init_state() re-creates them
    bt_keys = [k for k in st.session_state.keys()
               if k.startswith("bt_") and k not in keep]
    for k in bt_keys:
        del st.session_state[k]

    # Clear counter keys so _next_id() starts fresh
    for prefix in ("_counter_ind", "_counter_cond", "_counter_lvl"):
        if prefix in st.session_state:
            del st.session_state[prefix]

    # Clear the loaded slug tracker
    if "bt_loaded_slug" in st.session_state:
        del st.session_state["bt_loaded_slug"]


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
        # Old strategies without price_source default to "option" (backward compat)
        if "price_source" not in ind:
            ind["price_source"] = "option"
        indicators.append(ind)
    st.session_state.bt_indicators = indicators

    # Set widget keys for each indicator (type, source, param inputs)
    for ind in st.session_state.bt_indicators:
        uid = ind["id"]
        st.session_state[f"ind_type_{uid}"] = ind["type"]
        st.session_state[f"ind_ps_{uid}"] = ind.get("price_source", "option")
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
        # Restore price field (close/high/low/open) for price-vs-indicator comparisons.
        if "price_field" in sc:
            cond["price_field"] = sc["price_field"]
        conditions.append(cond)
    st.session_state.bt_conditions = conditions

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
        # Price field (for price_above, price_below, price_crosses_above, etc.)
        if "price_field" in cond:
            st.session_state[f"cond_pf_{uid}"] = cond["price_field"]

    # Update counter so _next_id("cond") won't collide
    st.session_state["_counter_cond"] = len(st.session_state.bt_conditions)

    # Signal logic
    st.session_state.bt_logic = strategy.get("signal_logic", "AND")

    # --- Entry ---
    entry = strategy.get("entry", {})
    entry_type = entry.get("type", "direct")

    if entry_type == "indicator_level":
        st.session_state.bt_entry_type = "Indicator Level"
        st.session_state.bt_entry_indicator = entry.get("indicator", "")
    elif entry_type == "staggered":
        st.session_state.bt_entry_type = "Staggered"
        levels = entry.get("levels", [])
        st.session_state.bt_entry_levels = [
            {"id": f"lvl_{i}", "pct": lvl["pct_from_base"], "capital_pct": lvl["capital_pct"]}
            for i, lvl in enumerate(levels)
        ]
        # Set widget keys for each entry level
        for lvl in st.session_state.bt_entry_levels:
            uid = lvl["id"]
            st.session_state[f"lvl_pct_{uid}"] = float(lvl["pct"])
            st.session_state[f"lvl_cap_{uid}"] = float(lvl["capital_pct"])
        # Update counter so _next_id("lvl") won't collide
        st.session_state["_counter_lvl"] = len(st.session_state.bt_entry_levels)
    else:
        st.session_state.bt_entry_type = "Direct"

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
        cfg = {
            "type": ind["type"],
            "name": auto_name(ind),
            "price_source": ind.get("price_source", "spot"),
        }
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
        # Save selected price field (close/high/low/open) for price-vs-indicator comparisons.
        if cond["compare"] in NEEDS_PRICE_FIELD:
            sc["price_field"] = cond.get("price_field", "close")
        sig_conditions.append(sc)

    # -- Entry config (modular dict) --
    entry_type = st.session_state.get("bt_entry_type", "Direct")
    if entry_type == "Indicator Level":
        entry_cfg = {
            "type": "indicator_level",
            "indicator": st.session_state.get("bt_entry_indicator", ""),
        }
    elif entry_type == "Staggered":
        entry_cfg = {
            "type": "staggered",
            "levels": [
                {"pct_from_base": lvl["pct"], "capital_pct": lvl["capital_pct"]}
                for lvl in st.session_state.bt_entry_levels
            ],
        }
    else:
        entry_cfg = {"type": "direct"}

    # -- SL/TP: disabled = 9999 (effectively never triggers, only EOD exit) --
    sl = st.session_state.get("bt_sl_pct", 15.0) if st.session_state.get("bt_sl_on") else 9999
    tp = st.session_state.get("bt_tp_pct", 10.0) if st.session_state.get("bt_tp_on") else 9999

    start_t = st.session_state.get("bt_start_time", dt_time(9, 30))
    end_t = st.session_state.get("bt_end_time", dt_time(15, 15))
    start_d = st.session_state.get("bt_start_date", date(2025, 1, 1))
    end_d = st.session_state.get("bt_end_date", date(2025, 12, 31))
    max_trades = st.session_state.get("bt_max_trades", 0)

    # Use user-provided name/description, fall back to a sensible default
    name = st.session_state.get("bt_name", "").strip()
    if not name:
        name = f"Custom {st.session_state.get('bt_direction', 'buy').title()}"
    desc = st.session_state.get("bt_desc", "").strip() or "Custom strategy from UI"

    strategy_dict = {
        "name": name,
        "description": desc,
        "indicators": ind_configs,
        "signal_conditions": sig_conditions,
        "signal_logic": st.session_state.get("bt_logic", "AND"),
        "direction": st.session_state.get("bt_direction", "buy"),
        "entry": entry_cfg,
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

    return strategy_dict
