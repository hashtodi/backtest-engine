"""Streamlit form + runner for the SuperTrend Low-Band backtest."""
import json
import os
from datetime import date
from io import StringIO

import pandas as pd
import streamlit as st

from engine.supertrend_low_band_backtest import (
    run_backtest,
    summarize_trades,
    trades_to_dataframe,
)


DEFAULT_CONFIG_PATH = "saved_strategies/supertrend_low_band.json"


def _load_default_config() -> dict:
    if os.path.exists(DEFAULT_CONFIG_PATH):
        with open(DEFAULT_CONFIG_PATH) as f:
            return json.load(f)
    return {
        "instrument": "NIFTY",
        "supertrend": {"factor": 3, "atr_period": 10},
        "first_5min_window": {"start": "09:15", "end": "09:20"},
        "band_pct": 5.0,
        "dte_table": {
            "0": {"tp_pct": 20.0, "sl_pct": 12.5},
            "1": {"tp_pct": 15.0, "sl_pct": 10.0},
            "2": {"tp_pct": 10.0, "sl_pct": 7.5},
            "3": {"tp_pct":  7.5, "sl_pct": 5.0},
            "4": {"tp_pct":  7.5, "sl_pct": 5.0},
        },
        "trading": {"scan_start": "09:20", "force_exit": "14:45"},
        "lot_size": 1,
        "backtest_start": "2025-01-01",
        "backtest_end":   "2026-04-30",
    }


def render_supertrend_low_band_backtest() -> None:
    st.header("SuperTrend Low-Band — Daily ATM Reversal")
    st.caption(
        "Buys NIFTY weekly ATM CE/PE when continuous SuperTrend(3,10) is "
        "bullish AND its value is within ±5% of the contract's 9:15-9:19 "
        "morning low. TP 10%, SL 7.5%, force-exit 14:45. CE/PE run as "
        "fully independent state machines."
    )

    cfg = _load_default_config()

    st.subheader("Indicator")
    c1, c2 = st.columns(2)
    factor = c1.number_input(
        "SuperTrend factor", min_value=1, max_value=20,
        value=int(cfg["supertrend"]["factor"]), key="stlb_factor",
    )
    atr_period = c2.number_input(
        "SuperTrend atr_period", min_value=2, max_value=50,
        value=int(cfg["supertrend"]["atr_period"]), key="stlb_atr",
    )

    st.subheader("Band")
    band_pct = st.number_input(
        "Band % above morning low (close ≤ low × (1 + pct/100) qualifies)",
        min_value=0.5, max_value=20.0,
        value=float(cfg["band_pct"]), step=0.5, key="stlb_band",
    )

    st.subheader("DTE-based SL / TP (% of entry price)")
    st.caption(
        "DTE = trading days to nearest weekly expiry (0 = expiry day). "
        "DTE ≥ 5 uses the DTE=4 row."
    )
    dte_cfg = cfg.get("dte_table", {})
    dte_table_inputs: dict = {}
    cols = st.columns(5)
    for i, dte in enumerate(["0", "1", "2", "3", "4"]):
        with cols[i]:
            st.markdown(f"**DTE {dte}**")
            row = dte_cfg.get(dte, {"tp_pct": 10.0, "sl_pct": 7.5})
            tp_in = st.number_input(
                "TP %", min_value=0.5, max_value=100.0,
                value=float(row.get("tp_pct", 10.0)), step=0.5,
                key=f"stlb_tp_{dte}",
            )
            sl_in = st.number_input(
                "SL %", min_value=0.5, max_value=50.0,
                value=float(row.get("sl_pct", 7.5)), step=0.5,
                key=f"stlb_sl_{dte}",
            )
            dte_table_inputs[dte] = {"tp_pct": float(tp_in), "sl_pct": float(sl_in)}

    st.subheader("Times (HH:MM)")
    c1, c2, c3, c4 = st.columns(4)
    win_start = c1.text_input(
        "First-5min start", value=cfg["first_5min_window"]["start"], key="stlb_winstart",
    )
    win_end = c2.text_input(
        "First-5min end (excl.)", value=cfg["first_5min_window"]["end"], key="stlb_winend",
    )
    scan_start = c3.text_input(
        "Scan start", value=cfg["trading"]["scan_start"], key="stlb_scan",
    )
    force_exit = c4.text_input(
        "Force exit", value=cfg["trading"]["force_exit"], key="stlb_force",
    )

    st.subheader("Run window")
    c1, c2, c3 = st.columns(3)
    start_date = c1.date_input(
        "Start", value=date.fromisoformat(cfg["backtest_start"]), key="stlb_start",
    )
    end_date = c2.date_input(
        "End", value=date.fromisoformat(cfg["backtest_end"]), key="stlb_end",
    )
    lot_multiplier = c3.number_input(
        "Lots (multiplier)", min_value=1, value=int(cfg.get("lot_size", 1)), key="stlb_lots",
    )

    if not st.button("Run backtest", type="primary", key="stlb_run"):
        return

    run_config = {
        "instrument": "NIFTY",
        "supertrend": {"factor": int(factor), "atr_period": int(atr_period)},
        "first_5min_window": {"start": win_start, "end": win_end},
        "band_pct": float(band_pct),
        "dte_table": dte_table_inputs,
        "trading": {"scan_start": scan_start, "force_exit": force_exit},
        "lot_size": int(lot_multiplier),
        "backtest_start": start_date.isoformat(),
        "backtest_end":   end_date.isoformat(),
    }

    with st.spinner("Running..."):
        trades = run_backtest(run_config)

    summary = summarize_trades(trades)
    st.subheader("Summary")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Trades", summary["total_trades"])
    c2.metric("Win rate", f"{summary['win_rate'] * 100:.1f}%")
    c3.metric("Total points", f"{summary['total_pnl_points']:.2f}")
    c4.metric("Total INR", f"{summary['total_pnl_inr']:,.0f}")
    if summary["by_side"]:
        st.write("By side:", summary["by_side"])

    if not trades:
        st.info("No trades generated in this window.")
        return

    df = trades_to_dataframe(trades)

    st.subheader("Equity curve")
    equity = df["pnl_inr"].cumsum()
    chart_df = pd.DataFrame(
        {"Cumulative P&L (₹)": equity.values},
        index=range(1, len(equity) + 1),
    )
    chart_df.index.name = "Trade #"
    st.line_chart(chart_df)

    st.subheader("Trades")
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        filter_type = st.multiselect(
            "Filter by option type", options=["CE", "PE"], key="stlb_ftype",
        )
    with fc2:
        filter_reason = st.multiselect(
            "Filter by exit reason", options=["TP", "SL", "EOD"], key="stlb_freason",
        )
    with fc3:
        filter_date = st.text_input(
            "Filter by date (YYYY-MM-DD)", value="", key="stlb_fdate",
        )
    filtered = df.copy()
    if filter_type:
        filtered = filtered[filtered["option_type"].isin(filter_type)]
    if filter_reason:
        filtered = filtered[filtered["exit_reason"].isin(filter_reason)]
    if filter_date:
        filtered = filtered[filtered["date"] == filter_date]
    st.dataframe(filtered, use_container_width=True)

    csv_buf = StringIO()
    df.to_csv(csv_buf, index=False)
    st.download_button(
        "Download CSV",
        data=csv_buf.getvalue(),
        file_name=f"supertrend_low_band_{start_date}_{end_date}.csv",
        mime="text/csv",
    )
