"""
IV Symmetry Naked Short Straddle — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.iv_symmetry_straddle_backtest import (
    IVSymmetryStraddleEngine,
    trades_to_dataframe,
)


def render_iv_symmetry_backtest():
    st.header("IV Symmetry Short Straddle")
    st.caption(
        "Sell ATM straddle when the IV smile is symmetric (CE & PE sym ≥ 80%)  |  "
        "SL: 8%  |  TP: 30%  |  09:45 - 15:10  |  fills at next bar open (no lookahead)"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-01").date(), key="ivs_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-05-22").date(), key="ivs_end"
            )

        with col2:
            st.markdown("**Exit**")
            sl_pct = st.number_input(
                "SL (% of entry premium)", value=8.0, step=0.5, min_value=0.5,
                key="ivs_sl",
            )
            tp_pct = st.number_input(
                "TP (% of entry premium)", value=30.0, step=1.0, min_value=1.0,
                key="ivs_tp",
            )

        with col3:
            st.markdown("**Signal**")
            sym_min = st.number_input(
                "Min symmetry (0-1)", value=0.80, step=0.05,
                min_value=0.10, max_value=1.0, key="ivs_sym_min",
            )
            min_pairs = st.number_input(
                "Min valid pairs per side", value=2, step=1, min_value=1,
                max_value=10, key="ivs_min_pairs",
            )

    with st.expander("Time Windows", expanded=False):
        tc1, tc2 = st.columns(2)
        with tc1:
            entry_start = st.text_input(
                "Entry window start", value="09:45", key="ivs_entry_start"
            )
        with tc2:
            force_exit = st.text_input(
                "Force exit time", value="15:10", key="ivs_force_exit"
            )

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="ivs_run"):
        engine = IVSymmetryStraddleEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            sl_pct=float(sl_pct),
            tp_pct=float(tp_pct),
            sym_min=float(sym_min),
            min_pairs=int(min_pairs),
            entry_start=entry_start,
            force_exit_time=force_exit,
        )

        progress_bar = st.progress(0.0)
        status_text = st.empty()
        status_text.text("Loading options data (this can take a minute)...")

        def on_progress(i, total, date_str):
            progress_bar.progress(min((i + 1) / total, 1.0))
            status_text.text(f"Processing {date_str}  ({i + 1} / {total})")

        trades = engine.run(progress_callback=on_progress)

        progress_bar.empty()
        status_text.empty()

        if not trades:
            st.warning("No trades found for the selected parameters and date range.")
            return

        st.session_state["ivs_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "ivs_results" in st.session_state:
        _show_results(st.session_state["ivs_results"])


# ----------------------------------------------------------------
# Results display
# ----------------------------------------------------------------

def _show_results(df: pd.DataFrame):
    st.divider()
    st.subheader("Results")

    total = len(df)
    wins = int((df["pnl_inr"] > 0).sum())
    losses = total - wins
    win_rate = wins / total * 100 if total > 0 else 0
    total_pnl = df["pnl_inr"].sum()
    avg_pnl = df["pnl_inr"].mean()
    cum = df["pnl_inr"].cumsum()
    max_dd = (cum - cum.cummax()).min()

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Trades", total)
    c2.metric("Wins", wins)
    c3.metric("Losses", losses)
    c4.metric("Win Rate", f"{win_rate:.1f}%")
    c5.metric("Total P&L", f"₹{total_pnl:,.0f}")
    c6.metric("Avg P&L / Trade", f"₹{avg_pnl:,.0f}")

    r1, r2, r3, r4, r5 = st.columns(5)
    reasons = df["exit_reason"].value_counts()
    r1.metric("SL exits", int(reasons.get("SL", 0)))
    r2.metric("TP exits", int(reasons.get("TP", 0)))
    r3.metric("Force exits", int(reasons.get("FORCE", 0)))
    r4.metric("EOD exits", int(reasons.get("EOD", 0)))
    r5.metric("Max Drawdown", f"₹{max_dd:,.0f}")

    # Avg P&L by exit reason
    st.markdown("**Avg P&L by Exit Reason:**")
    reason_stats = df.groupby("exit_reason")["pnl_inr"].agg(["count", "mean", "sum"])
    reason_stats.columns = ["Count", "Avg P&L", "Total P&L"]
    st.dataframe(
        reason_stats.style.format({"Avg P&L": "₹{:,.0f}", "Total P&L": "₹{:,.0f}"}),
    )

    # Expiry vs non-expiry split
    st.markdown("**Expiry vs Non-Expiry Days:**")
    exp_stats = (
        df.groupby("is_expiry_day")["pnl_inr"]
        .agg(["count", "mean", "sum"])
        .rename(index={True: "Expiry day", False: "Normal day"})
    )
    exp_stats["win_rate"] = (
        df.groupby("is_expiry_day")
        .apply(lambda g: (g["pnl_inr"] > 0).mean() * 100, include_groups=False)
        .rename(index={True: "Expiry day", False: "Normal day"})
    )
    exp_stats.columns = ["Count", "Avg P&L", "Total P&L", "Win Rate %"]
    st.dataframe(
        exp_stats.style.format(
            {"Avg P&L": "₹{:,.0f}", "Total P&L": "₹{:,.0f}", "Win Rate %": "{:.1f}"}
        ),
    )

    # Equity curve
    st.divider()
    st.subheader("Equity Curve")
    chart_df = pd.DataFrame(
        {"Cumulative P&L (₹)": cum.values},
        index=range(1, len(cum) + 1),
    )
    chart_df.index.name = "Trade #"
    st.line_chart(chart_df)

    # All trades table
    st.divider()
    st.subheader("All Trades")

    filter_reason = st.multiselect(
        "Filter by exit reason",
        options=["SL", "TP", "FORCE", "EOD"],
        key="ivs_filter_reason",
    )

    filtered = df.copy()
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="iv_symmetry_straddle_backtest.csv",
        mime="text/csv",
        key="ivs_download",
    )
