"""
Boom ST Entry Strategy — Backtest Runner UI.
"""

import streamlit as st
import pandas as pd

from engine.boom_st_backtest import BoomStBacktestEngine, trades_to_dataframe


def render_boom_st_backtest():
    st.header("Boom ST Entry")
    st.caption(
        "SMA + SuperTrend bias  →  ST pullback entry  →  Fixed SL/TP %  |  "
        "CE & PE independent  |  T-1 lag fix"
    )

    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input("From", value=pd.Timestamp("2025-01-01").date(), key="bst_start")
            end_date = st.date_input("To", value=pd.Timestamp("2026-04-02").date(), key="bst_end")

        with col2:
            st.markdown("**Indicators (1-min option close)**")
            sma_period = st.number_input("SMA period", value=13, step=1, min_value=2, key="bst_sma")
            st.markdown("*SuperTrend — Signal*")
            st_sig_f = st.number_input("Factor", value=4, step=1, min_value=1, key="bst_sig_f")
            st_sig_a = st.number_input("ATR period", value=11, step=1, min_value=1, key="bst_sig_a")
            st.markdown("*SuperTrend — Entry*")
            st_ent_f = st.number_input("Factor", value=3, step=1, min_value=1, key="bst_ent_f")
            st_ent_a = st.number_input("ATR period", value=10, step=1, min_value=1, key="bst_ent_a")

        with col3:
            st.markdown("**Exit**")
            sl_pct = st.number_input("SL (%)", value=5.0, step=0.5, min_value=0.1, key="bst_sl")
            tp_pct = st.number_input("TP (%)", value=7.5, step=0.5, min_value=0.1, key="bst_tp")

    with st.expander("Time Windows", expanded=False):
        tc1, tc2 = st.columns(2)
        with tc1:
            trading_start = st.text_input("Entry start", value="09:30", key="bst_t_start")
        with tc2:
            trading_end = st.text_input("Entry end / EOD", value="14:45", key="bst_t_end")

    if st.button("Run Backtest", type="primary", key="bst_run"):
        engine = BoomStBacktestEngine(
            start_date=str(start_date), end_date=str(end_date),
            sma_period=int(sma_period),
            st_signal_factor=int(st_sig_f), st_signal_atr=int(st_sig_a),
            st_entry_factor=int(st_ent_f), st_entry_atr=int(st_ent_a),
            sl_pct=float(sl_pct), tp_pct=float(tp_pct),
            trading_start=trading_start, trading_end=trading_end,
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
            st.warning("No trades found.")
            return

        st.session_state["bst_results"] = trades_to_dataframe(trades)
        st.rerun()

    if "bst_results" in st.session_state:
        _show_results(st.session_state["bst_results"])


def _show_results(df: pd.DataFrame):
    st.divider()
    st.subheader("Results")

    total = len(df)
    wins = int((df["pnl_inr"] > 0).sum())
    losses = int((df["pnl_inr"] <= 0).sum())
    win_rate = wins / total * 100 if total > 0 else 0
    total_pnl = df["pnl_inr"].sum()
    avg_pnl = df["pnl_inr"].mean()

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Trades", total)
    c2.metric("Wins", wins)
    c3.metric("Losses", losses)
    c4.metric("Win Rate", f"{win_rate:.1f}%")
    c5.metric("Total P&L", f"₹{total_pnl:,.0f}")
    c6.metric("Avg P&L / Trade", f"₹{avg_pnl:,.0f}")

    reasons = df["exit_reason"].value_counts()
    r1, r2, r3 = st.columns(3)
    r1.metric("SL exits", int(reasons.get("SL", 0)))
    r2.metric("TP exits", int(reasons.get("TP", 0)))
    r3.metric("EOD exits", int(reasons.get("EOD", 0)))

    ce = df[df["option_type"] == "CE"]
    pe = df[df["option_type"] == "PE"]
    d1, d2 = st.columns(2)
    with d1:
        ce_wr = (ce["pnl_inr"] > 0).mean() * 100 if len(ce) > 0 else 0
        st.markdown(f"**CE:** {len(ce)} trades | WR: {ce_wr:.1f}% | P&L: ₹{ce['pnl_inr'].sum():,.0f}")
    with d2:
        pe_wr = (pe["pnl_inr"] > 0).mean() * 100 if len(pe) > 0 else 0
        st.markdown(f"**PE:** {len(pe)} trades | WR: {pe_wr:.1f}% | P&L: ₹{pe['pnl_inr'].sum():,.0f}")

    same = df[df["signal_time"] == df["entry_time"]]
    if len(same) == 0:
        st.success("No lookahead: 0 same-candle signal+entry trades")

    st.divider()
    st.subheader("Equity Curve")
    equity = df["pnl_inr"].cumsum()
    chart_df = pd.DataFrame({"Cumulative P&L (₹)": equity.values}, index=range(1, len(equity) + 1))
    st.line_chart(chart_df)

    st.divider()
    st.subheader("All Trades")
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        filter_type = st.multiselect("Option type", ["CE", "PE"], key="bst_f_type")
    with fc2:
        filter_reason = st.multiselect("Exit reason", ["SL", "TP", "EOD"], key="bst_f_reason")
    with fc3:
        filter_date = st.text_input("Date (YYYY-MM-DD)", value="", key="bst_f_date")

    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]
    if filter_date:
        filtered = filtered[filtered["date"] == filter_date]

    st.dataframe(filtered, width="stretch", hide_index=True)
    st.download_button(
        f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="boom_st_entry_backtest.csv",
        mime="text/csv", key="bst_dl",
    )
