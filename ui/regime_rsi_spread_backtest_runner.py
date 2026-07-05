"""Streamlit form + runner for the Regime RSI credit-spread backtest."""
import json
import math
import os
from datetime import date

import pandas as pd
import streamlit as st

from engine.regime_rsi_spread_backtest import run


DEFAULT_CONFIG_PATH = "saved_strategies/regime_rsi_spread.json"
DEFAULT_OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"
DEFAULT_OUTPUT_DIR = "output/regime_rsi_spread"


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


def render_regime_rsi_spread_backtest() -> None:
    st.header("Regime RSI Spread - WMA44 regime + RSI(7) crossover credit spreads")
    st.caption(
        "All on 15-min NIFTY spot bars (indicators continuous across days). "
        "RSI(7) crossing ABOVE its SMA(7) -> LONG: sell ATM-2 PE / buy ATM-6 "
        "PE. RSI crossing BELOW -> SHORT: sell ATM+2 CE / buy ATM+6 CE. "
        "Regime filter (close vs WMA44) trades only with the trend; a distance "
        "gate skips entries too far from the WMA. One trade per regime per day "
        "(re-arms on an intraday regime flip; no reversal). Entries fill at "
        "the NEXT 1-min OPEN; SL/TP scan every 1-min spot close and exit at "
        "the NEXT 1-min OPEN; TIME square-off at the deadline."
    )

    cfg = _load_default_config()
    signal_cfg = cfg.get("signal", {}) or {}
    entry_cfg = cfg.get("entry", {}) or {}
    exit_cfg = cfg.get("exit", {}) or {}
    structure_cfg = cfg.get("structure", {}) or {}
    expiry_cfg = cfg.get("expiry", {}) or {}

    expiry_options = ["WEEK", "MONTH"]
    default_expiry_type = str(expiry_cfg.get("expiry_type", "WEEK")).upper()
    default_idx = expiry_options.index(default_expiry_type) if default_expiry_type in expiry_options else 0

    col_a, col_b, col_c = st.columns(3)
    start_date = col_a.date_input(
        "Backtest start", value=date.fromisoformat(cfg["backtest_start"]),
        key="rrs_start",
    )
    end_date = col_b.date_input(
        "Backtest end", value=date.fromisoformat(cfg["backtest_end"]),
        key="rrs_end",
    )
    expiry_type = col_c.selectbox(
        "Expiry type", options=expiry_options, index=default_idx,
        help="Nearest weekly or monthly expiry (expiry_code stays 1).",
        key="rrs_expiry_type",
    )

    col_d, col_e, col_f = st.columns(3)
    rsi_length = col_d.number_input(
        "RSI length", min_value=2, max_value=100, step=1,
        value=int(signal_cfg.get("rsi_length", 7)),
        key="rrs_rsi_len",
    )
    rsi_ma_length = col_e.number_input(
        "RSI-MA length (SMA of RSI)", min_value=1, max_value=100, step=1,
        value=int(signal_cfg.get("rsi_ma_length", 7)),
        key="rrs_rsi_ma_len",
    )
    wma_length = col_f.number_input(
        "WMA length (regime)", min_value=2, max_value=300, step=1,
        value=int(signal_cfg.get("wma_length", 44)),
        key="rrs_wma_len",
    )

    col_g, col_h, col_i = st.columns(3)
    max_dist = col_g.number_input(
        "Max distance from WMA (points)", min_value=0.0, step=1.0,
        value=float(signal_cfg.get("max_dist", 36)),
        help="Skip entries when |spot - WMA| exceeds this.",
        key="rrs_max_dist",
    )
    use_filter = col_h.checkbox(
        "Regime filter (long-only in bull, short-only in bear)",
        value=bool(signal_cfg.get("use_filter", True)),
        key="rrs_use_filter",
    )
    lots = col_i.number_input(
        "Lots", min_value=1, step=1,
        value=int(structure_cfg.get("lots", 1)),
        key="rrs_lots",
    )

    col_j, col_k, col_l = st.columns(3)
    sl_points = col_j.number_input(
        "Stop loss (spot points, 0 = off)", min_value=0.0, step=2.0,
        value=float(exit_cfg.get("sl_points", 36)),
        help="Exit when spot moves this far against the entry spot.",
        key="rrs_sl",
    )
    tp_points = col_k.number_input(
        "Target (spot points, 0 = off)", min_value=0.0, step=2.0,
        value=float(exit_cfg.get("tp_points", 36)),
        help="Exit when spot moves this far in favor of the entry spot.",
        key="rrs_tp",
    )
    capital = col_l.number_input(
        "Reference capital (Rs)", min_value=1, step=10000,
        value=int(cfg["sizing"]["reference_capital"]),
        key="rrs_capital",
    )

    col_m, col_n = st.columns(2)
    sell_offset_abs = col_m.number_input(
        "Sell-leg offset (|strikes| from ATM)", min_value=1, max_value=10, step=1,
        value=int(structure_cfg.get("sell_offset_abs", 2)),
        key="rrs_sell_off",
    )
    buy_offset_abs = col_n.number_input(
        "Buy-leg offset (|strikes| from ATM)", min_value=1, max_value=10, step=1,
        value=int(structure_cfg.get("buy_offset_abs", 6)),
        help="Must be further OTM than the sell leg.",
        key="rrs_buy_off",
    )

    if st.button("Run backtest", type="primary", key="rrs_run_button"):
        if buy_offset_abs <= sell_offset_abs:
            st.error("Buy-leg offset must be greater than the sell-leg offset.")
            return
        run_config = dict(cfg)
        run_config["backtest_start"] = start_date.isoformat()
        run_config["backtest_end"] = end_date.isoformat()
        run_config["signal"] = {
            **signal_cfg,
            "rsi_length": int(rsi_length),
            "rsi_ma_length": int(rsi_ma_length),
            "wma_length": int(wma_length),
            "max_dist": float(max_dist),
            "use_filter": bool(use_filter),
        }
        run_config["entry"] = dict(entry_cfg)
        run_config["exit"] = {
            **exit_cfg,
            "sl_points": float(sl_points),
            "tp_points": float(tp_points),
        }
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
        m1.metric(
            "Trades",
            f"{summary['total_trades']} over {result['days_processed']} days",
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
        m7.metric("Best trade", _fmt_money(summary["best_trade_inr"]))
        m8.metric("Worst trade", _fmt_money(summary["worst_trade_inr"]))

        m9, m10, m11, m12 = st.columns(4)
        m9.metric("Mean P&L", _fmt_money(summary["mean_pnl_inr"]))
        m10.metric("Median P&L", _fmt_money(summary["median_pnl_inr"]))
        m11.metric("LONG trades", str(summary["long_trades"]))
        m12.metric("SHORT trades", str(summary["short_trades"]))

        st.write("**Exit reasons:**", summary["exit_reason_counts"])
        if summary["fill_fallback_count"]:
            st.warning(
                f"{summary['fill_fallback_count']} trade(s) used a fallback "
                "fill (legs missing at the intended minute)."
            )
        if result["signals_skipped"]:
            st.warning(
                f"{result['signals_skipped']} signal(s) skipped because ATM "
                "or spread legs were missing at the entry minute."
            )

        equity_df = pd.read_csv(result["equity_csv"])
        if not equity_df.empty:
            st.subheader("Equity curve")
            st.line_chart(equity_df["equity_inr"])
            st.subheader("Drawdown")
            st.area_chart(equity_df["drawdown_inr"])

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
                key="rrs_dl_trades",
            )
        with open(result["equity_csv"], "rb") as f:
            d2.download_button(
                "Download equity CSV",
                data=f.read(),
                file_name=os.path.basename(result["equity_csv"]),
                mime="text/csv",
                key="rrs_dl_equity",
            )
