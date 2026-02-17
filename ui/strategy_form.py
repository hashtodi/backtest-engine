"""
Strategy form rendering for the Run Backtest tab.

Each function renders one section of the configuration form.
All form state is stored in st.session_state with "bt_" prefix.

IMPORTANT: Widget defaults are set ONLY in init_state(), never in the
widget call itself. This avoids the "default value vs Session State API"
conflict when loading saved strategies.

Uses unique IDs per item (not array indices) for widget keys.
This prevents stale widget values when items are added/removed.
"""

import streamlit as st
from datetime import date, time as dt_time

import config
from ui.form_config import (
    INDICATOR_PARAMS, COMPARE_TYPES,
    NEEDS_VALUE, NEEDS_OTHER,
    auto_name, get_available_columns,
)


# ============================================
# UNIQUE ID GENERATOR
# ============================================
def _next_id(prefix: str) -> str:
    """Generate a unique ID for a dynamic form item."""
    key = f"_counter_{prefix}"
    if key not in st.session_state:
        st.session_state[key] = 0
    st.session_state[key] += 1
    return f"{prefix}_{st.session_state[key]}"


# ============================================
# SESSION STATE INIT
# ============================================
def init_state():
    """
    Initialize ALL form defaults in session state.

    Only sets a key if it doesn't already exist,
    so loaded/saved values are never overwritten.
    """
    # Identity
    if "bt_name" not in st.session_state:
        st.session_state.bt_name = "My Strategy"
    if "bt_desc" not in st.session_state:
        st.session_state.bt_desc = ""

    # Indicators
    if "bt_indicators" not in st.session_state:
        st.session_state.bt_indicators = [
            {"id": "ind_0", "type": "RSI", "period": 14},
        ]

    # Signal conditions
    if "bt_conditions" not in st.session_state:
        st.session_state.bt_conditions = [
            {"id": "cond_0", "compare": "crosses_above", "value": 70.0},
        ]
    if "bt_logic" not in st.session_state:
        st.session_state.bt_logic = "AND"

    # Entry
    if "bt_direction" not in st.session_state:
        st.session_state.bt_direction = "sell"
    if "bt_entry_type" not in st.session_state:
        st.session_state.bt_entry_type = "Direct"
    if "bt_entry_levels" not in st.session_state:
        st.session_state.bt_entry_levels = [
            {"id": "lvl_0", "pct": 5.0, "capital_pct": 33.33},
            {"id": "lvl_1", "pct": 10.0, "capital_pct": 33.33},
            {"id": "lvl_2", "pct": 15.0, "capital_pct": 33.34},
        ]

    # Risk management
    if "bt_sl_on" not in st.session_state:
        st.session_state.bt_sl_on = True
    if "bt_sl_pct" not in st.session_state:
        st.session_state.bt_sl_pct = 20.0
    if "bt_tp_on" not in st.session_state:
        st.session_state.bt_tp_on = True
    if "bt_tp_pct" not in st.session_state:
        st.session_state.bt_tp_pct = 10.0

    # Session settings
    if "bt_start_time" not in st.session_state:
        st.session_state.bt_start_time = dt_time(9, 30)
    if "bt_end_time" not in st.session_state:
        st.session_state.bt_end_time = dt_time(14, 30)
    if "bt_start_date" not in st.session_state:
        st.session_state.bt_start_date = date(2025, 1, 1)
    if "bt_end_date" not in st.session_state:
        st.session_state.bt_end_date = date(2025, 12, 31)
    if "bt_instruments" not in st.session_state:
        st.session_state.bt_instruments = list(config.DATA_PATH.keys())
    if "bt_capital" not in st.session_state:
        st.session_state.bt_capital = 200000
    if "bt_max_trades" not in st.session_state:
        st.session_state.bt_max_trades = 0


# ============================================
# STRATEGY IDENTITY (name & description)
# ============================================
def render_identity():
    """Strategy name and description inputs."""
    st.markdown("##### Strategy")
    c1, c2 = st.columns([1, 2])
    with c1:
        st.text_input("Name", key="bt_name",
                       help="A short name for this strategy")
    with c2:
        st.text_input("Description", key="bt_desc",
                       help="Brief description of the strategy logic")


# ============================================
# INDICATORS
# ============================================
def render_indicators():
    """Dynamic indicator list with type-specific params and duplicate detection."""
    st.markdown("##### Indicators")
    indicators = st.session_state.bt_indicators

    for i, ind in enumerate(indicators):
        uid = ind["id"]
        with st.container(border=True):
            c_type, c_params, c_rm = st.columns([2, 5, 0.5])

            with c_type:
                new_type = st.selectbox(
                    "Type", list(INDICATOR_PARAMS.keys()),
                    index=list(INDICATOR_PARAMS.keys()).index(ind.get("type", "RSI")),
                    key=f"ind_type_{uid}",
                )
                # When type changes: clear ALL old param keys, set new defaults
                if new_type != ind.get("type"):
                    for p in INDICATOR_PARAMS[ind["type"]]:
                        wk = f"ind_{uid}_{p['key']}"
                        if wk in st.session_state:
                            del st.session_state[wk]
                    indicators[i] = {"id": uid, "type": new_type}
                    for p in INDICATOR_PARAMS[new_type]:
                        indicators[i][p["key"]] = p["default"]
                        st.session_state[f"ind_{uid}_{p['key']}"] = p["default"]
                    st.rerun()

            with c_params:
                params = INDICATOR_PARAMS[ind["type"]]
                if params:
                    pcols = st.columns(len(params))
                    for j, p in enumerate(params):
                        with pcols[j]:
                            if p["type"] == int:
                                indicators[i][p["key"]] = st.number_input(
                                    p["label"], p["min"], p["max"],
                                    value=int(ind.get(p["key"], p["default"])),
                                    key=f"ind_{uid}_{p['key']}",
                                )
                            else:
                                indicators[i][p["key"]] = st.number_input(
                                    p["label"], float(p["min"]), float(p["max"]),
                                    value=float(ind.get(p["key"], p["default"])),
                                    step=0.1, key=f"ind_{uid}_{p['key']}",
                                )
                else:
                    st.caption("No parameters needed")

            with c_rm:
                if len(indicators) > 1:
                    if st.button("✕", key=f"rm_ind_{uid}"):
                        indicators.pop(i)
                        st.rerun()

    if st.button("+ Add Indicator", key="add_ind"):
        indicators.append({"id": _next_id("ind"), "type": "RSI", "period": 14})
        st.rerun()

    # Duplicate detection
    names = [auto_name(ind) for ind in indicators]
    if len(names) != len(set(names)):
        st.error("Duplicate indicator detected. Change the type or parameters.")

    cols = get_available_columns(indicators)
    st.caption(f"Available columns: `{'`, `'.join(cols)}`")


# ============================================
# SIGNAL CONDITIONS
# ============================================
def render_conditions():
    """Dynamic signal condition list with comparison type selection."""
    st.markdown("##### Signal Conditions")
    conditions = st.session_state.bt_conditions
    available = get_available_columns(st.session_state.bt_indicators)

    if not available:
        st.warning("Add at least one indicator first.")
        return

    # No default value here — init_state() sets bt_logic
    st.radio("Logic", ["AND", "OR"], horizontal=True, key="bt_logic",
             help="AND = all must fire. OR = any fires.")

    for i, cond in enumerate(conditions):
        uid = cond["id"]

        # Clear stale selectbox keys if stored value no longer available
        for sk in (f"cond_ind_{uid}", f"cond_other_{uid}"):
            if sk in st.session_state and st.session_state[sk] not in available:
                del st.session_state[sk]

        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 2, 2, 0.5])

            with c1:
                conditions[i]["indicator_col"] = st.selectbox(
                    "Indicator", available, key=f"cond_ind_{uid}",
                )
            with c2:
                cmp_idx = COMPARE_TYPES.index(cond.get("compare", "crosses_above"))
                conditions[i]["compare"] = st.selectbox(
                    "Compare", COMPARE_TYPES, index=cmp_idx, key=f"cond_cmp_{uid}",
                )
            with c3:
                compare = conditions[i]["compare"]
                if compare in NEEDS_VALUE:
                    conditions[i]["value"] = st.number_input(
                        "Threshold", value=float(cond.get("value", 70.0)),
                        step=1.0, format="%.2f", key=f"cond_val_{uid}",
                    )
                elif compare in NEEDS_OTHER:
                    conditions[i]["other"] = st.selectbox(
                        "Other Indicator", available, key=f"cond_other_{uid}",
                    )
                else:
                    st.caption("Compares to close price")
            with c4:
                if len(conditions) > 1:
                    if st.button("✕", key=f"rm_cond_{uid}"):
                        conditions.pop(i)
                        st.rerun()

    if st.button("+ Add Condition", key="add_cond"):
        conditions.append({"id": _next_id("cond"), "compare": "crosses_above", "value": 70.0})
        st.rerun()


# ============================================
# ENTRY CONFIG (direct vs staggered)
# ============================================
def render_entry():
    """Direction + direct/staggered entry with dynamic levels."""
    st.markdown("##### Trade Entry")

    c1, c2 = st.columns(2)
    with c1:
        # No default — init_state() sets bt_direction
        st.selectbox("Direction", ["sell", "buy"], key="bt_direction")
    with c2:
        # No default — init_state() sets bt_entry_type
        st.radio("Entry Type", ["Direct", "Staggered"],
                 horizontal=True, key="bt_entry_type")

    if st.session_state.bt_entry_type == "Staggered":
        levels = st.session_state.bt_entry_levels

        for i, lvl in enumerate(levels):
            uid = lvl["id"]
            c_pct, c_cap, c_rm = st.columns([2, 2, 0.5])
            with c_pct:
                levels[i]["pct"] = st.number_input(
                    f"Level {i + 1} (% from base)", 0.0, 100.0,
                    value=float(lvl.get("pct", 5.0)), step=1.0, key=f"lvl_pct_{uid}",
                )
            with c_cap:
                levels[i]["capital_pct"] = st.number_input(
                    f"Level {i + 1} Capital %", 0.0, 100.0,
                    value=float(lvl.get("capital_pct", 33.33)),
                    step=1.0, key=f"lvl_cap_{uid}",
                )
            with c_rm:
                if len(levels) > 1:
                    if st.button("✕", key=f"rm_lvl_{uid}"):
                        levels.pop(i)
                        st.rerun()

        if st.button("+ Add Level", key="add_lvl"):
            levels.append({"id": _next_id("lvl"), "pct": 0.0, "capital_pct": 0.0})
            st.rerun()

        total = sum(l["capital_pct"] for l in levels)
        if abs(total - 100.0) > 0.1:
            st.error(f"Capital must sum to 100%. Currently: {total:.1f}%")
        else:
            st.success(f"Capital allocation: {total:.1f}% ✓")


# ============================================
# RISK MANAGEMENT
# ============================================
def render_risk():
    """SL/TP with on/off toggles. Defaults come from init_state()."""
    st.markdown("##### Risk Management")

    c1, c2 = st.columns(2)
    with c1:
        # No value= param — default set in init_state()
        sl_on = st.toggle("Stop Loss", key="bt_sl_on")
        if sl_on:
            st.number_input("SL %", 0.1, 100.0, step=1.0, key="bt_sl_pct")
    with c2:
        tp_on = st.toggle("Take Profit", key="bt_tp_on")
        if tp_on:
            st.number_input("TP %", 0.1, 100.0, step=1.0, key="bt_tp_pct")


# ============================================
# SESSION SETTINGS
# ============================================
def render_session():
    """Trading hours, dates, instruments, capital. Defaults from init_state()."""
    st.markdown("##### Session")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        # No default value — init_state() sets bt_start_time
        st.time_input("Start Time", key="bt_start_time")
    with c2:
        st.time_input("End Time", key="bt_end_time")
    with c3:
        st.date_input("Start Date", key="bt_start_date")
    with c4:
        st.date_input("End Date", key="bt_end_date")

    c5, c6, c7 = st.columns(3)
    with c5:
        st.multiselect("Instruments", list(config.DATA_PATH.keys()),
                        key="bt_instruments")
    with c6:
        st.number_input("Initial Capital (₹)", 10000, 100000000,
                         step=50000, key="bt_capital")
    with c7:
        st.number_input("Max Trades/Day (0 = unlimited)", 0, 100,
                         key="bt_max_trades")
