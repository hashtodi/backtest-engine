"""
DEMA MTF VWAP Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.dema_mtf_vwap_backtest import DemaMtfVwapBacktestEngine, trades_to_dataframe


def render_dema_mtf_vwap_backtest():
    st.header("DEMA MTF VWAP")
    st.caption(
        "Spot vs DEMA(20) on 5m → CE/PE bias  |  Last 1H candle vs EMA(20) & EMA(50) confirmation  |  "
        "ATM option 5m close crosses above intraday VWAP (fresh crossover)  →  buy next candle open  |  "
        "Expiry day rolls to next weekly"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-20").date(), key="dmv_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-12").date(), key="dmv_end"
            )

        with col2:
            st.markdown("**Indicators**")
            dema_period = st.number_input(
                "DEMA period (5m spot)", value=20, step=1, min_value=2, key="dmv_dema"
            )
            mtf_ema_fast = st.number_input(
                "1H EMA fast", value=20, step=1, min_value=1, key="dmv_ema_fast"
            )
            mtf_ema_slow = st.number_input(
                "1H EMA slow", value=50, step=1, min_value=1, key="dmv_ema_slow"
            )

        with col3:
            st.markdown("**Exit (% of premium)**")
            sl_pct = st.number_input(
                "SL (%)", value=30.0, step=1.0, min_value=1.0, key="dmv_sl"
            )
            tp_pct = st.number_input(
                "TP (%)", value=50.0, step=1.0, min_value=1.0, key="dmv_tp"
            )

    with st.expander("Time Windows", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            entry_start = st.text_input("Entry window start", value="09:30", key="dmv_entry_start")
        with tc2:
            entry_end = st.text_input("Entry window end", value="14:45", key="dmv_entry_end")
        with tc3:
            force_exit = st.text_input("Force exit time", value="15:15", key="dmv_force_exit")

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="dmv_run"):
        engine = DemaMtfVwapBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            dema_period=int(dema_period),
            mtf_ema_fast=int(mtf_ema_fast),
            mtf_ema_slow=int(mtf_ema_slow),
            sl_pct=float(sl_pct),
            tp_pct=float(tp_pct),
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

        st.session_state["dmv_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "dmv_results" in st.session_state:
        _show_results(st.session_state["dmv_results"])


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
    r1, r2, r3 = st.columns(3)
    r1.metric("SL exits", int(reasons.get("SL", 0)))
    r2.metric("TP exits", int(reasons.get("TP", 0)))
    r3.metric("EOD exits", int(reasons.get("EOD", 0)))

    # Row 3: CE vs PE breakdown
    d1, d2 = st.columns(2)
    for col, otype in ((d1, "CE"), (d2, "PE")):
        subset = df[df["option_type"] == otype]
        wr = (subset["pnl_inr"] > 0).mean() * 100 if len(subset) > 0 else 0
        with col:
            st.markdown(
                f"**{otype} trades:** {len(subset)}  |  "
                f"Win rate: {wr:.1f}%  |  "
                f"P&L: ₹{subset['pnl_inr'].sum():,.0f}"
            )

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

    fc1, fc2 = st.columns(2)
    with fc1:
        filter_type = st.multiselect(
            "Filter by option type", options=["CE", "PE"], key="dmv_filter_type"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason", options=["SL", "TP", "EOD"], key="dmv_filter_reason"
        )

    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]

    st.dataframe(filtered, width="stretch", hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="dema_mtf_vwap_backtest.csv",
        mime="text/csv",
        key="dmv_download",
    )
