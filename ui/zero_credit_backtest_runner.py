"""Streamlit form + runner for the Zero Credit 4-leg backtest."""
import json
import math
import os
from datetime import date

import pandas as pd
import streamlit as st

from engine.zero_credit_backtest import run


DEFAULT_CONFIG_PATH = "saved_strategies/zero_credit.json"
DEFAULT_OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"
DEFAULT_OUTPUT_DIR = "output/zero_credit"


def _load_default_config() -> dict:
    if os.path.exists(DEFAULT_CONFIG_PATH):
        with open(DEFAULT_CONFIG_PATH) as f:
            return json.load(f)
    raise FileNotFoundError(
        f"Missing config file at {DEFAULT_CONFIG_PATH}."
    )


def _fmt_money(val: float) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    return f"Rs {val:,.0f}"


def _fmt_pct(val: float) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    return f"{val * 100:.2f}%"


def render_zero_credit_backtest() -> None:
    st.header("Zero Credit - 4-leg premium-targeted NIFTY")
    st.caption(
        "Daily NIFTY weekly-options strategy. At 09:20, buy 1xCE + 1xPE near "
        "Rs 100 and sell 2xCE + 2xPE near Rs 50 (net premium ~ 0). Exit on "
        "first of: TP (default Rs 800), SL (default Rs 2500), or 15:20 close."
    )

    cfg = _load_default_config()

    col_a, col_b, col_c = st.columns(3)
    start_date = col_a.date_input(
        "Backtest start", value=date.fromisoformat(cfg["backtest_start"]),
        key="zc_start",
    )
    end_date = col_b.date_input(
        "Backtest end", value=date.fromisoformat(cfg["backtest_end"]),
        key="zc_end",
    )
    tp_target = col_c.number_input(
        "TP target (Rs)",
        min_value=100, max_value=20000, step=100,
        value=int(cfg["exit"]["tp_target_inr"]),
        help="Take-profit target in rupees on combined unrealized P&L.",
        key="zc_tp",
    )

    col_d, col_e, col_f = st.columns(3)
    entry_time = col_d.text_input(
        "Entry time (HH:MM)", value=cfg["entry"]["entry_time"],
        key="zc_entry_time",
    )
    time_exit = col_e.text_input(
        "Time exit (HH:MM)", value=cfg["exit"]["time_exit"],
        key="zc_time_exit",
    )
    sl_target = col_f.number_input(
        "SL target (Rs)",
        min_value=0, max_value=100000, step=100,
        value=int(cfg["exit"].get("sl_target_inr") or 0),
        help="Stop-loss in rupees on combined unrealized loss. Set to 0 to disable.",
        key="zc_sl",
    )

    col_cap, _, _ = st.columns(3)
    capital = col_cap.number_input(
        "Reference capital (Rs)",
        min_value=50000, step=50000,
        value=int(cfg["sizing"]["reference_capital"]),
        key="zc_capital",
    )

    col_g, col_h, col_i = st.columns(3)
    buy_target = col_g.number_input(
        "Buy premium target (Rs)",
        min_value=10, max_value=500, step=5,
        value=int(cfg["structure"]["buy_premium_target_inr"]),
        key="zc_buy_target",
    )
    sell_target = col_h.number_input(
        "Sell premium target (Rs)",
        min_value=5, max_value=300, step=5,
        value=int(cfg["structure"]["sell_premium_target_inr"]),
        key="zc_sell_target",
    )
    tolerance = col_i.number_input(
        "Premium match tolerance (Rs)",
        min_value=1, max_value=100, step=1,
        value=int(cfg["structure"]["premium_match_tolerance_inr"]),
        key="zc_tolerance",
    )

    col_j, col_k, _ = st.columns(3)
    buy_lots = col_j.number_input(
        "Buy lots per leg",
        min_value=1, max_value=20, step=1,
        value=int(cfg["structure"]["buy_lots"]),
        key="zc_buy_lots",
    )
    sell_lots = col_k.number_input(
        "Sell lots per leg",
        min_value=1, max_value=20, step=1,
        value=int(cfg["structure"]["sell_lots"]),
        key="zc_sell_lots",
    )

    if st.button("Run backtest", type="primary", key="zc_run_button"):
        run_config = dict(cfg)
        run_config["backtest_start"] = start_date.isoformat()
        run_config["backtest_end"] = end_date.isoformat()
        run_config["entry"] = {"entry_time": entry_time}
        run_config["exit"] = {
            **cfg["exit"],
            "tp_target_inr": int(tp_target),
            "sl_target_inr": int(sl_target) if int(sl_target) > 0 else None,
            "time_exit": time_exit,
        }
        run_config["structure"] = {
            **cfg["structure"],
            "buy_premium_target_inr": int(buy_target),
            "sell_premium_target_inr": int(sell_target),
            "buy_lots": int(buy_lots),
            "sell_lots": int(sell_lots),
            "premium_match_tolerance_inr": int(tolerance),
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
        m1.metric("Trades placed",
                  f"{summary['trades_placed']} / {summary['total_days_processed']}")
        m2.metric("Win rate", _fmt_pct(summary["win_rate"]))
        m3.metric("Total P&L", _fmt_money(summary["total_pnl_inr"]))
        m4.metric("Total return on capital", _fmt_pct(summary["total_return_pct"]))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Max drawdown", _fmt_money(summary["max_drawdown_inr"]),
                  delta=_fmt_pct(-summary["max_drawdown_pct"]),
                  delta_color="inverse")
        m6.metric("Max consec losses", str(summary["max_consecutive_losses"]))
        m7.metric("Best day", _fmt_money(summary["best_trade_inr"]))
        m8.metric("Worst day", _fmt_money(summary["worst_trade_inr"]))

        m9, m10, _, _ = st.columns(4)
        m9.metric("Mean P&L", _fmt_money(summary["mean_pnl_inr"]))
        m10.metric("Median P&L", _fmt_money(summary["median_pnl_inr"]))

        if summary["exit_reason_counts"]:
            st.write("**Exit reasons:**", summary["exit_reason_counts"])
        if summary["skip_reason_counts"]:
            st.write("**Skip reasons:**", summary["skip_reason_counts"])

        equity_df = pd.read_csv(result["equity_csv"])
        if not equity_df.empty:
            st.subheader("Equity curve")
            st.line_chart(equity_df.set_index("date")["equity_inr"])

            st.subheader("Drawdown")
            st.area_chart(equity_df.set_index("date")["drawdown_inr"])

        trades_df = pd.read_csv(result["trades_csv"])
        if not trades_df.empty:
            st.subheader("Trades")
            st.dataframe(trades_df, use_container_width=True)
        else:
            st.info("No trades generated in this window.")

        d1, d2 = st.columns(2)
        with open(result["trades_csv"], "rb") as f:
            d1.download_button(
                "Download trades CSV",
                data=f.read(),
                file_name=os.path.basename(result["trades_csv"]),
                mime="text/csv",
                key="zc_dl_trades",
            )
        with open(result["equity_csv"], "rb") as f:
            d2.download_button(
                "Download equity CSV",
                data=f.read(),
                file_name=os.path.basename(result["equity_csv"]),
                mime="text/csv",
                key="zc_dl_equity",
            )
