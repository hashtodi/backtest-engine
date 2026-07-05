"""
BB Reversal PE Buy (PineScript + Spot Exit) — Backtest Runner UI.
"""

import streamlit as st
import pandas as pd

from engine.bb_reversal_pine_exit_backtest import BBReversalPineExitBacktestEngine, trades_to_dataframe


def render_bb_reversal_pine_exit_backtest():
    st.header("BB Reversal PE — PineScript-exit")
    st.caption(
        "PineScript signal + spot-based exit  |  "
        "SL = highestHigh  |  TP = 1:2 RR on spot  |  Exit PE at next bar open"
    )

    with st.expander("Strategy Parameters", expanded=True):
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-20").date(), key="bbrpe_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-12").date(), key="bbrpe_end"
            )

        with col2:
            st.markdown("**Exit (spot-based)**")
            rr_ratio = st.number_input(
                "Risk:Reward ratio", value=2.0, step=0.5, min_value=0.5, key="bbrpe_rr"
            )
            st.caption("SL = highestHigh, TP = entry - RR × risk")

    with st.expander("Bollinger Band Settings", expanded=False):
        bc1, bc2 = st.columns(2)
        with bc1:
            bb_period = st.number_input(
                "BB Period", value=20, step=1, min_value=2, key="bbrpe_bb_period"
            )
        with bc2:
            bb_std = st.number_input(
                "BB Std Dev", value=2.0, step=0.1, min_value=0.1, key="bbrpe_bb_std"
            )

    with st.expander("Time Windows", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            entry_start = st.text_input(
                "Entry start", value="09:18", key="bbrpe_entry_start"
            )
        with tc2:
            entry_end = st.text_input(
                "Entry end", value="15:19", key="bbrpe_entry_end"
            )
        with tc3:
            force_exit = st.text_input(
                "Force exit time", value="15:20", key="bbrpe_force_exit"
            )

    if st.button("Run Backtest", type="primary", key="bbrpe_run"):
        engine = BBReversalPineExitBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            rr_ratio=float(rr_ratio),
            bb_period=int(bb_period),
            bb_std=float(bb_std),
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

        st.session_state["bbrpe_results"] = trades_to_dataframe(trades)
        st.rerun()

    if "bbrpe_results" in st.session_state:
        _show_results(st.session_state["bbrpe_results"])


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

    # Risk stats
    st.markdown("**Risk Stats:**")
    rc1, rc2, rc3 = st.columns(3)
    rc1.metric("Avg Risk (pts)", f"{df['risk_points'].mean():.1f}")
    rc2.metric("Avg HH", f"{df['highest_high'].mean():.0f}")
    rc3.metric("Avg Spot Entry", f"{df['spot_at_entry'].mean():.0f}")

    reasons = df["exit_reason"].value_counts()
    r1, r2, r3 = st.columns(3)
    r1.metric("SL exits", int(reasons.get("SL", 0)))
    r2.metric("TP exits", int(reasons.get("TP", 0)))
    r3.metric("EOD exits", int(reasons.get("EOD", 0)))

    st.markdown("**Avg P&L by Exit Reason:**")
    reason_stats = df.groupby("exit_reason")["pnl_inr"].agg(["count", "mean", "sum"])
    reason_stats.columns = ["Count", "Avg P&L", "Total P&L"]
    st.dataframe(
        reason_stats.style.format({"Avg P&L": "\u20b9{:,.0f}", "Total P&L": "\u20b9{:,.0f}"}),
    )

    st.divider()
    st.subheader("Equity Curve")
    equity = df["pnl_inr"].cumsum()
    chart_df = pd.DataFrame(
        {"Cumulative P&L (\u20b9)": equity.values},
        index=range(1, len(equity) + 1),
    )
    chart_df.index.name = "Trade #"
    st.line_chart(chart_df)

    st.divider()
    st.subheader("Daily P&L")
    daily_pnl = df.groupby("date")["pnl_inr"].sum().reset_index()
    daily_pnl = daily_pnl.set_index("date")
    daily_pnl.index.name = "Date"
    daily_pnl.columns = ["P&L (\u20b9)"]
    st.bar_chart(daily_pnl)

    st.divider()
    st.subheader("All Trades")

    filter_reason = st.multiselect(
        "Filter by exit reason",
        options=["SL", "TP", "EOD"],
        key="bbrpe_filter_reason",
    )

    filtered = df.copy()
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="bb_reversal_pine_exit_backtest.csv",
        mime="text/csv",
        key="bbrpe_download",
    )
