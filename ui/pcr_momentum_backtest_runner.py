"""Streamlit form + runner for the PCR Momentum credit-spread backtest."""
import json
import math
import os
from datetime import date

import pandas as pd
import streamlit as st

from engine.pcr_momentum_backtest import run


DEFAULT_CONFIG_PATH = "saved_strategies/pcr_momentum.json"
DEFAULT_OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"
DEFAULT_OUTPUT_DIR = "output/pcr_momentum"


def _load_default_config() -> dict:
    if os.path.exists(DEFAULT_CONFIG_PATH):
        with open(DEFAULT_CONFIG_PATH) as f:
            return json.load(f)
    raise FileNotFoundError(f"Missing config file at {DEFAULT_CONFIG_PATH}.")


def _fmt_money(val: float) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    return f"Rs {val:,.0f}"


def _fmt_pct(val: float) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    return f"{val * 100:.2f}%"


def render_pcr_momentum_backtest() -> None:
    st.header("PCR Momentum - NIFTY credit spread")
    st.caption(
        "At pcr_first_time (default 09:20) and pcr_second_time (default 10:00) IST, "
        "compute PCR = ΣPE OI / ΣCE OI across the 10 OTM strikes each side of ATM. "
        "If pcr_first > threshold (1.0) AND (pcr_second - pcr_first) >= min_pcr_delta, "
        "sell a PE credit spread (bull put). If pcr_first < threshold AND "
        "(pcr_first - pcr_second) >= min_pcr_delta, sell a CE credit spread (bear call). "
        "Otherwise skip. Spread is filled at the SIGNAL bar's next minute OPEN (T+1; "
        "ATM and leg strikes are resolved on the signal bar). Between fill and "
        "force-exit, scan each minute close for SL / TP; priority SL > TP. "
        "Signal at T, fill at T+1 OPEN. Force exit at 15:00 close (TIME)."
    )

    cfg = _load_default_config()
    expiry_cfg = cfg.get("expiry", {}) or {}
    entry_cfg = cfg.get("entry", {}) or {}
    exit_cfg = cfg.get("exit", {}) or {}
    structure_cfg = cfg.get("structure", {}) or {}

    default_expiry_type = str(expiry_cfg.get("expiry_type", "WEEK")).upper()
    expiry_options = ["WEEK", "MONTH"]
    default_idx = expiry_options.index(default_expiry_type) if default_expiry_type in expiry_options else 0
    default_tp = float(exit_cfg.get("tp_inr", 1200.0))
    default_sl = float(exit_cfg.get("sl_inr", 2000.0))
    default_lots = int(structure_cfg.get("lots", 4))

    def _hhmm(s: str) -> str:
        return str(s)[:5]

    first_time_options = ["09:16", "09:20", "09:25", "09:30", "09:35", "09:40", "09:45"]
    default_first = _hhmm(entry_cfg.get("pcr_first_time", "09:20"))
    if default_first not in first_time_options:
        first_time_options = sorted(set(first_time_options) | {default_first})
    first_idx = first_time_options.index(default_first)

    second_time_options = ["09:45", "09:50", "09:55", "10:00", "10:05", "10:10", "10:15", "10:30"]
    default_second = _hhmm(entry_cfg.get("pcr_second_time", "10:00"))
    if default_second not in second_time_options:
        second_time_options = sorted(set(second_time_options) | {default_second})
    second_idx = second_time_options.index(default_second)

    force_exit_options = ["14:00", "14:15", "14:30", "14:45", "15:00", "15:15", "15:25"]
    default_force = _hhmm(exit_cfg.get("force_exit_time", "15:00"))
    if default_force not in force_exit_options:
        force_exit_options = sorted(set(force_exit_options) | {default_force})
    force_idx = force_exit_options.index(default_force)

    col_a, col_b, col_c = st.columns(3)
    start_date = col_a.date_input(
        "Backtest start", value=date.fromisoformat(cfg["backtest_start"]),
        key="pcrm_start",
    )
    end_date = col_b.date_input(
        "Backtest end", value=date.fromisoformat(cfg["backtest_end"]),
        key="pcrm_end",
    )
    expiry_type = col_c.selectbox(
        "Expiry type",
        options=expiry_options,
        index=default_idx,
        help="Nearest weekly or monthly expiry (expiry_code stays 1).",
        key="pcrm_expiry_type",
    )

    col_d, col_e, col_lots = st.columns(3)
    capital = col_d.number_input(
        "Reference capital (Rs)",
        min_value=1, step=10000,
        value=int(cfg["sizing"]["reference_capital"]),
        key="pcrm_capital",
    )
    lots = col_e.number_input(
        "Lots",
        min_value=1, step=1,
        value=default_lots,
        help="Number of lots per spread. P&L scales linearly; TP/SL thresholds are per-lot.",
        key="pcrm_lots",
    )
    pcr_strikes = col_lots.number_input(
        "PCR strikes each side",
        min_value=1, max_value=10, step=1,
        value=int(entry_cfg.get("pcr_strikes_each_side", 10)),
        help="Number of OTM strikes each side included in the PCR sum.",
        key="pcrm_pcr_strikes",
    )

    col_th, col_dl = st.columns(2)
    pcr_threshold = col_th.number_input(
        "PCR threshold (side cut-off)",
        min_value=0.0, step=0.1,
        value=float(entry_cfg.get("pcr_threshold", 1.0)),
        format="%.2f",
        help="pcr_first > threshold -> PE side; pcr_first < threshold -> CE side; equal -> skip.",
        key="pcrm_threshold",
    )
    min_pcr_delta = col_dl.number_input(
        "Min PCR delta",
        min_value=0.0, step=0.05,
        value=float(entry_cfg.get("min_pcr_delta", 0.1)),
        format="%.3f",
        help="Magnitude PCR must move between snapshots, in the correct direction. 0 = direction-only filter.",
        key="pcrm_min_delta",
    )

    col_so, col_bo = st.columns(2)
    sell_offset_abs = col_so.number_input(
        "Sell-leg offset (|strikes| from ATM)",
        min_value=1, max_value=10, step=1,
        value=int(structure_cfg.get("sell_offset_abs", 2)),
        help="Short (sell) leg's |strike_offset| from ATM. CE side sells ATM+N CE; "
             "PE side sells ATM-N PE. Must be smaller than the buy-leg offset.",
        key="pcrm_sell_off",
    )
    buy_offset_abs = col_bo.number_input(
        "Buy-leg offset (|strikes| from ATM)",
        min_value=1, max_value=10, step=1,
        value=int(structure_cfg.get("buy_offset_abs", 6)),
        help="Long (buy) leg's |strike_offset| from ATM (further OTM than the sell leg).",
        key="pcrm_buy_off",
    )

    col_ft, col_st, col_fe = st.columns(3)
    pcr_first_time = col_ft.selectbox(
        "PCR first snapshot",
        options=first_time_options,
        index=first_idx,
        key="pcrm_first_time",
    )
    pcr_second_time = col_st.selectbox(
        "PCR second snapshot (signal bar)",
        options=second_time_options,
        index=second_idx,
        help="Signal evaluated here; spread fills at this bar's next minute OPEN.",
        key="pcrm_second_time",
    )
    force_exit_time = col_fe.selectbox(
        "Force exit time",
        options=force_exit_options,
        index=force_idx,
        help="Forced TIME exit at this minute close if no TP/SL hit.",
        key="pcrm_force_exit",
    )

    col_f, col_g = st.columns(2)
    tp_inr = col_f.number_input(
        "Take profit (Rs / lot, 0 = off)",
        min_value=0.0, step=100.0,
        value=default_tp,
        help="Threshold = this value * lots. Exit when live spread P&L >= threshold.",
        key="pcrm_tp",
    )
    sl_inr = col_g.number_input(
        "Stop loss (Rs / lot, 0 = off)",
        min_value=0.0, step=100.0,
        value=default_sl,
        help="Threshold = this value * lots. Exit when live spread P&L <= -threshold.",
        key="pcrm_sl",
    )

    if st.button("Run backtest", type="primary", key="pcrm_run_button"):
        run_config = dict(cfg)
        run_config["backtest_start"] = start_date.isoformat()
        run_config["backtest_end"] = end_date.isoformat()
        run_config["entry"] = {
            **entry_cfg,
            "pcr_first_time": f"{pcr_first_time}:00",
            "pcr_second_time": f"{pcr_second_time}:00",
            "pcr_threshold": float(pcr_threshold),
            "min_pcr_delta": float(min_pcr_delta),
            "pcr_strikes_each_side": int(pcr_strikes),
        }
        run_config["sizing"] = {**cfg["sizing"], "reference_capital": int(capital)}
        run_config["structure"] = {
            **structure_cfg,
            "lots": int(lots),
            "sell_offset_abs": int(sell_offset_abs),
            "buy_offset_abs": int(buy_offset_abs),
        }
        run_config["expiry"] = {
            **expiry_cfg,
            "expiry_type": str(expiry_type).upper(),
            "expiry_code": 1,
        }
        run_config["exit"] = {
            **exit_cfg,
            "tp_inr": float(tp_inr),
            "sl_inr": float(sl_inr),
            "force_exit_time": f"{force_exit_time}:00",
        }

        with st.spinner("Running backtest..."):
            result = run(
                run_config,
                options_path=DEFAULT_OPTIONS_PATH,
                output_dir=DEFAULT_OUTPUT_DIR,
            )

        summary = result["summary"]

        st.subheader("Summary")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(
            "Trades placed",
            f"{summary['trades_placed']} / {summary['total_days_processed']}",
        )
        m2.metric("Win rate", _fmt_pct(summary["win_rate"]))
        m3.metric("Total P&L", _fmt_money(summary["total_pnl_inr"]))
        m4.metric("Total return on capital", _fmt_pct(summary["total_return_pct"]))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric(
            "Max drawdown", _fmt_money(summary["max_drawdown_inr"]),
            delta=_fmt_pct(-summary["max_drawdown_pct"]),
            delta_color="inverse",
        )
        m6.metric("Max consec losses", str(summary["max_consecutive_losses"]))
        m7.metric("Best day", _fmt_money(summary["best_trade_inr"]))
        m8.metric("Worst day", _fmt_money(summary["worst_trade_inr"]))

        m9, m10, m11, m12 = st.columns(4)
        m9.metric("Mean P&L", _fmt_money(summary["mean_pnl_inr"]))
        m10.metric("Median P&L", _fmt_money(summary["median_pnl_inr"]))
        m11.metric("CE trades", str(summary["ce_trades"]))
        m12.metric("PE trades", str(summary["pe_trades"]))

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
                key="pcrm_dl_trades",
            )
        with open(result["equity_csv"], "rb") as f:
            d2.download_button(
                "Download equity CSV",
                data=f.read(),
                file_name=os.path.basename(result["equity_csv"]),
                mime="text/csv",
                key="pcrm_dl_equity",
            )
