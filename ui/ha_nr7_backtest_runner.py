"""
HA-NR7 Strategy — Backtest Runner UI.

Renders the full backtest tab: parameters, run button, results, download.
"""

import streamlit as st
import pandas as pd

from engine.ha_nr7_backtest import HaNr7BacktestEngine, trades_to_dataframe


def render_ha_nr7_backtest():
    st.header("HA-NR7 Strategy")
    st.caption(
        "Heikin-Ashi neutral candle alert  →  NR7 on ITM option  →  "
        "DTE-based TP/SL  →  Pyramid up to 3 lots  →  Reversal on opposite NR7"
    )

    # ----------------------------------------------------------------
    # Parameters
    # ----------------------------------------------------------------
    with st.expander("Strategy Parameters", expanded=True):
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("**Date Range**")
            start_date = st.date_input(
                "From", value=pd.Timestamp("2025-01-01").date(), key="hanr7_start"
            )
            end_date = st.date_input(
                "To", value=pd.Timestamp("2026-04-13").date(), key="hanr7_end"
            )
            st.markdown("**Instrument**")
            instrument = st.selectbox(
                "Instrument",
                options=["NIFTY"],
                index=0,
                key="hanr7_instrument",
            )
            strike_rounding = st.number_input(
                "Strike rounding",
                value=100, step=50, min_value=50, key="hanr7_strike_round",
            )

        with col2:
            st.markdown("**Heikin-Ashi Alert**")
            ha_body_threshold = st.number_input(
                "HA body threshold",
                value=2.5, step=0.5, min_value=0.1, key="hanr7_ha_body",
            )
            ha_range_threshold = st.number_input(
                "HA range threshold",
                value=20.0, step=1.0, min_value=1.0, key="hanr7_ha_range",
            )
            st.markdown("**NR7**")
            nr7_lookback = st.number_input(
                "NR7 lookback",
                value=7, step=1, min_value=2, key="hanr7_nr7_lb",
            )
            nr7_scan_window = st.number_input(
                "NR7 scan window",
                value=5, step=1, min_value=1, key="hanr7_nr7_scan",
            )

        with col3:
            st.markdown("**EMA (on option prices)**")
            ema_short_period = st.number_input(
                "EMA short period",
                value=10, step=1, min_value=2, key="hanr7_ema_short",
            )
            ema_long_period = st.number_input(
                "EMA long period",
                value=21, step=1, min_value=2, key="hanr7_ema_long",
            )

    # ----------------------------------------------------------------
    # Session Timing
    # ----------------------------------------------------------------
    with st.expander("Session Timing", expanded=False):
        tc1, tc2, tc3 = st.columns(3)
        with tc1:
            trading_start = st.text_input(
                "Trading start", value="09:30", key="hanr7_t_start"
            )
        with tc2:
            last_entry = st.text_input(
                "Last entry", value="14:45", key="hanr7_t_last_entry"
            )
        with tc3:
            force_exit = st.text_input(
                "Force exit", value="14:55", key="hanr7_t_force_exit"
            )

    # ----------------------------------------------------------------
    # DTE TP/SL Reference Table
    # ----------------------------------------------------------------
    with st.expander("DTE TP/SL Reference Table", expanded=False):
        st.markdown(
            """
| Trading DTE | Base TP (%) | Base SL (%) | EMA Adjustment |
|:-----------:|:-----------:|:-----------:|:---------------|
| 0           | 15.0        | 15.0        | If price > both EMAs and TP >= 7.5 → TP = 5% |
| 1           | 12.5        | 12.5        | If price < both EMAs and TP <= 7.5 → TP = 10% |
| 2           | 10.0        | 10.0        | Otherwise → no change |
| 3           | 7.5         | 7.5         | |
| >= 4        | 5.0         | 7.5         | |
"""
        )

    # ----------------------------------------------------------------
    # Run
    # ----------------------------------------------------------------
    if st.button("Run Backtest", type="primary", key="hanr7_run"):
        engine = HaNr7BacktestEngine(
            start_date=str(start_date),
            end_date=str(end_date),
            instrument=instrument,
            strike_rounding=int(strike_rounding),
            ha_body_threshold=float(ha_body_threshold),
            ha_range_threshold=float(ha_range_threshold),
            nr7_lookback=int(nr7_lookback),
            nr7_scan_window=int(nr7_scan_window),
            ema_short_period=int(ema_short_period),
            ema_long_period=int(ema_long_period),
            trading_start=trading_start,
            last_entry=last_entry,
            force_exit=force_exit,
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

        st.session_state["hanr7_results"] = trades_to_dataframe(trades)
        st.rerun()

    # ----------------------------------------------------------------
    # Results
    # ----------------------------------------------------------------
    if "hanr7_results" in st.session_state:
        _show_results(st.session_state["hanr7_results"])


# ----------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------


def _time_only(val) -> str:
    """Extract HH:MM from a datetime string like '2026-03-30 10:00:00+05:30'."""
    if pd.isna(val) or val == "":
        return ""
    s = str(val)
    # Handle '2026-03-30 10:00:00+05:30' or '2026-03-30 10:00'
    if " " in s:
        time_part = s.split(" ")[1]
        # Strip timezone suffix like '+05:30'
        if "+" in time_part:
            time_part = time_part.split("+")[0]
        # Return HH:MM only
        parts = time_part.split(":")
        return f"{parts[0]}:{parts[1]}"
    return s


def _clean_entry_prices(val) -> str:
    """Format entry_prices from '[87.2]' or '[310.6, 287.9]' to '87.20' or '310.60, 287.90'."""
    s = str(val).strip("[] ")
    if not s:
        return ""
    prices = [p.strip() for p in s.split(",")]
    return ", ".join(f"{float(p):.2f}" for p in prices if p)


def _format_entry_times(val) -> str:
    """Format entry_times list string to show HH:MM per lot."""
    s = str(val).strip("[] '\"")
    if not s:
        return ""
    times = [t.strip().strip("'\"") for t in s.split(",")]
    return ", ".join(_time_only(t) for t in times if t)


def _format_trades_for_display(df: pd.DataFrame) -> pd.DataFrame:
    """Reformat trades DataFrame for audit-friendly display."""
    out = pd.DataFrame()
    out["date"] = df["entry_date"]
    out["type"] = df["option_type"]
    out["strike"] = df["strike"]
    out["lots"] = df["num_lots"]
    out["alert"] = df["alert_candle_time"].apply(_time_only)
    out["entries"] = df["entry_times"].apply(_format_entry_times)
    out["exit"] = df["exit_time"].apply(_time_only)
    out["entry_prices"] = df["entry_prices"].apply(_clean_entry_prices)
    out["avg_entry"] = df["avg_entry"].apply(lambda x: f"{x:.2f}")
    out["exit_price"] = df["exit_price"].apply(lambda x: f"{x:.2f}")
    out["reason"] = df["exit_reason"]
    out["dte"] = df["dte"]
    out["tp%"] = df["tp_pct"]
    out["sl%"] = df["sl_pct"]
    out["ema_adj"] = df["ema_adjusted"].map({True: "Y", False: ""})
    out["reversal"] = df["is_reversal"].map({True: "Y", False: ""})
    out["pnl_pts"] = df["pnl_points"].apply(lambda x: f"{x:.2f}")
    out["pnl_pct"] = df.apply(
        lambda r: f"{(r['pnl_points'] / r['avg_entry'] * 100):.1f}%" if r["avg_entry"] > 0 else "",
        axis=1,
    )
    out["pnl_inr"] = df["pnl_inr"].apply(lambda x: f"{x:,.0f}")
    return out


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
    c5.metric("Total P&L", f"\u20b9{total_pnl:,.0f}")
    c6.metric("Avg P&L / Trade", f"\u20b9{avg_pnl:,.0f}")

    # Row 2: Exit reason breakdown
    reasons = df["exit_reason"].value_counts()
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("TP exits", int(reasons.get("TP", 0)))
    r2.metric("SL exits", int(reasons.get("SL", 0)))
    r3.metric("EOD exits", int(reasons.get("EOD", 0)))
    r4.metric("Reversal exits", int(reasons.get("REVERSAL", 0)))
    r5.metric("Reversal Stop exits", int(reasons.get("REVERSAL_STOP", 0)))

    # Row 3: CE vs PE breakdown
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

    # Row 4: Pyramid / Reversal / EMA-adjusted counts
    pyramid_trades = df[df["num_lots"] > 1]
    reversal_trades = df[df["is_reversal"] == True]
    ema_adjusted_trades = df[df["ema_adjusted"] == True]
    st.markdown(
        f"**Pyramided:** {len(pyramid_trades)} trades  |  "
        f"**Reversals:** {len(reversal_trades)} trades  |  "
        f"**EMA-adjusted TP:** {len(ema_adjusted_trades)} trades"
    )

    # Row 5: P&L by DTE table
    if "dte" in df.columns:
        st.divider()
        st.subheader("P&L by DTE")
        dte_summary = (
            df.groupby("dte")
            .agg(
                trades=("pnl_inr", "count"),
                wins=("pnl_inr", lambda x: (x > 0).sum()),
                total_pnl=("pnl_inr", "sum"),
                avg_pnl=("pnl_inr", "mean"),
            )
            .reset_index()
        )
        dte_summary["win_rate"] = (
            dte_summary["wins"] / dte_summary["trades"] * 100
        ).round(1)
        dte_summary = dte_summary[["dte", "trades", "wins", "win_rate", "total_pnl", "avg_pnl"]]
        dte_summary.columns = ["DTE", "Trades", "Wins", "Win Rate (%)", "Total P&L", "Avg P&L"]
        st.dataframe(dte_summary, use_container_width=True, hide_index=True)

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

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        filter_type = st.multiselect(
            "Filter by option type", options=["CE", "PE"], key="hanr7_filter_type"
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason",
            options=["TP", "SL", "EOD", "REVERSAL", "REVERSAL_STOP"],
            key="hanr7_filter_reason",
        )
    with fc3:
        filter_date = st.text_input(
            "Filter by date (YYYY-MM-DD)", value="", key="hanr7_filter_date"
        )

    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]
    if filter_date:
        filtered = filtered[filtered["entry_date"] == filter_date]

    # Format for audit-friendly display
    display = _format_trades_for_display(filtered)
    st.dataframe(display, use_container_width=True, hide_index=True)

    st.download_button(
        label=f"Download CSV ({len(filtered)} trades)",
        data=display.to_csv(index=False),
        file_name="ha_nr7_strategy_backtest.csv",
        mime="text/csv",
        key="hanr7_download",
    )
