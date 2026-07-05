"""
VWAP + EMA + RSI Momentum Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.vwap_ema_rsi_backtest import VwapEmaRsiBacktestEngine, trades_to_dataframe


def render_vwap_ema_rsi_backtest():
    st.header("VWAP + EMA + RSI Momentum")
    st.caption(
        "VWAP offset + EMA9/20 crossover + RSI momentum + strong candle → "
        "ATM weekly option  |  Spot-based SL/TP  |  3-stage trailing SL"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-20").date(), key="ver_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-12").date(), key="ver_end"
            )

        with col2:
            st.markdown("**Indicators (5-min spot)**")
            ema_short = st.number_input(
                "EMA short period", value=9, step=1, min_value=1, key="ver_ema_short"
            )
            ema_long = st.number_input(
                "EMA long period", value=20, step=1, min_value=1, key="ver_ema_long"
            )
            rsi_period = st.number_input(
                "RSI period", value=14, step=1, min_value=1, key="ver_rsi_period"
            )
            vwap_offset = st.number_input(
                "VWAP offset %", value=0.2, step=0.05, min_value=0.0,
                format="%.2f", key="ver_vwap_offset",
                help="Spot must be at least this % above/below VWAP"
            )

        with col3:
            st.markdown("**Signal Thresholds**")
            rsi_upper = st.number_input(
                "RSI upper (CE signal)", value=55.0, step=1.0,
                min_value=50.0, max_value=90.0, key="ver_rsi_upper"
            )
            rsi_lower = st.number_input(
                "RSI lower (PE signal)", value=45.0, step=1.0,
                min_value=10.0, max_value=50.0, key="ver_rsi_lower"
            )

    with st.expander("Filters", expanded=True):
        fc1, fc2 = st.columns(2)
        with fc1:
            st.markdown("**Strong Candle Filter**")
            candle_strength = st.number_input(
                "Close in top/bottom N% of range", value=25.0, step=5.0,
                min_value=5.0, max_value=50.0, key="ver_candle_str",
                help="CE: close in top N% of 5-min bar range. PE: bottom N%."
            )
        with fc2:
            st.markdown("**EMA Flatness Filter**")
            ema_flat = st.number_input(
                "EMA flat threshold (% of spot)", value=0.05, step=0.01,
                min_value=0.0, format="%.2f", key="ver_ema_flat",
                help="Skip if |EMA9 - EMA20| <= this % of spot price"
            )

    with st.expander("Exit & Trailing SL", expanded=True):
        col4, col5 = st.columns(2)

        with col4:
            st.markdown("**SL / TP (spot points)**")
            sl_points = st.number_input(
                "Stop loss points", value=15.0, step=1.0, min_value=1.0, key="ver_sl_pts"
            )
            tp_points = st.number_input(
                "Target profit points", value=30.0, step=1.0, min_value=1.0, key="ver_tp_pts"
            )

        with col5:
            st.markdown("**3-Stage Trailing SL**")
            trail_s1 = st.number_input(
                "Stage 1: trigger (pts)", value=15.0, step=1.0, min_value=1.0,
                key="ver_trail_s1",
                help="At +N pts profit → SL moves to cost"
            )
            trail_s2 = st.number_input(
                "Stage 2: trigger (pts)", value=20.0, step=1.0, min_value=1.0,
                key="ver_trail_s2",
                help="At +N pts profit → SL locks some profit"
            )
            trail_lock = st.number_input(
                "Stage 2: lock profit (pts)", value=10.0, step=1.0, min_value=0.0,
                key="ver_trail_lock",
                help="At stage 2, SL moves to entry + this"
            )
            trail_dist = st.number_input(
                "Stage 3: trail distance (pts)", value=10.0, step=1.0, min_value=1.0,
                key="ver_trail_dist",
                help="Beyond stage 2, SL trails N pts from peak"
            )

    with st.expander("Risk & Session", expanded=False):
        col6, col7 = st.columns(2)

        with col6:
            st.markdown("**Daily Limits**")
            max_trades = st.number_input(
                "Max trades per day", value=3, step=1, min_value=1, key="ver_max_trades"
            )
            max_consec = st.number_input(
                "Max consecutive losses", value=2, step=1, min_value=1, key="ver_max_consec",
                help="Stop trading after N consecutive losses (resets daily)"
            )

        with col7:
            st.markdown("**Time Windows**")
            entry_start = st.text_input(
                "Entry window start", value="09:20", key="ver_entry_start"
            )
            force_exit = st.text_input(
                "Force exit time", value="14:30", key="ver_force_exit"
            )

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="ver_run"):
        engine = VwapEmaRsiBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            ema_short_period=int(ema_short),
            ema_long_period=int(ema_long),
            rsi_period=int(rsi_period),
            rsi_upper=float(rsi_upper),
            rsi_lower=float(rsi_lower),
            vwap_offset_pct=float(vwap_offset),
            sl_points=float(sl_points),
            tp_points=float(tp_points),
            trail_stage1_trigger=float(trail_s1),
            trail_stage2_trigger=float(trail_s2),
            trail_stage2_lock=float(trail_lock),
            trail_distance=float(trail_dist),
            max_trades_per_day=int(max_trades),
            max_consecutive_losses=int(max_consec),
            ema_flat_pct=float(ema_flat),
            candle_strength_pct=float(candle_strength),
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

        st.session_state["ver_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "ver_results" in st.session_state:
        _show_results(st.session_state["ver_results"])


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
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("SL exits", int(reasons.get("SL", 0)))
    r2.metric("TP exits", int(reasons.get("TP", 0)))
    r3.metric("Trail SL exits", int(reasons.get("TRAIL_SL", 0)))
    r4.metric("EOD exits", int(reasons.get("EOD", 0)))

    # Avg P&L by exit reason
    st.markdown("**Avg P&L by Exit Reason:**")
    reason_stats = df.groupby("exit_reason")["pnl_inr"].agg(["count", "mean", "sum"])
    reason_stats.columns = ["Count", "Avg P&L", "Total P&L"]
    st.dataframe(
        reason_stats.style.format({"Avg P&L": "\u20b9{:,.0f}", "Total P&L": "\u20b9{:,.0f}"}),
        use_container_width=True,
    )

    # CE vs PE
    ce_trades = df[df["option_type"] == "CE"]
    pe_trades = df[df["option_type"] == "PE"]
    d1, d2 = st.columns(2)
    with d1:
        ce_wr = (ce_trades["pnl_inr"] > 0).mean() * 100 if len(ce_trades) > 0 else 0
        st.markdown(
            f"**CE trades:** {len(ce_trades)}  |  "
            f"Win rate: {ce_wr:.1f}%  |  "
            f"P&L: \u20b9{ce_trades['pnl_inr'].sum():,.0f}"
        )
    with d2:
        pe_wr = (pe_trades["pnl_inr"] > 0).mean() * 100 if len(pe_trades) > 0 else 0
        st.markdown(
            f"**PE trades:** {len(pe_trades)}  |  "
            f"Win rate: {pe_wr:.1f}%  |  "
            f"P&L: \u20b9{pe_trades['pnl_inr'].sum():,.0f}"
        )

    # Trail activation stats
    trail_count = int(df["trail_triggered"].sum())
    st.markdown(f"**Trailing SL activated:** {trail_count} / {total} trades ({trail_count/total*100:.1f}%)")

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

    # Daily P&L
    st.subheader("Daily P&L")
    daily_pnl = df.groupby("date")["pnl_inr"].sum()
    st.bar_chart(daily_pnl)

    # All trades table
    st.divider()
    st.subheader("All Trades")

    fc1, fc2 = st.columns(2)
    with fc1:
        filter_type = st.multiselect(
            "Filter by option type", options=["CE", "PE"], key="ver_filter_type"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason",
            options=["SL", "TP", "TRAIL_SL", "EOD"],
            key="ver_filter_reason",
        )

    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="vwap_ema_rsi_momentum_backtest.csv",
        mime="text/csv",
        key="ver_download",
    )
