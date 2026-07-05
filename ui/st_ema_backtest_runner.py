"""
Supertrend + EMA Pullback Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.st_ema_backtest import StEmaBacktestEngine, trades_to_dataframe


def render_st_ema_backtest():
    st.header("Supertrend + EMA Pullback")
    st.caption(
        "Supertrend bias → EMA6/12 momentum → EMA12 limit pullback entry → "
        "ATM weekly option  |  TP: swing high/low  |  SL: TP/RR ratio"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-20").date(), key="se_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-12").date(), key="se_end"
            )

        with col2:
            st.markdown("**Indicators (5-min spot)**")
            st_period = st.number_input(
                "SuperTrend ATR period", value=12, step=1, min_value=1, key="se_st_period"
            )
            st_factor = st.number_input(
                "SuperTrend factor", value=3.0, step=0.5, min_value=0.5, key="se_st_factor"
            )
            ema_short = st.number_input(
                "EMA short period", value=6, step=1, min_value=1, key="se_ema_short"
            )
            ema_long = st.number_input(
                "EMA long period (entry level)", value=12, step=1, min_value=1, key="se_ema_long"
            )

        with col3:
            st.markdown("**Exit / Target**")
            min_target = st.number_input(
                "Min target (pts)", value=20.0, step=5.0, min_value=1.0, key="se_min_target"
            )
            rr_ratio = st.number_input(
                "Risk:Reward ratio", value=1.25, step=0.05, min_value=0.1, key="se_rr"
            )
            swing_lookback = st.number_input(
                "Swing lookback (5-min bars)", value=12, step=1, min_value=1, key="se_swing"
            )
            max_holding = st.number_input(
                "Max holding (mins)", value=30, step=5, min_value=0,
                help="0 = disabled. Exit if trade open longer than this.",
                key="se_max_hold"
            )

    with st.expander("Time Windows", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            entry_start = st.text_input("Entry window start", value="09:30", key="se_entry_start")
        with tc2:
            entry_end = st.text_input("Entry window end", value="14:55", key="se_entry_end")
        with tc3:
            force_exit = st.text_input("Force exit time", value="15:00", key="se_force_exit")

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="se_run"):
        engine = StEmaBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            supertrend_period=int(st_period),
            supertrend_factor=float(st_factor),
            ema_short_period=int(ema_short),
            ema_long_period=int(ema_long),
            min_target=float(min_target),
            rr_ratio=float(rr_ratio),
            swing_lookback=int(swing_lookback),
            max_holding_mins=int(max_holding),
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

        st.session_state["se_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "se_results" in st.session_state:
        _show_results(st.session_state["se_results"])


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
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("ST Flip exits", int(reasons.get("ST_FLIP", 0)))
    r2.metric("SL exits", int(reasons.get("SL", 0)))
    r3.metric("TP exits", int(reasons.get("TP", 0)))
    r4.metric("Max Bars exits", int(reasons.get("MAX_BARS", 0)))
    r5.metric("EOD exits", int(reasons.get("EOD", 0)))

    # Avg P&L by exit reason
    st.markdown("**Avg P&L by Exit Reason:**")
    reason_stats = df.groupby("exit_reason")["pnl_inr"].agg(["count", "mean", "sum"])
    reason_stats.columns = ["Count", "Avg P&L", "Total P&L"]
    st.dataframe(reason_stats.style.format({"Avg P&L": "\u20b9{:,.0f}", "Total P&L": "\u20b9{:,.0f}"}))

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

    fc1, fc2 = st.columns(2)
    with fc1:
        filter_type = st.multiselect(
            "Filter by option type", options=["CE", "PE"], key="se_filter_type"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason",
            options=["ST_FLIP", "SL", "TP", "MAX_BARS", "EOD"],
            key="se_filter_reason",
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
        file_name="st_ema_pullback_backtest.csv",
        mime="text/csv",
        key="se_download",
    )
