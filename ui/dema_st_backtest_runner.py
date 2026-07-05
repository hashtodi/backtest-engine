"""
DEMA-SuperTrend EMA Pullback Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.dema_st_backtest import DemaStBacktestEngine, trades_to_dataframe


def render_dema_st_backtest():
    st.header("DEMA-SuperTrend EMA Pullback")
    st.caption(
        "DEMA(200) + SuperTrend bias  →  EMA(12) pullback entry  →  ATM weekly option  |  "
        "SL 30%  |  TP: expiry 30% / expiry-1 20% / other 10%"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-20").date(), key="ds_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-12").date(), key="ds_end"
            )

        with col2:
            st.markdown("**Indicators (5-min spot)**")
            dema_period = st.number_input(
                "DEMA period", value=200, step=10, min_value=10, key="ds_dema"
            )
            st_period = st.number_input(
                "SuperTrend ATR period", value=12, step=1, min_value=1, key="ds_st_period"
            )
            st_factor = st.number_input(
                "SuperTrend factor", value=3, step=1, min_value=1, key="ds_st_factor"
            )
            ema_period = st.number_input(
                "EMA period (entry touch)", value=12, step=1, min_value=1, key="ds_ema"
            )

        with col3:
            st.markdown("**Exit**")
            sl_pct = st.number_input(
                "SL (%)", value=30.0, step=1.0, min_value=1.0, key="ds_sl"
            )
            tp_expiry = st.number_input(
                "TP % (expiry day)", value=30.0, step=1.0, min_value=1.0, key="ds_tp_exp"
            )
            tp_expiry_m1 = st.number_input(
                "TP % (expiry-1 day)", value=20.0, step=1.0, min_value=1.0, key="ds_tp_exp1"
            )
            tp_other = st.number_input(
                "TP % (other days)", value=10.0, step=1.0, min_value=1.0, key="ds_tp_other"
            )

    with st.expander("Time Windows", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            entry_start = st.text_input("Entry window start", value="09:45", key="ds_entry_start")
        with tc2:
            entry_end = st.text_input("Entry window end", value="14:45", key="ds_entry_end")
        with tc3:
            force_exit = st.text_input("Force exit time", value="15:00", key="ds_force_exit")

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="ds_run"):
        engine = DemaStBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            dema_period=int(dema_period),
            supertrend_period=int(st_period),
            supertrend_factor=int(st_factor),
            ema_period=int(ema_period),
            sl_pct=float(sl_pct),
            tp_expiry=float(tp_expiry),
            tp_expiry_minus1=float(tp_expiry_m1),
            tp_other=float(tp_other),
            entry_start=entry_start,
            entry_end=entry_end,
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

        st.session_state["ds_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "ds_results" in st.session_state:
        _show_results(st.session_state["ds_results"])


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

    # Row 1: Key metrics
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Trades", total)
    c2.metric("Wins", wins)
    c3.metric("Losses", losses)
    c4.metric("Win Rate", f"{win_rate:.1f}%")
    c5.metric("Total P&L", f"₹{total_pnl:,.0f}")
    c6.metric("Avg P&L / Trade", f"₹{avg_pnl:,.0f}")

    # Row 2: Exit reason breakdown
    reasons = df["exit_reason"].value_counts()
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("ST Flip exits", int(reasons.get("ST_FLIP", 0)))
    r2.metric("SL exits", int(reasons.get("SL", 0)))
    r3.metric("TP exits", int(reasons.get("TP", 0)))
    r4.metric("EOD exits", int(reasons.get("EOD", 0)))

    # Row 3: CE vs PE breakdown
    ce_trades = df[df["option_type"] == "CE"]
    pe_trades = df[df["option_type"] == "PE"]
    d1, d2 = st.columns(2)
    with d1:
        ce_wr = (ce_trades["pnl_inr"] > 0).mean() * 100 if len(ce_trades) > 0 else 0
        st.markdown(
            f"**CE trades:** {len(ce_trades)}  |  "
            f"Win rate: {ce_wr:.1f}%  |  "
            f"P&L: ₹{ce_trades['pnl_inr'].sum():,.0f}"
        )
    with d2:
        pe_wr = (pe_trades["pnl_inr"] > 0).mean() * 100 if len(pe_trades) > 0 else 0
        st.markdown(
            f"**PE trades:** {len(pe_trades)}  |  "
            f"Win rate: {pe_wr:.1f}%  |  "
            f"P&L: ₹{pe_trades['pnl_inr'].sum():,.0f}"
        )

    # Row 4: Day-type breakdown
    st.divider()
    st.subheader("By Expiry Day Type")
    day_types = ["expiry", "expiry-1", "other"]
    dt_cols = st.columns(3)
    for col, dtype in zip(dt_cols, day_types):
        subset = df[df["expiry_day_type"] == dtype]
        with col:
            count = len(subset)
            wr = (subset["pnl_inr"] > 0).mean() * 100 if count > 0 else 0
            pnl = subset["pnl_inr"].sum()
            avg = subset["pnl_inr"].mean() if count > 0 else 0
            st.markdown(f"**{dtype}** (TP={subset['tp_pct'].iloc[0]:.0f}%)" if count > 0 else f"**{dtype}**")
            st.metric("Trades", count)
            st.metric("Win Rate", f"{wr:.1f}%")
            st.metric("Total P&L", f"₹{pnl:,.0f}")
            st.metric("Avg P&L", f"₹{avg:,.0f}")

    # Equity curve
    st.divider()
    st.subheader("Equity Curve")
    equity = df["pnl_inr"].cumsum()
    chart_df = pd.DataFrame({"Cumulative P&L (₹)": equity.values}, index=range(1, len(equity) + 1))
    chart_df.index.name = "Trade #"
    st.line_chart(chart_df)

    # All trades table
    st.divider()
    st.subheader("All Trades")

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        filter_type = st.multiselect(
            "Filter by option type", options=["CE", "PE"], key="ds_filter_type"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason", options=["ST_FLIP", "SL", "TP", "EOD"], key="ds_filter_reason"
        )
    with fc3:
        filter_day = st.multiselect(
            "Filter by day type", options=day_types, key="ds_filter_day"
        )

    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]
    if filter_day:
        filtered = filtered[filtered["expiry_day_type"].isin(filter_day)]

    st.dataframe(filtered, width="stretch", hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="dema_st_pullback_backtest.csv",
        mime="text/csv",
        key="ds_download",
    )
