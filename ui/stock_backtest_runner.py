"""
Stock Gap + EMA5 Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.stock_backtest import StockBacktestEngine, trades_to_dataframe


def render_stock_backtest():
    st.header("Stock Gap + EMA5 Strategy")
    st.caption(
        "Gap ≥ 1.5% + Volume ≥ 8× 3-day avg + Vol×Price ≥ 4Cr  →  enter at EMA5 touch (9:15–9:30)  |  SL 1%  |  TP 1.5%"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2023-01-01").date(), key="sb_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-30").date(), key="sb_end"
            )

        with col2:
            st.markdown("**Signal Conditions**")
            gap_pct = st.number_input(
                "Gap threshold (%)", value=1.5, step=0.1, min_value=0.1, key="sb_gap"
            )
            vol_mult = st.number_input(
                "Volume multiplier (×avg)", value=8.0, step=0.5, min_value=1.0, key="sb_volmult"
            )
            min_val_cr = st.number_input(
                "Min vol×price (crore)", value=4.0, step=0.5, min_value=0.1, key="sb_minval"
            )

        with col3:
            st.markdown("**Entry / Exit**")
            ema_period = st.number_input(
                "EMA period", value=5, step=1, min_value=1, key="sb_ema"
            )
            sl_pct = st.number_input(
                "SL (%)", value=1.0, step=0.1, min_value=0.1, key="sb_sl"
            )
            tp_pct = st.number_input(
                "TP (%)", value=1.5, step=0.1, min_value=0.1, key="sb_tp"
            )

    # ----------------------------------------------------------------
    # Stock selector
    # ----------------------------------------------------------------
    _engine_probe = StockBacktestEngine(str(start_date), str(end_date))
    available = _engine_probe.get_available_stocks()

    selected_stocks = st.multiselect(
        f"Stocks to backtest ({len(available)} available — leave empty to run all)",
        options=available,
        default=[],
        key="sb_stocks",
    )

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="sb_run"):
        stocks = selected_stocks if selected_stocks else None
        engine = StockBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            stocks=stocks,
            gap_pct_threshold=gap_pct,
            volume_multiplier=vol_mult,
            min_value_cr=min_val_cr,
            ema_period=int(ema_period),
            sl_pct=sl_pct,
            tp_pct=tp_pct,
        )

        total_symbols = len(stocks) if stocks else len(available)
        progress_bar = st.progress(0.0)
        status_text = st.empty()

        def on_progress(i, n, symbol):
            progress_bar.progress((i + 1) / n)
            status_text.text(f"Processing {symbol}  ({i + 1} / {n})")

        trades = engine.run(progress_callback=on_progress)

        progress_bar.empty()
        status_text.empty()

        if not trades:
            st.warning("No trades found for the selected parameters and date range.")
            return

        st.session_state["stock_bt_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "stock_bt_results" in st.session_state:
        _show_results(st.session_state["stock_bt_results"])


# ----------------------------------------------------------------
# Results display
# ----------------------------------------------------------------

def _show_results(df: pd.DataFrame):
    st.divider()
    st.subheader("Results")

    total = len(df)
    wins = int((df["pnl"] > 0).sum())
    losses = int((df["pnl"] < 0).sum())
    win_rate = wins / total * 100 if total > 0 else 0
    total_pnl = df["pnl"].sum()
    avg_pnl = df["pnl"].mean()

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Trades", total)
    c2.metric("Wins", wins)
    c3.metric("Losses", losses)
    c4.metric("Win Rate", f"{win_rate:.1f}%")
    c5.metric("Total P&L (₹)", f"{total_pnl:,.2f}")
    c6.metric("Avg P&L / Trade", f"{avg_pnl:.2f}")

    # Exit reason breakdown
    reasons = df["exit_reason"].value_counts()
    r1, r2, r3 = st.columns(3)
    r1.metric("SL exits", int(reasons.get("SL", 0)))
    r2.metric("TP exits", int(reasons.get("TP", 0)))
    r3.metric("EOD exits", int(reasons.get("EOD", 0)))

    # Direction breakdown
    long_trades = df[df["direction"] == "long"]
    short_trades = df[df["direction"] == "short"]
    d1, d2 = st.columns(2)
    with d1:
        st.markdown(
            f"**Long trades:** {len(long_trades)}  |  "
            f"Win rate: {(long_trades['pnl'] > 0).mean() * 100:.1f}%  |  "
            f"P&L: ₹{long_trades['pnl'].sum():,.2f}"
        )
    with d2:
        st.markdown(
            f"**Short trades:** {len(short_trades)}  |  "
            f"Win rate: {(short_trades['pnl'] > 0).mean() * 100:.1f}%  |  "
            f"P&L: ₹{short_trades['pnl'].sum():,.2f}"
        )

    st.divider()

    # Per-stock summary
    st.subheader("Per-Stock Summary")
    stock_summary = (
        df.groupby("symbol")
        .agg(
            trades=("pnl", "count"),
            wins=("pnl", lambda x: (x > 0).sum()),
            total_pnl=("pnl", "sum"),
            avg_pnl=("pnl", "mean"),
            best_trade=("pnl", "max"),
            worst_trade=("pnl", "min"),
        )
        .reset_index()
    )
    stock_summary["win_rate_%"] = (
        stock_summary["wins"] / stock_summary["trades"] * 100
    ).round(1)
    stock_summary["total_pnl"] = stock_summary["total_pnl"].round(2)
    stock_summary["avg_pnl"] = stock_summary["avg_pnl"].round(2)
    stock_summary = stock_summary.sort_values("total_pnl", ascending=False)

    st.dataframe(
        stock_summary[
            ["symbol", "trades", "wins", "win_rate_%", "total_pnl", "avg_pnl", "best_trade", "worst_trade"]
        ],
        width="stretch",
        hide_index=True,
    )

    st.divider()

    # All trades table
    st.subheader("All Trades")

    # Filters
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        filter_symbol = st.multiselect(
            "Filter by stock", options=sorted(df["symbol"].unique()), key="sb_filter_sym"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason", options=["SL", "TP", "EOD"], key="sb_filter_reason"
        )
    with fc3:
        filter_dir = st.multiselect(
            "Filter by direction", options=["long", "short"], key="sb_filter_dir"
        )

    filtered = df.copy()
    if filter_symbol:
        filtered = filtered[filtered["symbol"].isin(filter_symbol)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]
    if filter_dir:
        filtered = filtered[filtered["direction"].isin(filter_dir)]

    st.dataframe(filtered, width="stretch", hide_index=True)

    # Download
    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="stock_gap_ema5_backtest.csv",
        mime="text/csv",
        key="sb_download",
    )
