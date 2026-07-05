"""Streamlit form + runner for the SuperTrend + PCR + VIX CREDIT-spread backtest."""
import json
import math
import os
from datetime import date

import pandas as pd
import streamlit as st

from engine.st_pcr_vix_credit_spread_backtest import run


DEFAULT_CONFIG_PATH = "saved_strategies/st_pcr_vix_credit_spread.json"
DEFAULT_OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"
DEFAULT_SPOT_PATH = "data/spot/nifty/NIFTY_1m.parquet"
DEFAULT_VIX_PATH = "data/vix/VIX_1m.parquet"
DEFAULT_OUTPUT_DIR = "output/st_pcr_vix_credit_spread"


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


def render_st_pcr_vix_credit_spread_backtest() -> None:
    st.header("SuperTrend + PCR + VIX - Credit Spread")
    st.caption(
        "Credit-spread variant of the major 15-min strategy. TRIGGER is a 15-min "
        "SuperTrend(ATR 7, x3) FLIP; PCR-pre-flip (previous candle's full-chain "
        "put/call OI ratio) and VIX direction are OPTIONAL filters (toggle each). "
        "BULL -> bull-put: SELL ATM-100 PE / BUY ATM-200 PE. "
        "BEAR -> bear-call: SELL ATM+100 CE / BUY ATM+200 CE. Order placed on the "
        "NEXT 15-min candle's OPEN (ATM from that open spot). Exits: TP = 20% of "
        "the entry credit, hard SL = 25 pts, and EOD square-off - NO SuperTrend-"
        "reversal exit. One spread at a time, unlimited sequential re-entries. "
        "Weekly expiry rolls to next weekly (code 2) on expiry days. No brokerage/"
        "costs applied."
    )

    cfg = _load_default_config()
    signal_cfg = cfg.get("signal", {}) or {}
    entry_cfg = cfg.get("entry", {}) or {}
    exit_cfg = cfg.get("exit", {}) or {}
    structure_cfg = cfg.get("structure", {}) or {}
    expiry_cfg = cfg.get("expiry", {}) or {}

    col_a, col_b = st.columns(2)
    start_date = col_a.date_input(
        "Backtest start", value=date.fromisoformat(cfg["backtest_start"]),
        key="spv_start")
    end_date = col_b.date_input(
        "Backtest end", value=date.fromisoformat(cfg["backtest_end"]),
        key="spv_end")

    st.markdown("**SuperTrend (15-min regime flip)**")
    col_c, col_d, col_e = st.columns(3)
    st_factor = col_c.number_input(
        "SuperTrend factor", min_value=0.1, max_value=20.0, step=0.1,
        value=float(signal_cfg.get("st_factor", 3.0)), key="spv_st_factor")
    st_atr_period = col_d.number_input(
        "SuperTrend ATR period", min_value=1, max_value=100, step=1,
        value=int(signal_cfg.get("st_atr_period", 7)), key="spv_st_atr")
    warmup_days = col_e.number_input(
        "Warm-up days", min_value=1, max_value=60, step=1,
        value=int(signal_cfg.get("warmup_days", 10)),
        help="Calendar days loaded before the start to seed the 15m SuperTrend.",
        key="spv_warmup")

    st.markdown("**PCR & VIX filters** (optional confirmations on the flip)")
    col_f, col_g = st.columns(2)
    use_pcr_filter = col_f.checkbox(
        "Use PCR filter", value=bool(signal_cfg.get("use_pcr_filter", True)),
        help="When off, the SuperTrend flip is taken regardless of PCR.",
        key="spv_use_pcr")
    use_vix_filter = col_g.checkbox(
        "Use VIX filter", value=bool(signal_cfg.get("use_vix_filter", True)),
        help="Requires data/vix/VIX_1m.parquet. dVIX over the signal candle.",
        key="spv_use_vix")

    col_h, col_i = st.columns(2)
    pcr_bull_min = col_h.number_input(
        "PCR > (bull)", min_value=0.0, max_value=10.0, step=0.05,
        value=float(signal_cfg.get("pcr_bull_min", 1.1)),
        help="Previous candle's full-chain PCR must exceed this for a BULL entry.",
        key="spv_pcr_bull")
    pcr_bear_max = col_i.number_input(
        "PCR < (bear)", min_value=0.0, max_value=10.0, step=0.05,
        value=float(signal_cfg.get("pcr_bear_max", 0.9)),
        help="Previous candle's full-chain PCR must be below this for a BEAR entry.",
        key="spv_pcr_bear")

    col_j, col_k = st.columns(2)
    dvix_bull_max = col_j.number_input(
        "dVIX <= (bull, flat/falling)", min_value=-5.0, max_value=5.0, step=0.05,
        value=float(signal_cfg.get("dvix_bull_max", 0.3)), key="spv_dvix_bull")
    dvix_bear_min = col_k.number_input(
        "dVIX >= (bear, flat/rising)", min_value=-5.0, max_value=5.0, step=0.05,
        value=float(signal_cfg.get("dvix_bear_min", -0.3)), key="spv_dvix_bear")

    st.markdown("**Entry window & structure**")
    col_k, col_l, col_m = st.columns(3)
    window_start = col_k.text_input(
        "Entry window start (HH:MM)",
        value=str(entry_cfg.get("window_start", "09:46")), key="spv_win_start")
    window_end = col_l.text_input(
        "Entry window end (HH:MM)",
        value=str(entry_cfg.get("window_end", "14:15")), key="spv_win_end")
    lots = col_m.number_input(
        "Lots", min_value=1, step=1,
        value=int(structure_cfg.get("lots", 1)), key="spv_lots")

    col_n, col_o, col_p = st.columns(3)
    sell_offset_abs = col_n.number_input(
        "Sell-leg offset (|strikes| from ATM)", min_value=1, max_value=20, step=1,
        value=int(structure_cfg.get("sell_offset_abs", 2)),
        help="2 strikes = 100 pts OTM (the richer, closer leg you SELL).",
        key="spv_sell_off")
    buy_offset_abs = col_o.number_input(
        "Buy-leg offset (|strikes| from ATM)", min_value=1, max_value=20, step=1,
        value=int(structure_cfg.get("buy_offset_abs", 4)),
        help="4 strikes = 200 pts OTM (the protective wing). 100-pt-wide spread.",
        key="spv_buy_off")
    max_trades = col_p.number_input(
        "Max trades per day (0 = unlimited)", min_value=0, max_value=50, step=1,
        value=int(structure_cfg.get("max_trades_per_day", 0)), key="spv_max_trades")

    max_credit_pts = st.number_input(
        "Max entry credit (pts, 0 = no cap)", min_value=0.0, step=1.0,
        value=float(structure_cfg.get("max_credit_pts", 0.0)), key="spv_max_credit")

    st.markdown("**Exit (TP = 20% of credit, SL = 25 pts, EOD)**")
    col_s, col_t, col_u = st.columns(3)
    tp_credit_frac = col_s.number_input(
        "TP fraction of entry credit", min_value=0.0, max_value=1.0, step=0.05,
        value=float(exit_cfg.get("tp_credit_frac", 0.20)),
        help="Take profit once you have captured this fraction of the credit "
             "(0.20 -> exit when the spread falls to 80% of the entry credit).",
        key="spv_tp_frac")
    sl_pts = col_t.number_input(
        "Stop-loss (spread pts, 0 = off)", min_value=0.0, step=1.0,
        value=float(exit_cfg.get("sl_pts", 25.0)),
        help="Close when the spread loses this many points.", key="spv_sl_pts")
    square_off_time = col_u.text_input(
        "EOD square-off (HH:MM)",
        value=str(exit_cfg.get("square_off_time", "15:15")), key="spv_square_off")

    capital = st.number_input(
        "Reference capital (Rs)", min_value=1, step=10000,
        value=int(cfg["sizing"]["reference_capital"]), key="spv_capital")

    if st.button("Run backtest", type="primary", key="spv_run_button"):
        if buy_offset_abs <= sell_offset_abs:
            st.error("Buy-leg offset must be greater than the sell-leg offset.")
            return
        run_config = dict(cfg)
        run_config["backtest_start"] = start_date.isoformat()
        run_config["backtest_end"] = end_date.isoformat()
        run_config["signal"] = {
            **signal_cfg,
            "st_factor": float(st_factor),
            "st_atr_period": int(st_atr_period),
            "use_pcr_filter": bool(use_pcr_filter),
            "pcr_bull_min": float(pcr_bull_min),
            "pcr_bear_max": float(pcr_bear_max),
            "use_vix_filter": bool(use_vix_filter),
            "dvix_bull_max": float(dvix_bull_max),
            "dvix_bear_min": float(dvix_bear_min),
            "warmup_days": int(warmup_days),
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
            "max_credit_pts": float(max_credit_pts),
        }
        run_config["exit"] = {
            **exit_cfg,
            "tp_credit_frac": float(tp_credit_frac),
            "sl_pts": float(sl_pts),
            "square_off_time": str(square_off_time),
        }
        run_config["expiry"] = {
            **expiry_cfg,
            "expiry_type": "WEEK",
            "expiry_roll": True,
        }
        run_config["sizing"] = {**cfg["sizing"], "reference_capital": int(capital)}

        with st.spinner("Running backtest..."):
            result = run(
                run_config,
                options_path=DEFAULT_OPTIONS_PATH,
                output_dir=DEFAULT_OUTPUT_DIR,
                spot_path=DEFAULT_SPOT_PATH,
                vix_path=DEFAULT_VIX_PATH,
            )

        summary = result["summary"]

        st.subheader("Summary")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Trades",
                  f"{summary['total_trades']} over {result['days_processed']} days")
        m2.metric("Win rate", _fmt_pct(summary["win_rate"]))
        m3.metric("Total P&L", _fmt_money(summary["total_pnl_inr"]))
        m4.metric("Total return on capital", _fmt_pct(summary["total_return_pct"]))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Max drawdown", _fmt_money(summary["max_drawdown_inr"]),
                  delta=_fmt_pct(-summary["max_drawdown_pct"]),
                  delta_color="inverse")
        m6.metric("Max consec losses", str(summary["max_consecutive_losses"]))
        m7.metric("Best trade", _fmt_money(summary["best_trade_inr"]))
        m8.metric("Worst trade", _fmt_money(summary["worst_trade_inr"]))

        m9, m10, m11, m12 = st.columns(4)
        m9.metric("Mean P&L", _fmt_money(summary["mean_pnl_inr"]))
        m10.metric("Median P&L", _fmt_money(summary["median_pnl_inr"]))
        m11.metric("LONG (bull-put)", str(summary["long_trades"]))
        m12.metric("SHORT (bear-call)", str(summary["short_trades"]))

        st.write("**Exit reasons:**", summary["exit_reason_counts"])
        st.write(f"**Expiry-day rolled trades (code 2):** {summary['rolled_trades']}")
        if summary["fill_fallback_count"]:
            st.warning(
                f"{summary['fill_fallback_count']} trade(s) used a fallback exit "
                "fill (legs missing at the intended minute).")
        if result["signals_skipped"]:
            sk = result["skip_reason_counts"]
            st.warning(
                f"{result['signals_skipped']} signal(s) skipped — "
                f"buy leg not listed: {sk['buy_leg_missing']}, "
                f"sell leg not listed: {sk['sell_leg_missing']}, "
                f"spot missing: {sk['spot_missing']}, "
                f"credit out of band: {sk['credit_out_of_band']}.")

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
                "Download trades CSV", data=f.read(),
                file_name=os.path.basename(result["trades_csv"]),
                mime="text/csv", key="spv_dl_trades")
        with open(result["equity_csv"], "rb") as f:
            d2.download_button(
                "Download equity CSV", data=f.read(),
                file_name=os.path.basename(result["equity_csv"]),
                mime="text/csv", key="spv_dl_equity")
