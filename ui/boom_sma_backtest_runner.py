"""
Boom SMA Pullback Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.boom_sma_backtest import BoomSmaBacktestEngine, trades_to_dataframe


def render_boom_sma_backtest():
    st.header("Boom SMA Pullback")
    st.caption(
        "SMA + SuperTrend bias  →  SMA pullback entry  →  Dynamic ST SL + ratio TP  |  "
        "T-1 lag fix: no lookahead on entry or SL/TP"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-01").date(), key="bm_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-04-02").date(), key="bm_end"
            )

        with col2:
            st.markdown("**Indicators (1-min option close)**")
            sma_period = st.number_input(
                "SMA period (entry + signal)", value=13, step=1, min_value=2, key="bm_sma"
            )
            st.markdown("*SuperTrend — Signal*")
            st_sig_factor = st.number_input(
                "Factor", value=4, step=1, min_value=1, key="bm_st_sig_f"
            )
            st_sig_atr = st.number_input(
                "ATR period", value=11, step=1, min_value=1, key="bm_st_sig_a"
            )
            st.markdown("*SuperTrend — SL*")
            st_sl_factor = st.number_input(
                "Factor", value=3, step=1, min_value=1, key="bm_st_sl_f"
            )
            st_sl_atr = st.number_input(
                "ATR period", value=10, step=1, min_value=1, key="bm_st_sl_a"
            )

        with col3:
            st.markdown("**Exit**")
            tp_ratio = st.number_input(
                "TP ratio (× SL distance)", value=1.0, step=0.1, min_value=0.1, key="bm_tp_ratio"
            )
            max_sl_pct = st.number_input(
                "Max SL (%)", value=20.0, step=1.0, min_value=1.0, key="bm_max_sl"
            )
            max_loss_day = st.number_input(
                "Daily loss cap (%)", value=20.0, step=1.0, min_value=0.0, key="bm_daily_loss"
            )

    with st.expander("Time Windows", expanded=False):
        tc1, tc2 = st.columns(2)
        with tc1:
            trading_start = st.text_input("Entry start", value="09:30", key="bm_start_time")
        with tc2:
            trading_end = st.text_input("Entry end / EOD exit", value="14:45", key="bm_end_time")

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="bm_run"):
        engine = BoomSmaBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            sma_period=int(sma_period),
            st_signal_factor=int(st_sig_factor),
            st_signal_atr=int(st_sig_atr),
            st_sl_factor=int(st_sl_factor),
            st_sl_atr=int(st_sl_atr),
            tp_ratio=float(tp_ratio),
            max_sl_pct=float(max_sl_pct),
            max_loss_pct_per_day=float(max_loss_day) if max_loss_day > 0 else None,
            trading_start=trading_start,
            trading_end=trading_end,
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

        st.session_state["bm_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "bm_results" in st.session_state:
        _show_results(st.session_state["bm_results"])


# ----------------------------------------------------------------
# Results display
# ----------------------------------------------------------------

def _show_results(df: pd.DataFrame):
    st.divider()
    st.subheader("Results")

    total = len(df)
    wins = int((df["pnl_inr"] > 0).sum())
    losses = int((df["pnl_inr"] <= 0).sum())
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
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("ST Flip exits", int(reasons.get("ST_FLIP", 0)))
    r2.metric("SL exits", int(reasons.get("SL", 0)))
    r3.metric("TP exits", int(reasons.get("TP", 0)))
    r4.metric("EOD exits", int(reasons.get("EOD", 0)))
    r5.metric("Daily Loss exits", int(reasons.get("DAILY_LOSS", 0)))

    # Row 3: CE vs PE
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

    # Row 4: Lookahead verification
    same_candle = df[df["signal_time"] == df["entry_time"]]
    if len(same_candle) == 0:
        st.success("No lookahead: 0 same-candle signal+entry trades")
    else:
        st.warning(f"Lookahead detected: {len(same_candle)} same-candle signal+entry trades")

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
            "Filter by option type", options=["CE", "PE"], key="bm_filter_type"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason",
            options=["ST_FLIP", "SL", "TP", "EOD", "DAILY_LOSS"],
            key="bm_filter_reason",
        )
    with fc3:
        filter_date = st.text_input("Filter by date (YYYY-MM-DD)", value="", key="bm_filter_date")

    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]
    if filter_date:
        filtered = filtered[filtered["date"] == filter_date]

    st.dataframe(filtered, width="stretch", hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="boom_sma_pullback_backtest.csv",
        mime="text/csv",
        key="bm_download",
    )
