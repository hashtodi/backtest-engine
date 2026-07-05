"""
BB Reversal PE Buy Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.bb_reversal_backtest import BBReversalBacktestEngine, trades_to_dataframe


def render_bb_reversal_backtest():
    st.header("BB Reversal PE Buy")
    st.caption(
        "Buy ATM PE on BB upper-band reversal signal  |  "
        "TP: +15 pts  |  SL: -15 pts  |  09:18 - 15:19"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-20").date(), key="bbr_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-12").date(), key="bbr_end"
            )

        with col2:
            st.markdown("**Exit (points on PE)**")
            tp_pts = st.number_input(
                "TP pts", value=15.0, step=1.0, min_value=0.1, key="bbr_tp"
            )
            sl_pts = st.number_input(
                "SL pts", value=15.0, step=1.0, min_value=0.1, key="bbr_sl"
            )

    with st.expander("Bollinger Band Settings", expanded=False):
        bc1, bc2 = st.columns(2)
        with bc1:
            bb_period = st.number_input(
                "BB Period", value=20, step=1, min_value=2, key="bbr_bb_period"
            )
        with bc2:
            bb_std = st.number_input(
                "BB Std Dev", value=2.0, step=0.1, min_value=0.1, key="bbr_bb_std"
            )

    with st.expander("Time Windows", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            entry_start = st.text_input(
                "Entry start", value="09:18", key="bbr_entry_start"
            )
        with tc2:
            entry_end = st.text_input(
                "Entry end", value="15:19", key="bbr_entry_end"
            )
        with tc3:
            force_exit = st.text_input(
                "Force exit time", value="15:20", key="bbr_force_exit"
            )

    with st.expander("Signal Behavior", expanded=False):
        st.caption("Toggle between our logic and PineScript logic")
        trigger_on_low = st.toggle(
            "Trigger on low breach (off = close breach like PineScript)",
            value=True, key="bbr_trigger_low"
        )
        reset_green_only = st.toggle(
            "Only green candle above BB resets setup (off = any candle like PineScript)",
            value=True, key="bbr_reset_green"
        )
        watching_after_exit = st.toggle(
            "Jump to WATCHING after exit if above BB (off = always IDLE like PineScript)",
            value=True, key="bbr_watching_exit"
        )

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="bbr_run"):
        engine = BBReversalBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            tp_points=float(tp_pts),
            sl_points=float(sl_pts),
            bb_period=int(bb_period),
            bb_std=float(bb_std),
            entry_start=entry_start,
            entry_end=entry_end,
            force_exit_time=force_exit,
            trigger_on_low=trigger_on_low,
            reset_green_only=reset_green_only,
            watching_after_exit=watching_after_exit,
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

        st.session_state["bbr_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "bbr_results" in st.session_state:
        _show_results(st.session_state["bbr_results"])


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

    # Daily P&L bar chart
    st.divider()
    st.subheader("Daily P&L")
    daily_pnl = df.groupby("date")["pnl_inr"].sum().reset_index()
    daily_pnl = daily_pnl.set_index("date")
    daily_pnl.index.name = "Date"
    daily_pnl.columns = ["P&L (\u20b9)"]
    st.bar_chart(daily_pnl)

    # All trades table
    st.divider()
    st.subheader("All Trades")

    filter_reason = st.multiselect(
        "Filter by exit reason",
        options=["SL", "TP", "EOD"],
        key="bbr_filter_reason",
    )

    filtered = df.copy()
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="bb_reversal_pe_backtest.csv",
        mime="text/csv",
        key="bbr_download",
    )
