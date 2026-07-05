"""
Inter-Sector Pairs Trading — Backtest Runner UI.
"""

import streamlit as st
import pandas as pd

from engine.pairs_backtest import DEFAULT_UNIVERSE, PairsBacktestEngine, trades_to_dataframe


def render_pairs_backtest():
    st.header("Inter-Sector Pairs Trading Strategy")
    st.caption(
        "EOD scan: RSI(14) filter → RS(50) rank  |  Enter pair at 9:15 next day (qty=1 each)  "
        "|  Combined TP 1%  |  Combined SL 0.75%"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2023-01-01").date(), key="pt_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-03-12").date(), key="pt_end"
            )

        with col2:
            st.markdown("**RSI Filter (Daily)**")
            rsi_period    = st.number_input("RSI period",        value=14, step=1, min_value=2,  key="pt_rsi_period")
            rsi_ma_period = st.number_input("RSI MA period",     value=14, step=1, min_value=2,  key="pt_rsi_ma")
            rs_period     = st.number_input("RS period (5-min)", value=50, step=5, min_value=10, key="pt_rs_period")

        with col3:
            st.markdown("**Exit (Combined)**")
            tp_pct = st.number_input("TP (%)", value=1.0,  step=0.1,  min_value=0.1,  key="pt_tp")
            sl_pct = st.number_input("SL (%)", value=0.75, step=0.05, min_value=0.05, key="pt_sl")

    with st.expander("Universe", expanded=False):
        for sector, stocks in DEFAULT_UNIVERSE.items():
            st.markdown(f"**{sector}**: {', '.join(stocks)}")

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="pt_run"):
        engine = PairsBacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            rsi_period=int(rsi_period),
            rsi_ma_period=int(rsi_ma_period),
            rs_period=int(rs_period),
            tp_pct=float(tp_pct),
            sl_pct=float(sl_pct),
        )

        total = len(engine.all_stocks) + 1
        progress_bar = st.progress(0.0)
        status_text  = st.empty()

        def on_progress(i, n, symbol):
            progress_bar.progress(min((i + 1) / n, 1.0))
            status_text.text(f"Loading {symbol}  ({i + 1} / {n})")

        trades = engine.run(progress_callback=on_progress)
        progress_bar.empty()
        status_text.empty()

        if not trades:
            st.warning("No trades found. Try a wider date range or check that stock data exists.")
            return

        st.session_state["pt_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "pt_results" in st.session_state:
        _show_results(st.session_state["pt_results"])


# ----------------------------------------------------------------
# Results display
# ----------------------------------------------------------------

def _show_results(df: pd.DataFrame):
    st.divider()
    st.subheader("Results")

    total    = len(df)
    wins     = int((df["combined_pnl_pct"] > 0).sum())
    losses   = int((df["combined_pnl_pct"] <= 0).sum())
    win_rate = wins / total * 100 if total else 0
    total_pct = df["combined_pnl_pct"].sum()
    total_inr = df["combined_pnl_inr"].sum()
    avg_pct   = df["combined_pnl_pct"].mean()

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Trades",        total)
    c2.metric("Wins",                wins)
    c3.metric("Losses",              losses)
    c4.metric("Win Rate",            f"{win_rate:.1f}%")
    c5.metric("Cumulative PnL%",     f"{total_pct:.2f}%")
    c6.metric("Total PnL ₹ (qty=1)", f"₹{total_inr:,.2f}")

    reasons = df["exit_reason"].value_counts()
    r1, r2, r3 = st.columns(3)
    r1.metric("TP exits",  int(reasons.get("TP",  0)))
    r2.metric("SL exits",  int(reasons.get("SL",  0)))
    r3.metric("EOD exits", int(reasons.get("EOD", 0)))

    st.divider()

    # Per-stock frequency
    st.subheader("Stock Frequency")
    col_l, col_r = st.columns(2)

    with col_l:
        st.markdown("**Long leg**")
        long_stats = (
            df.groupby("long_stock")
            .agg(
                count     =("combined_pnl_pct", "count"),
                pair_wins =("combined_pnl_pct", lambda x: (x > 0).sum()),
                leg_pnl   =("long_pnl_pct",     "sum"),
            )
            .reset_index()
            .sort_values("count", ascending=False)
        )
        long_stats["pair_win_%"] = (long_stats["pair_wins"] / long_stats["count"] * 100).round(1)
        long_stats["leg_pnl"]    = long_stats["leg_pnl"].round(2)
        st.dataframe(
            long_stats[["long_stock", "count", "pair_win_%", "leg_pnl"]],
            hide_index=True,
        )

    with col_r:
        st.markdown("**Short leg**")
        short_stats = (
            df.groupby("short_stock")
            .agg(
                count     =("combined_pnl_pct", "count"),
                pair_wins =("combined_pnl_pct", lambda x: (x > 0).sum()),
                leg_pnl   =("short_pnl_pct",    "sum"),
            )
            .reset_index()
            .sort_values("count", ascending=False)
        )
        short_stats["pair_win_%"] = (short_stats["pair_wins"] / short_stats["count"] * 100).round(1)
        short_stats["leg_pnl"]    = short_stats["leg_pnl"].round(2)
        st.dataframe(
            short_stats[["short_stock", "count", "pair_win_%", "leg_pnl"]],
            hide_index=True,
        )

    st.divider()

    # All trades table
    st.subheader("All Trades")

    fc1, fc2 = st.columns(2)
    with fc1:
        filter_reason = st.multiselect(
            "Exit reason", ["TP", "SL", "EOD"], key="pt_filter_reason"
        )
    with fc2:
        all_syms = sorted(set(df["long_stock"].tolist() + df["short_stock"].tolist()))
        filter_stock = st.multiselect(
            "Stock (either leg)", all_syms, key="pt_filter_stock"
        )

    filtered = df.copy()
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]
    if filter_stock:
        filtered = filtered[
            filtered["long_stock"].isin(filter_stock) |
            filtered["short_stock"].isin(filter_stock)
        ]

    display_cols = [
        "trade_date", "scan_date",
        "long_stock",  "long_rsi",  "long_rs",  "long_entry_price",  "long_exit_price",  "long_pnl_pct",
        "short_stock", "short_rsi", "short_rs", "short_entry_price", "short_exit_price", "short_pnl_pct",
        "combined_pnl_pct", "combined_pnl_inr", "exit_time", "exit_reason",
    ]
    st.dataframe(filtered[display_cols], hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=filtered.to_csv(index=False),
        file_name="pairs_backtest.csv",
        mime="text/csv",
        key="pt_download",
    )
