"""Streamlit form + runner for the BB-Pivot credit-spread backtest."""
import json
import math
import os
from datetime import date

import pandas as pd
import streamlit as st

from engine.bb_pivot_spread_backtest import run


DEFAULT_CONFIG_PATH = "saved_strategies/bb_pivot_spread.json"
DEFAULT_OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"
DEFAULT_SPOT_PATH = "data/spot/nifty/NIFTY_1m.parquet"
DEFAULT_OUTPUT_DIR = "output/bb_pivot_spread"


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


def render_bb_pivot_spread_backtest() -> None:
    st.header("BB-Pivot Credit Spread - Bollinger + RSI + pivot mean-reversion")
    st.caption(
        "Signals on 30-min NIFTY spot bars (indicators continuous across days). "
        "Upper-band touch + RSI>60 + ADX<25 + pivot confluence -> BEAR CALL: "
        "sell ATM+2 CE / buy ATM+6 CE. Lower-band touch + RSI<30 + ADX<25 + "
        "pivot confluence -> BULL PUT: sell ATM-2 PE / buy ATM-6 PE. Entries "
        "fill at the 30-min signal bar's own CLOSE minute (ATM + premiums from "
        "that bar); exits are value-based on every 1-min option close (TP when the "
        "spread decays to tp_ratio x credit, SL when it widens to sl_ratio x "
        "credit) and fill at the NEXT 1-min OPEN; EOD square-off at the "
        "deadline. Max 1 open trade, capped per day. On weekly expiry days the "
        "strategy rolls to the next weekly expiry (code 2)."
    )

    cfg = _load_default_config()
    signal_cfg = cfg.get("signal", {}) or {}
    entry_cfg = cfg.get("entry", {}) or {}
    exit_cfg = cfg.get("exit", {}) or {}
    structure_cfg = cfg.get("structure", {}) or {}
    expiry_cfg = cfg.get("expiry", {}) or {}

    expiry_options = ["WEEK", "MONTH"]
    default_expiry_type = str(expiry_cfg.get("expiry_type", "WEEK")).upper()
    default_idx = expiry_options.index(default_expiry_type) \
        if default_expiry_type in expiry_options else 0

    col_a, col_b, col_c = st.columns(3)
    start_date = col_a.date_input(
        "Backtest start", value=date.fromisoformat(cfg["backtest_start"]),
        key="bbp_start",
    )
    end_date = col_b.date_input(
        "Backtest end", value=date.fromisoformat(cfg["backtest_end"]),
        key="bbp_end",
    )
    expiry_type = col_c.selectbox(
        "Expiry type", options=expiry_options, index=default_idx,
        help="WEEK rolls to next weekly (code 2) on expiry days. MONTH = nearest monthly, no roll.",
        key="bbp_expiry_type",
    )

    st.markdown("**Signal (30-min spot bars)**")
    col_d, col_e, col_f = st.columns(3)
    bb_period = col_d.number_input(
        "Bollinger period", min_value=2, max_value=200, step=1,
        value=int(signal_cfg.get("bb_period", 20)),
        key="bbp_bb_period",
    )
    bb_std = col_e.number_input(
        "Bollinger std-dev", min_value=0.5, max_value=5.0, step=0.5,
        value=float(signal_cfg.get("bb_std", 2.0)),
        key="bbp_bb_std",
    )
    rsi_length = col_f.number_input(
        "RSI length", min_value=2, max_value=100, step=1,
        value=int(signal_cfg.get("rsi_length", 14)),
        key="bbp_rsi_len",
    )

    col_g, col_h, col_i = st.columns(3)
    rsi_upper = col_g.number_input(
        "RSI upper threshold (bear call)", min_value=1.0, max_value=100.0, step=1.0,
        value=float(signal_cfg.get("rsi_upper", 60)),
        help="Upper-band signal requires RSI above this.",
        key="bbp_rsi_upper",
    )
    rsi_lower = col_h.number_input(
        "RSI lower threshold (bull put)", min_value=0.0, max_value=99.0, step=1.0,
        value=float(signal_cfg.get("rsi_lower", 30)),
        help="Lower-band signal requires RSI below this.",
        key="bbp_rsi_lower",
    )
    band_tol_pct = col_i.number_input(
        "Band touch tolerance (%)", min_value=0.0, max_value=5.0, step=0.1,
        value=float(signal_cfg.get("band_tol_pct", 0.005)) * 100,
        help="How close (|close - band| / close) the 30-min close must be to a band.",
        key="bbp_band_tol",
    )

    col_adx1, col_adx2 = st.columns(2)
    adx_period = col_adx1.number_input(
        "ADX period", min_value=2, max_value=100, step=1,
        value=int(signal_cfg.get("adx_period", 14)),
        key="bbp_adx_period",
    )
    adx_max = col_adx2.number_input(
        "ADX max - trade only below (0 = off)", min_value=0.0, max_value=100.0, step=1.0,
        value=float(signal_cfg.get("adx_max", 25)),
        help="Entries only when ADX is below this (ranging market). 0 disables the filter.",
        key="bbp_adx_max",
    )

    col_j, col_k, col_l = st.columns(3)
    pivot_tol_pct = col_j.number_input(
        "Pivot tolerance (%)", min_value=0.0, max_value=5.0, step=0.05,
        value=float(signal_cfg.get("pivot_tol_pct", 0.001)) * 100,
        help="Price must be within max(this%, points) of a pivot level.",
        key="bbp_pivot_tol_pct",
    )
    pivot_tol_pts = col_k.number_input(
        "Pivot tolerance (points)", min_value=0.0, max_value=200.0, step=1.0,
        value=float(signal_cfg.get("pivot_tol_pts", 15)),
        key="bbp_pivot_tol_pts",
    )
    lots = col_l.number_input(
        "Lots", min_value=1, step=1,
        value=int(structure_cfg.get("lots", 1)),
        key="bbp_lots",
    )

    st.markdown("**Entry window & structure**")
    col_m, col_n, col_o = st.columns(3)
    window_start = col_m.text_input(
        "Entry window start (HH:MM)",
        value=str(entry_cfg.get("window_start", "09:45")),
        key="bbp_win_start",
    )
    window_end = col_n.text_input(
        "Entry window end (HH:MM)",
        value=str(entry_cfg.get("window_end", "14:00")),
        key="bbp_win_end",
    )
    max_trades = col_o.number_input(
        "Max trades per day", min_value=1, max_value=20, step=1,
        value=int(structure_cfg.get("max_trades_per_day", 3)),
        key="bbp_max_trades",
    )

    col_p, col_q = st.columns(2)
    sell_offset_abs = col_p.number_input(
        "Sell-leg offset (|strikes| from ATM)", min_value=1, max_value=20, step=1,
        value=int(structure_cfg.get("sell_offset_abs", 2)),
        help="2 strikes = 100 pts OTM.",
        key="bbp_sell_off",
    )
    buy_offset_abs = col_q.number_input(
        "Buy-leg offset (|strikes| from ATM)", min_value=1, max_value=20, step=1,
        value=int(structure_cfg.get("buy_offset_abs", 6)),
        help="6 strikes = 300 pts OTM. Must be further OTM than the sell leg.",
        key="bbp_buy_off",
    )

    st.markdown("**Exit**")
    col_r, col_s, col_t = st.columns(3)
    tp_ratio = col_r.number_input(
        "Take-profit ratio (x credit, 0 = off)", min_value=0.0, max_value=1.0, step=0.05,
        value=float(exit_cfg.get("tp_ratio", 0.5)),
        help="Close when spread value decays to this multiple of the entry credit.",
        key="bbp_tp",
    )
    sl_ratio = col_s.number_input(
        "Stop-loss ratio (x credit, 0 = off)", min_value=0.0, max_value=10.0, step=0.05,
        value=float(exit_cfg.get("sl_ratio", 1.5)),
        help="Close when spread value widens to this multiple of the entry credit.",
        key="bbp_sl",
    )
    square_off_time = col_t.text_input(
        "EOD square-off (HH:MM)",
        value=str(exit_cfg.get("square_off_time", "15:15")),
        key="bbp_square_off",
    )

    capital = st.number_input(
        "Reference capital (Rs)", min_value=1, step=10000,
        value=int(cfg["sizing"]["reference_capital"]),
        key="bbp_capital",
    )

    if st.button("Run backtest", type="primary", key="bbp_run_button"):
        if buy_offset_abs <= sell_offset_abs:
            st.error("Buy-leg offset must be greater than the sell-leg offset.")
            return
        run_config = dict(cfg)
        run_config["backtest_start"] = start_date.isoformat()
        run_config["backtest_end"] = end_date.isoformat()
        run_config["signal"] = {
            **signal_cfg,
            "bb_period": int(bb_period),
            "bb_std": float(bb_std),
            "rsi_length": int(rsi_length),
            "rsi_upper": float(rsi_upper),
            "rsi_lower": float(rsi_lower),
            "adx_period": int(adx_period),
            "adx_max": float(adx_max),
            "band_tol_pct": float(band_tol_pct) / 100.0,
            "pivot_tol_pct": float(pivot_tol_pct) / 100.0,
            "pivot_tol_pts": float(pivot_tol_pts),
        }
        run_config["entry"] = {
            **entry_cfg,
            "window_start": str(window_start),
            "window_end": str(window_end),
        }
        run_config["structure"] = {
            **structure_cfg,
            "lots": int(lots),
            "sell_offset_abs": int(sell_offset_abs),
            "buy_offset_abs": int(buy_offset_abs),
            "max_trades_per_day": int(max_trades),
        }
        run_config["exit"] = {
            **exit_cfg,
            "tp_ratio": float(tp_ratio),
            "sl_ratio": float(sl_ratio),
            "square_off_time": str(square_off_time),
        }
        run_config["expiry"] = {
            **expiry_cfg,
            "expiry_type": str(expiry_type).upper(),
            "expiry_roll": str(expiry_type).upper() == "WEEK",
        }
        run_config["sizing"] = {**cfg["sizing"], "reference_capital": int(capital)}

        with st.spinner("Running backtest..."):
            result = run(
                run_config,
                options_path=DEFAULT_OPTIONS_PATH,
                output_dir=DEFAULT_OUTPUT_DIR,
                spot_path=DEFAULT_SPOT_PATH,
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
        m11.metric("Bear-call trades", str(summary["bear_call_trades"]))
        m12.metric("Bull-put trades", str(summary["bull_put_trades"]))

        st.write("**Exit reasons:**", summary["exit_reason_counts"])
        st.write(f"**Expiry-day rolled trades (code 2):** {summary['rolled_trades']}")
        if summary["fill_fallback_count"]:
            st.warning(
                f"{summary['fill_fallback_count']} trade(s) used a fallback "
                "fill (legs missing at the intended minute)."
            )
        if result["signals_skipped"]:
            st.warning(
                f"{result['signals_skipped']} signal(s) skipped because ATM / "
                "spread legs were missing or the net credit was non-positive."
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
                key="bbp_dl_trades",
            )
        with open(result["equity_csv"], "rb") as f:
            d2.download_button(
                "Download equity CSV",
                data=f.read(),
                file_name=os.path.basename(result["equity_csv"]),
                mime="text/csv",
                key="bbp_dl_equity",
            )
