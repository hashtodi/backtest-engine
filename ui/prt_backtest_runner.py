"""
PRT Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.prt_backtest import PrtBacktestEngine, trades_to_dataframe


def render_prt_backtest():
    st.header("PRT Strategy")
    st.caption(
        "SuperTrend + MACD + VWAP + PCR  →  1-lot ATM option entry  →  "
        "TP / SL / EOD exits  •  ROI vs ₹25,000 fixed capital"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-01").date(), key="prt_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-04-02").date(), key="prt_end"
            )
            st.markdown("**Data**")
            indicator_source = st.selectbox(
                "Indicator source",
                options=["Futures", "Spot"],
                index=0,
                key="prt_ind_src",
            )
            spot_tf = st.selectbox(
                "Timeframe (min)", options=[3, 5], index=1, key="prt_tf"
            )

        with col2:
            st.markdown("**SuperTrend**")
            st_period = st.number_input(
                "ST period", value=10, step=1, min_value=1, key="prt_st_p"
            )
            st_factor = st.number_input(
                "ST factor", value=3.0, step=0.5, min_value=0.5, key="prt_st_f"
            )
            st.markdown("**MACD**")
            macd_fast = st.number_input(
                "Fast", value=12, step=1, min_value=1, key="prt_macd_f"
            )
            macd_slow = st.number_input(
                "Slow", value=26, step=1, min_value=1, key="prt_macd_s"
            )
            macd_signal = st.number_input(
                "Signal", value=9, step=1, min_value=1, key="prt_macd_sig"
            )

        with col3:
            st.markdown("**Exit**")
            tp_pct = st.number_input(
                "TP (%)", value=30.0, step=1.0, min_value=1.0, key="prt_tp"
            )
            sl_pct = st.number_input(
                "SL (%)", value=30.0, step=1.0, min_value=1.0, key="prt_sl"
            )
            st.markdown("**Capital**")
            initial_capital = st.number_input(
                "Initial capital (₹)",
                value=25000.0, step=1000.0, min_value=1000.0,
                key="prt_capital",
            )
            st.markdown("**PCR**")
            pcr_threshold = st.number_input(
                "PCR threshold", value=1.0, step=0.1, min_value=0.0, key="prt_pcr_th"
            )
            pcr_lookback = st.number_input(
                "PCR lookback (min)", value=30, step=5, min_value=1, key="prt_pcr_lb"
            )
            pcr_trend_min = st.number_input(
                "PCR trend min change", value=0.1, step=0.01, min_value=0.0, key="prt_pcr_trend"
            )

    with st.expander("Time & Limits", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            trading_start = st.text_input(
                "Entry start", value="09:30", key="prt_t_start"
            )
        with tc2:
            trading_end = st.text_input(
                "Entry end / EOD exit", value="14:30", key="prt_t_end"
            )
        with tc3:
            max_trades = st.number_input(
                "Max trades/day (0=unlimited)",
                value=0, step=1, min_value=0, key="prt_max_trades",
            )

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="prt_run"):
        engine = PrtBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            indicator_source=indicator_source.lower(),
            spot_timeframe=int(spot_tf),
            st_period=int(st_period),
            st_factor=float(st_factor),
            macd_fast=int(macd_fast),
            macd_slow=int(macd_slow),
            macd_signal=int(macd_signal),
            tp_pct=float(tp_pct),
            sl_pct=float(sl_pct),
            initial_capital=float(initial_capital),
            pcr_threshold=float(pcr_threshold),
            pcr_lookback=int(pcr_lookback),
            pcr_trend_min=float(pcr_trend_min),
            trading_start=trading_start,
            trading_end=trading_end,
            max_trades_per_day=int(max_trades),
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

        st.session_state["prt_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "prt_results" in st.session_state:
        _show_results(st.session_state["prt_results"])


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

    # Capital / ROI \u2014 derive initial from trade #0 (equity_after - pnl_inr)
    initial_capital = (
        float(df.iloc[0]["equity_after"]) - float(df.iloc[0]["pnl_inr"])
        if total > 0
        else 0.0
    )
    final_equity = (
        float(df.iloc[-1]["equity_after"]) if total > 0 else initial_capital
    )
    total_roi = (
        (final_equity - initial_capital) / initial_capital * 100
        if initial_capital > 0
        else 0.0
    )

    # Row 1: Key metrics
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Trades", total)
    c2.metric("Wins", wins)
    c3.metric("Losses", losses)
    c4.metric("Win Rate", f"{win_rate:.1f}%")
    c5.metric("Total P&L", f"\u20b9{total_pnl:,.0f}")
    c6.metric("Avg P&L / Trade", f"\u20b9{avg_pnl:,.0f}")

    # Row 2: Capital / ROI
    cap1, cap2, cap3, cap4 = st.columns(4)
    cap1.metric("Initial Capital", f"\u20b9{initial_capital:,.0f}")
    cap2.metric("Final Equity", f"\u20b9{final_equity:,.0f}")
    cap3.metric("Total ROI", f"{total_roi:.2f}%")
    cap4.metric(
        "Avg ROI / Trade",
        f"{df['roi_pct'].mean():.3f}%" if total > 0 else "0.000%",
    )

    # Row 3: Exit reason breakdown
    reasons = df["exit_reason"].value_counts()
    r1, r2, r3 = st.columns(3)
    r1.metric("SL exits", int(reasons.get("SL", 0)))
    r2.metric("TP exits", int(reasons.get("TP", 0)))
    r3.metric("EOD exits", int(reasons.get("EOD", 0)))

    # Row 4: CE vs PE
    ce_trades = df[df["option_type"] == "CE"]
    pe_trades = df[df["option_type"] == "PE"]
    d1, d2 = st.columns(2)
    with d1:
        ce_wr = (
            (ce_trades["pnl_inr"] > 0).mean() * 100 if len(ce_trades) > 0 else 0
        )
        st.markdown(
            f"**CE trades:** {len(ce_trades)}  |  "
            f"Win rate: {ce_wr:.1f}%  |  "
            f"P&L: \u20b9{ce_trades['pnl_inr'].sum():,.0f}"
        )
    with d2:
        pe_wr = (
            (pe_trades["pnl_inr"] > 0).mean() * 100 if len(pe_trades) > 0 else 0
        )
        st.markdown(
            f"**PE trades:** {len(pe_trades)}  |  "
            f"Win rate: {pe_wr:.1f}%  |  "
            f"P&L: \u20b9{pe_trades['pnl_inr'].sum():,.0f}"
        )

    # Equity curve \u2014 fixed 1-lot sizing, so this is just initial capital + cumulative P&L
    st.divider()
    st.subheader("Equity Curve")
    st.caption("Fixed 1-lot sizing \u2014 equity = initial capital + cumulative P&L")
    chart_df = pd.DataFrame(
        {"Equity (\u20b9)": df["equity_after"].values},
        index=range(1, len(df) + 1),
    )
    chart_df.index.name = "Trade #"
    st.line_chart(chart_df)

    # All trades table
    st.divider()
    st.subheader("All Trades")

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        filter_type = st.multiselect(
            "Filter by option type", options=["CE", "PE"], key="prt_filter_type"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason",
            options=["SL", "TP", "EOD"],
            key="prt_filter_reason",
        )
    with fc3:
        filter_date = st.text_input(
            "Filter by date (YYYY-MM-DD)", value="", key="prt_filter_date"
        )

    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]
    if filter_date:
        filtered = filtered[filtered["date"] == filter_date]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="prt_strategy_backtest.csv",
        mime="text/csv",
        key="prt_download",
    )
