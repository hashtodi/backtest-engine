"""
EMA5 Futures Breakout Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.ema5_futures_backtest import Ema5FuturesBacktestEngine, trades_to_dataframe


def render_ema5_futures_backtest():
    st.header("EMA5 Futures Breakout")
    st.caption(
        "5 EMA on Nifty Futures  →  Alert candle (EMA outside H-L)  →  "
        "Breakout confirmation  →  ITM option entry  |  "
        "SL/TP on futures prices, P&L from options"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2026-01-06").date(), key="e5f_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-31").date(), key="e5f_end"
            )

        with col2:
            st.markdown("**Indicator**")
            ema_period = st.number_input(
                "EMA period", value=5, step=1, min_value=2, key="e5f_ema"
            )
            signal_tf = st.selectbox(
                "Signal timeframe (min)",
                options=[1, 3, 5],
                index=1,  # default 3-min
                key="e5f_signal_tf",
                help="Alert & confirmation on this timeframe. SL/TP always checked on 1-min."
            )
            strike_depth = st.number_input(
                "ITM strike depth", value=1, step=1, min_value=1, max_value=5,
                key="e5f_depth",
                help="Number of strikes ITM from ATM (ATM from spot, then -50*N CE / +50*N PE)"
            )

        with col3:
            st.markdown("**Exit (futures points)**")
            sl_buffer = st.number_input(
                "SL buffer (points)", value=5.0, step=1.0, min_value=0.0,
                key="e5f_sl_buffer",
                help="Points subtracted/added from alert candle close for SL"
            )
            rr_ratio = st.number_input(
                "Risk:Reward ratio", value=1.0, step=0.1, min_value=0.1,
                key="e5f_rr",
                help="Target = entry + risk * this ratio"
            )

    with st.expander("Time Windows", expanded=False):
        tc1, tc2 = st.columns(2)
        with tc1:
            entry_start = st.text_input(
                "Entry window start", value="09:30", key="e5f_entry_start"
            )
        with tc2:
            force_exit = st.text_input(
                "Force exit time", value="15:00", key="e5f_force_exit"
            )

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="e5f_run"):
        engine = Ema5FuturesBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            ema_period=int(ema_period),
            signal_tf=int(signal_tf),
            sl_buffer=float(sl_buffer),
            rr_ratio=float(rr_ratio),
            entry_start=entry_start,
            force_exit_time=force_exit,
            strike_depth=int(strike_depth),
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

        st.session_state["e5f_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "e5f_results" in st.session_state:
        _show_results(st.session_state["e5f_results"])


# ----------------------------------------------------------------
# Results display
# ----------------------------------------------------------------

def _show_results(df: pd.DataFrame):
    st.divider()
    st.subheader("Results")

    total = len(df)
    wins = int((df["pnl_inr"] > 0).sum())
    losses = int((df["pnl_inr"] < 0).sum())
    breakeven = total - wins - losses
    win_rate = wins / total * 100 if total > 0 else 0
    total_pnl = df["pnl_inr"].sum()
    avg_pnl = df["pnl_inr"].mean()
    total_fut_pts = df["pnl_futures_points"].sum()

    # Row 1: Key metrics
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Trades", total)
    c2.metric("Wins", wins)
    c3.metric("Losses", losses)
    c4.metric("Win Rate", f"{win_rate:.1f}%")
    c5.metric("Total P&L", f"\u20b9{total_pnl:,.0f}")
    c6.metric("Futures Points", f"{total_fut_pts:+,.1f}")

    # Row 2: Exit reason breakdown
    reasons = df["exit_reason"].value_counts()
    r1, r2, r3 = st.columns(3)
    r1.metric("SL exits", int(reasons.get("SL", 0)))
    r2.metric("TP exits", int(reasons.get("TP", 0)))
    r3.metric("EOD exits", int(reasons.get("EOD", 0)))

    # Row 3: CE vs PE breakdown
    ce_trades = df[df["direction"] == "CE"]
    pe_trades = df[df["direction"] == "PE"]
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

    # Daily P&L summary
    st.divider()
    st.subheader("Daily Summary")
    daily = df.groupby("date").agg(
        trades=("pnl_inr", "size"),
        pnl_inr=("pnl_inr", "sum"),
        fut_pts=("pnl_futures_points", "sum"),
        wins=("pnl_inr", lambda x: (x > 0).sum()),
    ).reset_index()
    daily["win_rate"] = (daily["wins"] / daily["trades"] * 100).round(1)
    daily["cum_pnl"] = daily["pnl_inr"].cumsum()

    profitable_days = (daily["pnl_inr"] > 0).sum()
    st.markdown(
        f"**Profitable days:** {profitable_days} / {len(daily)}  |  "
        f"**Best day:** \u20b9{daily['pnl_inr'].max():,.0f}  |  "
        f"**Worst day:** \u20b9{daily['pnl_inr'].min():,.0f}"
    )

    # Equity curve
    st.divider()
    st.subheader("Equity Curve")
    equity = df["pnl_inr"].cumsum()
    chart_df = pd.DataFrame(
        {"Cumulative P&L (\u20b9)": equity.values}, index=range(1, len(equity) + 1)
    )
    chart_df.index.name = "Trade #"
    st.line_chart(chart_df)

    # All trades table
    st.divider()
    st.subheader("All Trades")

    fc1, fc2 = st.columns(2)
    with fc1:
        filter_dir = st.multiselect(
            "Filter by direction", options=["CE", "PE"], key="e5f_filter_dir"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason", options=["SL", "TP", "EOD"],
            key="e5f_filter_reason"
        )

    filtered = df.copy()
    if filter_dir:
        filtered = filtered[filtered["direction"].isin(filter_dir)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]

    st.dataframe(filtered, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="ema5_futures_breakout_backtest.csv",
        mime="text/csv",
        key="e5f_download",
    )
