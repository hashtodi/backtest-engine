"""Streamlit form + runner for the Zayn SMC backtest."""
import json
import math
import os
from datetime import date

import pandas as pd
import streamlit as st

from engine.zayn_smc_backtest import run


DEFAULT_CONFIG_PATH = "saved_strategies/zayn_smc.json"
DEFAULT_OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"
DEFAULT_SPOT_PATH = "data/spot/nifty/NIFTY_1m.parquet"
DEFAULT_OUTPUT_DIR = "output/zayn_smc"


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


def render_zayn_smc_backtest() -> None:
    st.header("Zayn SMC - NIFTY 5-min indicator credit spread")
    st.caption(
        "5-min NIFTY 50 spot is fed through the Zayn SMC indicator "
        "(HTF bias, opening range, prior-day H/L, swing pivots, ATR "
        "displacement, liquidity sweeps, breaker arming with retest "
        "window). Bullish entry signal -> sell PE credit spread "
        "(ATM-N PE sell, ATM-M PE buy). Bearish -> sell CE credit spread. "
        "Entry at the next 1-min OPEN after the 5-min signal close. "
        "Multiple trades per day. Exits: SL/TP on 1-min option closes "
        "(detect at T close, fill at T+1 open); OPPOSITE indicator signal "
        "on a 5-min close (close current spread AND auto-flip into a "
        "fresh spread at the next 1-min open); TIME at force-exit (default "
        "15:20). Priority within a minute close: SL > TP."
    )

    cfg = _load_default_config()
    expiry_cfg = cfg.get("expiry", {}) or {}
    entry_cfg = cfg.get("entry", {}) or {}
    exit_cfg = cfg.get("exit", {}) or {}
    structure_cfg = cfg.get("structure", {}) or {}
    ind_cfg = cfg.get("indicator", {}) or {}

    default_expiry_type = str(expiry_cfg.get("expiry_type", "WEEK")).upper()
    expiry_options = ["WEEK", "MONTH"]
    default_idx = expiry_options.index(default_expiry_type) if default_expiry_type in expiry_options else 0
    default_tp = float(exit_cfg.get("tp_inr", 1200.0))
    default_sl = float(exit_cfg.get("sl_inr", 2000.0))
    default_lots = int(structure_cfg.get("lots", 4))

    col_a, col_b, col_c = st.columns(3)
    start_date = col_a.date_input(
        "Backtest start", value=date.fromisoformat(cfg["backtest_start"]),
        key="zsmc_start",
    )
    end_date = col_b.date_input(
        "Backtest end", value=date.fromisoformat(cfg["backtest_end"]),
        key="zsmc_end",
    )
    expiry_type = col_c.selectbox(
        "Expiry type",
        options=expiry_options,
        index=default_idx,
        help="Nearest weekly or monthly expiry (expiry_code stays 1).",
        key="zsmc_expiry_type",
    )

    col_d, col_e, col_f = st.columns(3)
    capital = col_d.number_input(
        "Reference capital (Rs)",
        min_value=1, step=10000,
        value=int(cfg["sizing"]["reference_capital"]),
        key="zsmc_capital",
    )
    lots = col_e.number_input(
        "Lots",
        min_value=1, step=1,
        value=default_lots,
        help="Number of lots per spread. P&L scales linearly; TP/SL thresholds are per-lot.",
        key="zsmc_lots",
    )
    force_exit = col_f.text_input(
        "Force-exit time (HH:MM)",
        value=str(exit_cfg.get("force_exit_time", "15:20"))[:5],
        key="zsmc_force",
    )

    col_so, col_bo = st.columns(2)
    sell_offset_abs = col_so.number_input(
        "Sell-leg offset (|strikes| from ATM)",
        min_value=1, max_value=10, step=1,
        value=int(structure_cfg.get("sell_offset_abs", 2)),
        key="zsmc_sell_off",
    )
    buy_offset_abs = col_bo.number_input(
        "Buy-leg offset (|strikes| from ATM)",
        min_value=1, max_value=10, step=1,
        value=int(structure_cfg.get("buy_offset_abs", 6)),
        key="zsmc_buy_off",
    )

    col_tp, col_sl = st.columns(2)
    tp_inr = col_tp.number_input(
        "Take profit (Rs / lot, 0 = off)",
        min_value=0.0, step=100.0, value=default_tp,
        key="zsmc_tp",
    )
    sl_inr = col_sl.number_input(
        "Stop loss (Rs / lot, 0 = off)",
        min_value=0.0, step=100.0, value=default_sl,
        key="zsmc_sl",
    )

    with st.expander("Indicator parameters (advanced)", expanded=False):
        signal_mode_options = ["sweep", "breaker"]
        default_mode = str(ind_cfg.get("signal_mode", "sweep"))
        if default_mode not in signal_mode_options:
            default_mode = "sweep"
        signal_mode = st.selectbox(
            "Signal mode",
            options=signal_mode_options,
            index=signal_mode_options.index(default_mode),
            help="'sweep': enter on the next 1-min OPEN after a liquidity-sweep "
                 "bar (the green/red triangles on the chart). 'breaker': "
                 "Pine-faithful mode requiring sweep + displacement + breaker retest.",
            key="zsmc_signal_mode",
        )

        c1, c2, c3 = st.columns(3)
        bias_tf_min = c1.number_input(
            "Bias TF (min)", min_value=5, max_value=240, step=5,
            value=int(ind_cfg.get("bias_tf_min", 60)), key="zsmc_bias_tf",
        )
        bias_len = c2.number_input(
            "Bias EMA length", min_value=2, max_value=200, step=1,
            value=int(ind_cfg.get("bias_len", 50)), key="zsmc_bias_len",
        )
        use_bias = c3.checkbox(
            "Use bias filter", value=bool(ind_cfg.get("use_bias", False)),
            help="Optional directional filter. In sweep mode, only allows "
                 "long sweeps when HTF bias is up, short when down.",
            key="zsmc_use_bias",
        )

        c4, c5, c6 = st.columns(3)
        or_minutes = c4.number_input(
            "Opening-range minutes", min_value=5, max_value=60, step=5,
            value=int(ind_cfg.get("or_minutes", 15)), key="zsmc_or_min",
        )
        use_pdhl = c5.checkbox(
            "Use prior-day H/L", value=bool(ind_cfg.get("use_pdhl", True)),
            key="zsmc_use_pdhl",
        )
        use_orhl = c6.checkbox(
            "Use opening-range H/L", value=bool(ind_cfg.get("use_orhl", True)),
            key="zsmc_use_orhl",
        )

        c7, c8, c9 = st.columns(3)
        swing_lb = c7.number_input(
            "Swing lookback", min_value=2, max_value=50, step=1,
            value=int(ind_cfg.get("swing_lb", 10)), key="zsmc_swing_lb",
        )
        atr_len = c8.number_input(
            "ATR length", min_value=2, max_value=50, step=1,
            value=int(ind_cfg.get("atr_len", 14)), key="zsmc_atr_len",
        )
        retest_bars = c9.number_input(
            "Retest validity (bars)", min_value=1, max_value=50, step=1,
            value=int(ind_cfg.get("retest_bars", 10)), key="zsmc_retest",
        )

        c10, c11, c12 = st.columns(3)
        disp_mult = c10.number_input(
            "Displacement × ATR", min_value=0.1, max_value=5.0, step=0.1,
            value=float(ind_cfg.get("disp_mult", 1.5)), format="%.2f",
            key="zsmc_disp_mult",
        )
        body_pct = c11.number_input(
            "Min body / range", min_value=0.1, max_value=1.0, step=0.05,
            value=float(ind_cfg.get("body_pct", 0.5)), format="%.2f",
            key="zsmc_body_pct",
        )
        sweep_buf = c12.number_input(
            "Sweep buffer (pts)", min_value=0.0, step=0.5,
            value=float(ind_cfg.get("sweep_buf", 0.0)), format="%.2f",
            key="zsmc_sweep_buf",
        )

    if st.button("Run backtest", type="primary", key="zsmc_run_button"):
        run_config = dict(cfg)
        run_config["backtest_start"] = start_date.isoformat()
        run_config["backtest_end"] = end_date.isoformat()
        run_config["entry"] = {**entry_cfg}
        run_config["indicator"] = {
            **ind_cfg,
            "signal_mode": str(signal_mode),
            "bias_tf_min": int(bias_tf_min),
            "bias_len": int(bias_len),
            "use_bias": bool(use_bias),
            "or_minutes": int(or_minutes),
            "use_pdhl": bool(use_pdhl),
            "use_orhl": bool(use_orhl),
            "swing_lb": int(swing_lb),
            "atr_len": int(atr_len),
            "disp_mult": float(disp_mult),
            "body_pct": float(body_pct),
            "sweep_buf": float(sweep_buf),
            "retest_bars": int(retest_bars),
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
            "force_exit_time": f"{force_exit}:00",
        }

        with st.spinner("Running backtest..."):
            result = run(
                run_config,
                options_path=DEFAULT_OPTIONS_PATH,
                spot_path=DEFAULT_SPOT_PATH,
                output_dir=DEFAULT_OUTPUT_DIR,
            )

        summary = result["summary"]

        st.subheader("Summary")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Trades", str(summary["trades_placed"]))
        m2.metric("Days with trades", str(summary["days_with_trades"]))
        m3.metric("Win rate", _fmt_pct(summary["win_rate"]))
        m4.metric("Total P&L", _fmt_money(summary["total_pnl_inr"]))

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
        m11.metric("CE trades", str(summary["ce_trades"]))
        m12.metric("PE trades", str(summary["pe_trades"]))

        if summary["exit_reason_counts"]:
            st.write("**Exit reasons:**", summary["exit_reason_counts"])

        equity_df = pd.read_csv(result["equity_csv"])
        if not equity_df.empty:
            st.subheader("Equity curve (per trade)")
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
                key="zsmc_dl_trades",
            )
        with open(result["equity_csv"], "rb") as f:
            d2.download_button(
                "Download equity CSV",
                data=f.read(),
                file_name=os.path.basename(result["equity_csv"]),
                mime="text/csv",
                key="zsmc_dl_equity",
            )
