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
    NEEDS_VALUE, NEEDS_OTHER, NEEDS_PRICE_FIELD, PRICE_FIELDS,
    PRICE_SOURCES,
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
        st.session_state.bt_name = ""
    if "bt_desc" not in st.session_state:
        st.session_state.bt_desc = ""

    # Indicators — empty by default; user adds what they need
    if "bt_indicators" not in st.session_state:
        st.session_state.bt_indicators = []

    # Signal conditions — empty by default; user adds after indicators
    if "bt_conditions" not in st.session_state:
        st.session_state.bt_conditions = []
    if "bt_logic" not in st.session_state:
        st.session_state.bt_logic = "AND"

    # Entry — default: buy, direct entry
    if "bt_direction" not in st.session_state:
        st.session_state.bt_direction = "buy"
    if "bt_entry_type" not in st.session_state:
        st.session_state.bt_entry_type = "Direct"
    if "bt_entry_levels" not in st.session_state:
        st.session_state.bt_entry_levels = [
            {"id": "lvl_0", "pct": 5.0, "capital_pct": 33.33},
            {"id": "lvl_1", "pct": 10.0, "capital_pct": 33.33},
            {"id": "lvl_2", "pct": 15.0, "capital_pct": 33.34},
        ]
    # Indicator column used as dynamic entry level (for "Indicator Level" entry type)
    if "bt_entry_indicator" not in st.session_state:
        st.session_state.bt_entry_indicator = ""

    # Risk management — SL 15%, TP 10%
    if "bt_sl_on" not in st.session_state:
        st.session_state.bt_sl_on = True
    if "bt_sl_pct" not in st.session_state:
        st.session_state.bt_sl_pct = 15.0
    if "bt_tp_on" not in st.session_state:
        st.session_state.bt_tp_on = True
    if "bt_tp_pct" not in st.session_state:
        st.session_state.bt_tp_pct = 10.0

    # Session settings — 9:30 to 15:15, both instruments, 0 = unlimited
    if "bt_start_time" not in st.session_state:
        st.session_state.bt_start_time = dt_time(9, 30)
    if "bt_end_time" not in st.session_state:
        st.session_state.bt_end_time = dt_time(15, 15)
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
            c_type, c_source, c_params, c_rm = st.columns([2, 1.5, 3.5, 0.5])

            with c_type:
                # Ensure widget key exists (no index= to avoid Session State conflict)
                wk = f"ind_type_{uid}"
                if wk not in st.session_state:
                    st.session_state[wk] = ind.get("type", "RSI")

                new_type = st.selectbox(
                    "Type", list(INDICATOR_PARAMS.keys()),
                    key=wk,
                )
                # When type changes: clear ALL old param keys, set new defaults
                if new_type != ind.get("type"):
                    for p in INDICATOR_PARAMS[ind["type"]]:
                        wk = f"ind_{uid}_{p['key']}"
                        if wk in st.session_state:
                            del st.session_state[wk]
                    ps = ind.get("price_source", "spot")
                    indicators[i] = {"id": uid, "type": new_type, "price_source": ps}
                    for p in INDICATOR_PARAMS[new_type]:
                        indicators[i][p["key"]] = p["default"]
                        st.session_state[f"ind_{uid}_{p['key']}"] = p["default"]
                    st.rerun()

            with c_source:
                # Price source: spot (underlying) or option (contract close)
                wk = f"ind_ps_{uid}"
                if wk not in st.session_state:
                    st.session_state[wk] = ind.get("price_source", "spot")

                indicators[i]["price_source"] = st.selectbox(
                    "Source", PRICE_SOURCES, key=wk,
                )

            with c_params:
                params = INDICATOR_PARAMS[ind["type"]]
                if params:
                    pcols = st.columns(len(params))
                    for j, p in enumerate(params):
                        with pcols[j]:
                            # Ensure widget key exists (no value= to avoid Session State conflict)
                            wk = f"ind_{uid}_{p['key']}"
                            if wk not in st.session_state:
                                st.session_state[wk] = p["type"](ind.get(p["key"], p["default"]))

                            if p["type"] == int:
                                indicators[i][p["key"]] = st.number_input(
                                    p["label"], p["min"], p["max"],
                                    key=wk,
                                )
                            else:
                                indicators[i][p["key"]] = st.number_input(
                                    p["label"], float(p["min"]), float(p["max"]),
                                    step=0.1, key=wk,
                                )
                else:
                    st.caption("No parameters needed")

            with c_rm:
                if st.button("✕", key=f"rm_ind_{uid}"):
                    for p in INDICATOR_PARAMS[ind["type"]]:
                        k = f"ind_{uid}_{p['key']}"
                        if k in st.session_state:
                            del st.session_state[k]
                    for prefix in (f"ind_type_{uid}", f"ind_ps_{uid}"):
                        if prefix in st.session_state:
                            del st.session_state[prefix]
                    indicators.pop(i)
                    st.rerun()

    if not indicators:
        st.info("No indicators configured. Click **Add Indicator** to get started.")

    if st.button("+ Add Indicator", key="add_ind"):
        indicators.append({
            "id": _next_id("ind"), "type": "RSI", "period": 14, "price_source": "spot",
        })
        st.rerun()

    if indicators:
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
                # Ensure widget key exists with saved/default indicator
                wk = f"cond_ind_{uid}"
                if wk not in st.session_state:
                    default_ind = cond.get("indicator_col", available[0] if available else "")
                    if default_ind in available:
                        st.session_state[wk] = default_ind

                conditions[i]["indicator_col"] = st.selectbox(
                    "Indicator", available, key=wk,
                )
            with c2:
                # Ensure widget key exists (no index= to avoid Session State conflict)
                wk = f"cond_cmp_{uid}"
                if wk not in st.session_state:
                    st.session_state[wk] = cond.get("compare", "crosses_above")

                conditions[i]["compare"] = st.selectbox(
                    "Compare", COMPARE_TYPES, key=wk,
                )
            with c3:
                compare = conditions[i]["compare"]
                if compare in NEEDS_VALUE:
                    # Ensure widget key exists (no value= to avoid Session State conflict)
                    wk = f"cond_val_{uid}"
                    if wk not in st.session_state:
                        st.session_state[wk] = float(cond.get("value", 70.0))

                    conditions[i]["value"] = st.number_input(
                        "Threshold", step=1.0, format="%.2f", key=wk,
                    )
                elif compare in NEEDS_OTHER:
                    # Ensure widget key exists with saved/default other indicator
                    wk = f"cond_other_{uid}"
                    if wk not in st.session_state:
                        default_other = cond.get("other", available[0] if available else "")
                        if default_other in available:
                            st.session_state[wk] = default_other

                    conditions[i]["other"] = st.selectbox(
                        "Other Indicator", available, key=wk,
                    )
                elif compare in NEEDS_PRICE_FIELD:
                    # Price-vs-indicator: let user pick which price field to compare.
                    # e.g. "high" to catch wicks, "close" for standard comparison.
                    wk = f"cond_pf_{uid}"
                    if wk not in st.session_state:
                        st.session_state[wk] = cond.get("price_field", "close")

                    conditions[i]["price_field"] = st.selectbox(
                        "Price Field", PRICE_FIELDS, key=wk,
                    )
                else:
                    st.caption("Compares to close price")
            with c4:
                if st.button("✕", key=f"rm_cond_{uid}"):
                    for prefix in ("cond_ind_", "cond_cmp_", "cond_val_",
                                   "cond_other_", "cond_pf_"):
                        k = f"{prefix}{uid}"
                        if k in st.session_state:
                            del st.session_state[k]
                    conditions.pop(i)
                    st.rerun()

    if not conditions:
        st.info("No signal conditions configured. Click **Add Condition** below.")

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
        st.selectbox("Direction", ["buy", "sell"], key="bt_direction")
    with c2:
        # No default — init_state() sets bt_entry_type
        st.radio("Entry Type", ["Direct", "Staggered", "Indicator Level"],
                 horizontal=True, key="bt_entry_type")

    # "Indicator Level" entry: user picks an indicator whose live value
    # becomes the dynamic limit order price. Signal fires -> wait for
    # price to touch indicator level -> fill at indicator value.
    if st.session_state.bt_entry_type == "Indicator Level":
        available = get_available_columns(st.session_state.bt_indicators)
        if available:
            # Default to saved value if valid, else first available
            default = st.session_state.bt_entry_indicator
            if default not in available:
                default = available[0]
                st.session_state.bt_entry_indicator = default
            st.session_state.bt_entry_indicator = st.selectbox(
                "Entry Indicator (limit level)",
                available,
                index=available.index(default),
                key="_bt_entry_indicator_sel",
            )
        else:
            st.warning("Add indicators first to use Indicator Level entry.")

    elif st.session_state.bt_entry_type == "Staggered":
        levels = st.session_state.bt_entry_levels

        for i, lvl in enumerate(levels):
            uid = lvl["id"]
            c_pct, c_cap, c_rm = st.columns([2, 2, 0.5])
            with c_pct:
                # Ensure widget key exists (no value= to avoid Session State conflict)
                wk = f"lvl_pct_{uid}"
                if wk not in st.session_state:
                    st.session_state[wk] = float(lvl.get("pct", 5.0))

                levels[i]["pct"] = st.number_input(
                    f"Level {i + 1} (% from base)", 0.0, 100.0,
                    step=1.0, key=wk,
                )
            with c_cap:
                # Ensure widget key exists (no value= to avoid Session State conflict)
                wk = f"lvl_cap_{uid}"
                if wk not in st.session_state:
                    st.session_state[wk] = float(lvl.get("capital_pct", 33.33))

                levels[i]["capital_pct"] = st.number_input(
                    f"Level {i + 1} Capital %", 0.0, 100.0,
                    step=1.0, key=wk,
                )
            with c_rm:
                if len(levels) > 1:
                    if st.button("✕", key=f"rm_lvl_{uid}"):
                        # Clean up orphaned widget keys for this level
                        for prefix in ("lvl_pct_", "lvl_cap_"):
                            k = f"{prefix}{uid}"
                            if k in st.session_state:
                                del st.session_state[k]
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
            st.number_input(
                "SL %", min_value=0.1, max_value=100.0,
                step=0.5, format="%.1f", key="bt_sl_pct",
            )
    with c2:
        tp_on = st.toggle("Take Profit", key="bt_tp_on")
        if tp_on:
            st.number_input(
                "TP %", min_value=0.1, max_value=100.0,
                step=0.5, format="%.1f", key="bt_tp_pct",
            )


# ============================================
# SESSION SETTINGS
# ============================================
def render_session():
    """Trading hours, dates, instruments, capital. Defaults from init_state()."""
    st.markdown("##### Session")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.time_input("Start Time", key="bt_start_time")
    with c2:
        st.time_input("End Time", key="bt_end_time")
    with c3:
        st.date_input("Start Date", key="bt_start_date")
    with c4:
        st.date_input("End Date", key="bt_end_date")

    # Validate: start date must be before end date
    start_d = st.session_state.get("bt_start_date")
    end_d = st.session_state.get("bt_end_date")
    if start_d and end_d and start_d >= end_d:
        st.error("Start date must be before end date.")

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
