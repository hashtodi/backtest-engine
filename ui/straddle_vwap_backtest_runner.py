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
