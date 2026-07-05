"""Streamlit form + runner for the Gamma Blast backtest."""
import json
import os
from datetime import date
from io import StringIO

import streamlit as st

from engine.gamma_blast_backtest import (
    run_backtest,
    summarize_trades,
    trades_to_dataframe,
)


DEFAULT_CONFIG_PATH = "saved_strategies/gamma_blast.json"


def _load_default_config() -> dict:
    if os.path.exists(DEFAULT_CONFIG_PATH):
        with open(DEFAULT_CONFIG_PATH) as f:
            return json.load(f)
    return {
        "instruments": ["SENSEX"],
        "params": {
            "NIFTY":  {"alert_price": None, "entry_price": None, "sl": None, "tp": None},
            "SENSEX": {"alert_price": 20,   "entry_price": 40,   "sl": 15,   "tp": 80},
        },
        "timing": {"arm_start": "10:00", "arm_deadline": "15:00",
                   "entry_deadline": "15:05", "force_exit": "15:15"},
        "lot_size": 1,
        "backtest_start": "2025-01-01",
        "backtest_end":   "2026-04-13",
    }


def _param_inputs(instrument: str, defaults: dict, key_prefix: str) -> dict:
    c1, c2, c3, c4 = st.columns(4)
    alert = c1.number_input(
        f"{instrument} alert_price", min_value=0.0,
        value=float(defaults.get("alert_price") or 0.0),
        key=f"{key_prefix}_alert",
    )
    entry = c2.number_input(
        f"{instrument} entry_price", min_value=0.0,
        value=float(defaults.get("entry_price") or 0.0),
        key=f"{key_prefix}_entry",
    )
    sl = c3.number_input(
        f"{instrument} sl", min_value=0.0,
        value=float(defaults.get("sl") or 0.0),
        key=f"{key_prefix}_sl",
    )
    tp = c4.number_input(
        f"{instrument} tp", min_value=0.0,
        value=float(defaults.get("tp") or 0.0),
        key=f"{key_prefix}_tp",
    )
    def _none_if_zero(x):
        return None if x == 0 else float(x)
    return {
        "alert_price": _none_if_zero(alert),
        "entry_price": _none_if_zero(entry),
        "sl": _none_if_zero(sl),
        "tp": _none_if_zero(tp),
    }


def render_gamma_blast_backtest() -> None:
    st.header("Gamma Blast — Expiry-Day ATM Reversal")
    st.caption(
        "Buys ATM CE/PE after premium is crushed below alert_price then "
        "recovers above entry_price. Fixed-absolute SL and TP. "
        "Runs only on weekly expiry days."
    )

    cfg = _load_default_config()

    col_n, col_s = st.columns(2)
    use_nifty = col_n.checkbox("NIFTY", value="NIFTY" in cfg.get("instruments", []))
    use_sensex = col_s.checkbox("SENSEX", value="SENSEX" in cfg.get("instruments", []))

    if use_nifty:
        st.subheader("NIFTY levels")
        nifty_params = _param_inputs("NIFTY", cfg["params"].get("NIFTY", {}), "nifty")
    else:
        nifty_params = {"alert_price": None, "entry_price": None, "sl": None, "tp": None}

    if use_sensex:
        st.subheader("SENSEX levels")
        sensex_params = _param_inputs("SENSEX", cfg["params"].get("SENSEX", {}), "sensex")
    else:
        sensex_params = {"alert_price": None, "entry_price": None, "sl": None, "tp": None}

    col_a, col_b, col_c = st.columns(3)
    start_date = col_a.date_input("Start", value=date.fromisoformat(cfg["backtest_start"]))
    end_date = col_b.date_input("End", value=date.fromisoformat(cfg["backtest_end"]))
    lot_multiplier = col_c.number_input("Lots (multiplier)", min_value=1, value=int(cfg.get("lot_size", 1)))

    if st.button("Run backtest", type="primary"):
        instruments = [i for i, used in [("NIFTY", use_nifty), ("SENSEX", use_sensex)] if used]
        if not instruments:
            st.error("Select at least one instrument.")
            return

        run_config = {
            "instruments": instruments,
            "params": {"NIFTY": nifty_params, "SENSEX": sensex_params},
            "timing": cfg["timing"],
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
        if summary["by_instrument"]:
            st.write("By instrument:", summary["by_instrument"])

        if trades:
            df = trades_to_dataframe(trades)
            st.subheader("Trades")
            st.dataframe(df, use_container_width=True)
            csv_buf = StringIO()
            df.to_csv(csv_buf, index=False)
            st.download_button(
                "Download CSV",
                data=csv_buf.getvalue(),
                file_name=f"gamma_blast_{start_date}_{end_date}.csv",
                mime="text/csv",
            )
        else:
            st.info("No trades generated in this window.")
