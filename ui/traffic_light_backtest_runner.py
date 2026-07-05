"""Streamlit form + runner for the Traffic Light backtest."""
import json
import os
from datetime import date
from io import StringIO

import streamlit as st

from engine.traffic_light_backtest import (
    run_backtest,
    summarize_trades,
    trades_to_dataframe,
)


DEFAULT_CONFIG_PATH = "saved_strategies/traffic_light.json"


def _load_default_config() -> dict:
    if os.path.exists(DEFAULT_CONFIG_PATH):
        with open(DEFAULT_CONFIG_PATH) as f:
            return json.load(f)
    return {
        "name": "traffic_light",
        "instrument": "NIFTY",
        "params": {
            "rsi_period": 14, "ema_period": 15,
            "rsi_overbought": 70, "rsi_oversold": 30,
            "sl_buffer": 0.0, "rr_ratio": 1.2,
            "premium_budget_inr": 10000, "min_otm_offset": 0, "max_otm_offset": 4,
        },
        "timing": {"scan_start": "09:15", "entry_deadline": "14:44", "force_exit": "14:45"},
        "lot_size": 1,
        "backtest_start": "2025-01-01",
        "backtest_end": "2026-04-13",
    }


def render_traffic_light_backtest() -> None:
    st.header("Traffic Light — Two-Candle Reversal Breakout")
    st.caption(
        "Opposite-color spot candle pair defines pair_high / pair_low. "
        "Close-strict break above pair_high triggers CE; below pair_low triggers PE. "
        "RSI(14) + EMA(15) filter applied at pair formation (CE blocked if RSI overbought "
        "on both pair bars AND close <= EMA; PE symmetric). Both blocked = skip pair. "
        "Premium*lot_size budget caps strike at ATM..ATM±4."
    )

    cfg = _load_default_config()
    p = cfg["params"]
    t = cfg["timing"]

    st.subheader("Strategy params")
    c1, c2, c3, c4 = st.columns(4)
    rsi_period = c1.number_input("RSI period", min_value=2, value=int(p["rsi_period"]),
                                  key="tl_rsi_period")
    ema_period = c2.number_input("EMA period", min_value=2, value=int(p["ema_period"]),
                                  key="tl_ema_period")
    rsi_ob = c3.number_input("RSI overbought", min_value=50.0, max_value=100.0,
                              value=float(p["rsi_overbought"]), key="tl_rsi_ob")
    rsi_os = c4.number_input("RSI oversold", min_value=0.0, max_value=50.0,
                              value=float(p["rsi_oversold"]), key="tl_rsi_os")

    c5, c6, c7 = st.columns(3)
    sl_buffer = c5.number_input("SL buffer (spot pts)", min_value=0.0,
                                 value=float(p["sl_buffer"]), key="tl_sl_buffer")
    rr_ratio = c6.number_input("RR ratio", min_value=0.1,
                                value=float(p["rr_ratio"]), key="tl_rr_ratio")
    premium_budget = c7.number_input("Premium budget (INR)", min_value=0.0,
                                      value=float(p["premium_budget_inr"]),
                                      key="tl_premium_budget")

    st.markdown("**Strike walk (OTM):** engine tries strikes in order from "
                "*Start OTM offset* up to *Max OTM offset*, picking the first "
                "whose `premium × lot_size < budget`. Set start=0 to try ATM "
                "first; set start>0 to skip ATM and go straight to OTM.")
    c8a, c8b = st.columns(2)
    min_offset = c8a.number_input(
        "Start OTM offset", min_value=0, max_value=10,
        value=int(p.get("min_otm_offset", 0)),
        help="0 = ATM, 1 = first OTM, 2 = second OTM, ...",
        key="tl_min_offset",
    )
    max_offset = c8b.number_input(
        "Max OTM offset", min_value=0, max_value=10,
        value=int(p["max_otm_offset"]),
        help="Highest OTM offset to try. Walk stops here; if none fit budget, skip trade.",
        key="tl_max_offset",
    )

    st.subheader("Timing & dates")
    c9, c10, c11 = st.columns(3)
    scan_start = c9.text_input("Scan start (HH:MM)", value=t["scan_start"],
                                key="tl_scan_start")
    entry_deadline = c10.text_input("Entry deadline (HH:MM)", value=t["entry_deadline"],
                                     key="tl_entry_deadline")
    force_exit = c11.text_input("Force exit (HH:MM)", value=t["force_exit"],
                                 key="tl_force_exit")

    c12, c13, c14 = st.columns(3)
    start_date = c12.date_input("Start", value=date.fromisoformat(cfg["backtest_start"]),
                                 key="tl_start_date")
    end_date = c13.date_input("End", value=date.fromisoformat(cfg["backtest_end"]),
                               key="tl_end_date")
    lot_multiplier = c14.number_input("Lots (multiplier)", min_value=1,
                                       value=int(cfg.get("lot_size", 1)),
                                       key="tl_lot_multiplier")

    if not st.button("Run backtest", type="primary", key="tl_run_btn"):
        return

    run_config = {
        "instrument": "NIFTY",
        "params": {
            "rsi_period": int(rsi_period),
            "ema_period": int(ema_period),
            "rsi_overbought": float(rsi_ob),
            "rsi_oversold": float(rsi_os),
            "sl_buffer": float(sl_buffer),
            "rr_ratio": float(rr_ratio),
            "premium_budget_inr": float(premium_budget),
            "min_otm_offset": int(min_offset),
            "max_otm_offset": int(max_offset),
        },
        "timing": {
            "scan_start": scan_start,
            "entry_deadline": entry_deadline,
            "force_exit": force_exit,
        },
        "lot_size": int(lot_multiplier),
        "backtest_start": start_date.isoformat(),
        "backtest_end": end_date.isoformat(),
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
    if summary["by_reason"]:
        st.write("By exit reason:", summary["by_reason"])

    if trades:
        df = trades_to_dataframe(trades)
        st.subheader("Trades")
        st.dataframe(df, use_container_width=True)
        csv_buf = StringIO()
        df.to_csv(csv_buf, index=False)
        st.download_button(
            "Download CSV",
            data=csv_buf.getvalue(),
            file_name=f"traffic_light_{start_date}_{end_date}.csv",
            mime="text/csv",
            key="tl_download_csv",
        )
    else:
        st.info("No trades generated in this window.")
