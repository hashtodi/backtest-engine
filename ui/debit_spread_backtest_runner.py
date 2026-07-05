"""Streamlit form + runner for the Debit Spread (1-3-2 broken-wing condor) backtest."""
import json
import math
import os
from datetime import date
from io import StringIO

import pandas as pd
import streamlit as st

from engine.debit_spread_backtest import run


DEFAULT_CONFIG_PATH = "saved_strategies/debit_spread.json"
DEFAULT_OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"
DEFAULT_OUTPUT_DIR = "output/debit_spread"


def _load_default_config() -> dict:
    if os.path.exists(DEFAULT_CONFIG_PATH):
        with open(DEFAULT_CONFIG_PATH) as f:
            return json.load(f)
    raise FileNotFoundError(
        f"Missing config file at {DEFAULT_CONFIG_PATH}. "
        "Run the backtest from the saved_strategies folder."
    )


def _fmt_money(val: float) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    return f"₹{val:,.0f}"


def _fmt_pct(val: float) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    return f"{val * 100:.2f}%"


def _fmt_ratio(val: float) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    return f"{val:.2f}"


def render_debit_spread_backtest() -> None:
    st.header("Debit Spread — 1-3-2 Broken-Wing Condor")
    st.caption(
        "Calendar-driven NIFTY weekly options strategy. Two trading days "
        "before each weekly expiry at 11:00 AM, enters a 6-leg combined "
        "CE+PE 1-3-2 ratio structure. Exits at 1.5× net debit (intra-day "
        "1-min check) or 15:25 close on expiry day. No stop loss."
    )

    cfg = _load_default_config()

    col_a, col_b, col_c = st.columns(3)
    start_date = col_a.date_input(
        "Backtest start", value=date.fromisoformat(cfg["backtest_start"]),
        key="debspr_start",
    )
    end_date = col_b.date_input(
        "Backtest end", value=date.fromisoformat(cfg["backtest_end"]),
        key="debspr_end",
    )
    tp_multiple = col_c.number_input(
        "TP multiple of max loss",
        min_value=0.1, max_value=10.0, step=0.1,
        value=float(cfg["exit"]["tp_multiple_of_max_loss"]),
        help="Take-profit target = max(0, net_debit) × this multiple",
        key="debspr_tp_mult",
    )

    col_d, col_e, col_f = st.columns(3)
    entry_time = col_d.text_input(
        "Entry time (HH:MM)", value=cfg["entry"]["entry_time"],
        key="debspr_entry_time",
    )
    days_before = col_e.number_input(
        "Days before expiry",
        min_value=1, max_value=5,
        value=int(cfg["entry"]["days_before_expiry"]),
        key="debspr_days_before",
    )
    capital = col_f.number_input(
        "Reference capital (₹)",
        min_value=100000, step=50000,
        value=int(cfg["sizing"]["reference_capital"]),
        key="debspr_capital",
    )

    if st.button("Run backtest", type="primary", key="debspr_run_button"):
        run_config = dict(cfg)  # shallow copy of top level
        run_config["backtest_start"] = start_date.isoformat()
        run_config["backtest_end"] = end_date.isoformat()
        run_config["exit"] = {**cfg["exit"], "tp_multiple_of_max_loss": float(tp_multiple)}
        run_config["entry"] = {
            "days_before_expiry": int(days_before),
            "entry_time": entry_time,
        }
        run_config["sizing"] = {**cfg["sizing"], "reference_capital": int(capital)}

        with st.spinner("Running backtest..."):
            result = run(
                run_config,
                options_path=DEFAULT_OPTIONS_PATH,
                output_dir=DEFAULT_OUTPUT_DIR,
            )

        summary = result["summary"]

        st.subheader("Summary")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Trades placed", f"{summary['trades_placed']} / {summary['total_weeks_processed']}")
        m2.metric("Win rate", _fmt_pct(summary["win_rate"]))
        m3.metric("Total P&L", _fmt_money(summary["total_pnl_inr"]))
        m4.metric("Total return on capital", _fmt_pct(summary["total_return_pct"]))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Max drawdown", _fmt_money(summary["max_drawdown_inr"]),
                  delta=_fmt_pct(-summary["max_drawdown_pct"]), delta_color="inverse")
        m6.metric("Max consec losses", str(summary["max_consecutive_losses"]))
        m7.metric("Sharpe (weekly, ann.)", _fmt_ratio(summary["sharpe"]))
        m8.metric("Sortino (weekly, ann.)", _fmt_ratio(summary["sortino"]))

        m9, m10, m11, m12 = st.columns(4)
        m9.metric("Mean P&L", _fmt_money(summary["mean_pnl_inr"]))
        m10.metric("Median P&L", _fmt_money(summary["median_pnl_inr"]))
        m11.metric("Best trade", _fmt_money(summary["best_trade_inr"]))
        m12.metric("Worst trade", _fmt_money(summary["worst_trade_inr"]))

        if summary["exit_reason_counts"]:
            st.write("**Exit reasons:**", summary["exit_reason_counts"])
        if summary["skip_reason_counts"]:
            st.write("**Skip reasons:**", summary["skip_reason_counts"])

        # Equity curve + drawdown chart
        equity_df = pd.read_csv(result["equity_csv"])
        if not equity_df.empty:
            st.subheader("Equity curve")
            st.line_chart(equity_df.set_index("date")["equity_inr"])

            st.subheader("Drawdown")
            st.area_chart(equity_df.set_index("date")["drawdown_inr"])

        # Trades table
        trades_df = pd.read_csv(result["trades_csv"])
        if not trades_df.empty:
            st.subheader("Trades")
            st.dataframe(trades_df, use_container_width=True)
        else:
            st.info("No trades generated in this window.")

        # Downloads
        d1, d2 = st.columns(2)
        with open(result["trades_csv"], "rb") as f:
            d1.download_button(
                "Download trades CSV",
                data=f.read(),
                file_name=os.path.basename(result["trades_csv"]),
                mime="text/csv",
                key="debspr_dl_trades",
            )
        with open(result["equity_csv"], "rb") as f:
            d2.download_button(
                "Download equity CSV",
                data=f.read(),
                file_name=os.path.basename(result["equity_csv"]),
                mime="text/csv",
                key="debspr_dl_equity",
            )
