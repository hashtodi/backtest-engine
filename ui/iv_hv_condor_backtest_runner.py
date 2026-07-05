"""Streamlit form + runner for the IV/HV-ratio iron condor (S165) backtest."""
import json
import math
import os
from datetime import date

import pandas as pd
import streamlit as st

from engine.iv_hv_iron_condor_backtest import run, DEFAULT_OPTIONS_PATH, DEFAULT_SPOT_PATH


DEFAULT_CONFIG_PATH = "saved_strategies/iv_hv_iron_condor.json"


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


def render_iv_hv_condor_backtest() -> None:
    st.header("IV/HV-Ratio Iron Condor (S165)")
    st.caption(
        "Sells a NIFTY weekly iron condor when the market is over-pricing vol: "
        "ATM implied vol / 20-day realized vol (HV) crosses the threshold inside "
        "the morning window (first minute that qualifies, one entry/day). Legs are "
        "picked by computed Black-Scholes delta (SELL CE ~0.20 / BUY CE ~0.08 / "
        "SELL PE ~-0.20 / BUY PE ~-0.08). Managed with TP = % of net credit, "
        "SL = multiple of net credit, and a hard exit. Fills at the signal/exit "
        "bar CLOSE. Weekly code-1 (0DTE on expiry day). Delta uses minute-precise "
        "time-to-expiry against the true expiry calendar. Gross P&L only. Days where "
        "the chain is too narrow to place a distinct wing are skipped as un-formable."
    )

    cfg = _load_default_config()
    signal_cfg = cfg.get("signal", {}) or {}
    entry_cfg = cfg.get("entry", {}) or {}
    exit_cfg = cfg.get("exit", {}) or {}
    structure_cfg = cfg.get("structure", {}) or {}
    greeks_cfg = cfg.get("greeks", {}) or {}
    sizing_cfg = cfg.get("sizing", {}) or {}

    col_a, col_b = st.columns(2)
    start_date = col_a.date_input(
        "Backtest start", value=date.fromisoformat(cfg["backtest_start"]),
        key="ivhv_start")
    end_date = col_b.date_input(
        "Backtest end", value=date.fromisoformat(cfg["backtest_end"]),
        key="ivhv_end")

    st.markdown("**Signal (ATM IV / HV_20d)**")
    col_c, col_d = st.columns(2)
    ratio_min = col_c.number_input(
        "IV/HV ratio threshold (>)", min_value=0.5, max_value=5.0, step=0.05,
        value=float(signal_cfg.get("iv_rv_ratio_min", 1.3)),
        help="Enter when ATM implied vol exceeds this multiple of 20-day realized vol.",
        key="ivhv_ratio")
    hv_lookback = col_d.number_input(
        "HV lookback (trading days)", min_value=5, max_value=60, step=1,
        value=int(signal_cfg.get("hv_lookback", 20)),
        help="Realized-vol window (annualized, no look-ahead: uses returns through D-1).",
        key="ivhv_hv_lb")

    st.markdown("**Entry window & exit**")
    col_e, col_f, col_g = st.columns(3)
    window_start = col_e.text_input(
        "Window start (HH:MM)", value=str(entry_cfg.get("window_start", "09:45")),
        key="ivhv_win_start")
    window_end = col_f.text_input(
        "Window end (HH:MM)", value=str(entry_cfg.get("window_end", "11:30")),
        key="ivhv_win_end")
    hard_exit_time = col_g.text_input(
        "Hard exit (HH:MM)", value=str(exit_cfg.get("hard_exit_time", "15:10")),
        key="ivhv_hard_exit")

    col_h, col_i = st.columns(2)
    tp_pct = col_h.number_input(
        "Take profit (fraction of credit)", min_value=0.05, max_value=2.0, step=0.05,
        value=float(exit_cfg.get("tp_pct", 0.50)),
        help="0.50 -> take profit at +50% of the net credit collected.",
        key="ivhv_tp")
    sl_pct = col_i.number_input(
        "Stop loss (multiple of credit)", min_value=0.25, max_value=10.0, step=0.25,
        value=float(exit_cfg.get("sl_pct", 2.00)),
        help="2.00 -> stop at -200% of the net credit.", key="ivhv_sl")

    st.markdown("**Leg deltas (target; nearest available strike is chosen)**")
    col_j, col_k, col_l, col_m = st.columns(4)
    sell_ce_delta = col_j.number_input(
        "SELL CE delta", min_value=0.01, max_value=0.9, step=0.01,
        value=float(structure_cfg.get("sell_ce_delta", 0.20)), key="ivhv_sce")
    buy_ce_delta = col_k.number_input(
        "BUY CE delta", min_value=0.01, max_value=0.9, step=0.01,
        value=float(structure_cfg.get("buy_ce_delta", 0.08)), key="ivhv_bce")
    sell_pe_delta = col_l.number_input(
        "SELL PE delta", min_value=-0.9, max_value=-0.01, step=0.01,
        value=float(structure_cfg.get("sell_pe_delta", -0.20)), key="ivhv_spe")
    buy_pe_delta = col_m.number_input(
        "BUY PE delta", min_value=-0.9, max_value=-0.01, step=0.01,
        value=float(structure_cfg.get("buy_pe_delta", -0.08)), key="ivhv_bpe")

    st.markdown("**Structure, greeks & sizing**")
    col_n, col_o, col_p = st.columns(3)
    min_credit_pts = col_n.number_input(
        "Min net credit (pts, 0 = none)", min_value=0.0, step=1.0,
        value=float(structure_cfg.get("min_credit_pts", 0.0)), key="ivhv_mincr")
    max_trades = col_o.number_input(
        "Max trades per day", min_value=1, max_value=10, step=1,
        value=int(structure_cfg.get("max_trades_per_day", 1)), key="ivhv_maxtr")
    strike_step = col_p.number_input(
        "Strike step", min_value=1, step=1,
        value=int(structure_cfg.get("strike_step", 50)), key="ivhv_step")

    col_q, col_r, col_s, col_t = st.columns(4)
    risk_free_rate = col_q.number_input(
        "Risk-free rate", min_value=0.0, max_value=0.20, step=0.005, format="%.3f",
        value=float(greeks_cfg.get("risk_free_rate", 0.065)),
        help="Used in the Black-Scholes delta.", key="ivhv_rfr")
    dividend_yield = col_r.number_input(
        "Dividend yield (q)", min_value=0.0, max_value=0.10, step=0.005, format="%.3f",
        value=float(greeks_cfg.get("dividend_yield", 0.0)), key="ivhv_q")
    lots = col_s.number_input(
        "Lots", min_value=1, step=1, value=int(sizing_cfg.get("lots", 4)),
        key="ivhv_lots")
    lot_size = col_t.number_input(
        "Lot size", min_value=1, step=1, value=int(sizing_cfg.get("lot_size", 65)),
        key="ivhv_lotsize")

    if st.button("Run backtest", type="primary", key="ivhv_run_button"):
        run_config = dict(cfg)
        run_config["backtest_start"] = start_date.isoformat()
        run_config["backtest_end"] = end_date.isoformat()
        run_config["signal"] = {**signal_cfg,
            "iv_rv_ratio_min": float(ratio_min), "hv_lookback": int(hv_lookback)}
        run_config["entry"] = {**entry_cfg,
            "window_start": str(window_start), "window_end": str(window_end)}
        run_config["exit"] = {**exit_cfg,
            "tp_pct": float(tp_pct), "sl_pct": float(sl_pct),
            "hard_exit_time": str(hard_exit_time)}
        run_config["structure"] = {**structure_cfg,
            "sell_ce_delta": float(sell_ce_delta), "buy_ce_delta": float(buy_ce_delta),
            "sell_pe_delta": float(sell_pe_delta), "buy_pe_delta": float(buy_pe_delta),
            "min_credit_pts": float(min_credit_pts),
            "max_trades_per_day": int(max_trades), "strike_step": int(strike_step)}
        run_config["greeks"] = {**greeks_cfg,
            "risk_free_rate": float(risk_free_rate),
            "dividend_yield": float(dividend_yield)}
        run_config["sizing"] = {**sizing_cfg,
            "lots": int(lots), "lot_size": int(lot_size)}

        with st.spinner("Running backtest... (loading the option chain can take a minute)"):
            result = run(run_config, options_path=DEFAULT_OPTIONS_PATH,
                         spot_path=DEFAULT_SPOT_PATH)

        summary = result["summary"]
        stats = result["stats"]

        st.subheader("Summary — reliable trades only")
        st.caption(
            f"Headline covers the {summary['total_trades']} RELIABLE trades of "
            f"{summary['all_trades']} executed. The other "
            f"{summary['excluded_fallback_trades']} are EXCLUDED because a locked "
            "leg drifted outside the stored ±10-strike window (its exit price is "
            "unknown). Excluded trades skew to large-move days, so this headline is "
            "OPTIMISTIC about calm regimes — it is NOT an all-in result.")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Reliable trades",
                  f"{summary['total_trades']} / {summary['all_trades']}")
        m2.metric("Win rate", _fmt_pct(summary["win_rate"]))
        m3.metric("Total P&L (gross)", _fmt_money(summary["total_pnl_inr"]))
        m4.metric("Max drawdown", _fmt_money(summary["max_drawdown_inr"]))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Mean P&L / trade", _fmt_money(summary["mean_pnl_inr"]))
        m6.metric("Median P&L", _fmt_money(summary["median_pnl_inr"]))
        m7.metric("Best trade", _fmt_money(summary["best_trade_inr"]))
        m8.metric("Worst trade", _fmt_money(summary["worst_trade_inr"]))

        st.error(
            f"⚠️ EXCLUDED (unreliable): {summary['excluded_fallback_trades']} trades "
            "where a leg left the data window. Their freeze-valued P&L is "
            f"{_fmt_money(summary['excluded_fallback_pnl_inr'])} — treat as "
            "indicative only (freeze understates tail losses). These carry most of "
            "the strategy's real risk. See the `fill_fallback` column.")

        st.write("**Exit reasons (reliable):**", summary["exit_reason_counts"])
        st.write(
            f"**Signals:** {stats['signals']}  |  **Skipped** — overlap: "
            f"{stats['skipped_overlap']}, no valid legs: {stats['skipped_no_legs']}, "
            f"un-formable (wing collapsed onto short): {stats['skipped_unformable']}, "
            f"low credit: {stats['skipped_low_credit']}")
        if summary["sanity_flagged"]:
            st.warning(
                f"{summary['sanity_flagged']} trade(s) flagged as likely bad-tick "
                f"fills (|P&L| exceeds the spread width). P&L excluding them: "
                f"{_fmt_money(summary['total_pnl_inr_sanity_filtered'])}. "
                "These are flagged, NOT dropped — inspect the `sanity_flag` column.")

        trades_df = result["trades_df"]
        if not trades_df.empty:
            reliable = trades_df[~trades_df["fill_fallback"]]
            eq = reliable["pnl_inr"].cumsum().reset_index(drop=True)
            st.subheader("Equity curve (reliable trades)")
            st.line_chart(eq)
            st.subheader("Drawdown (reliable trades)")
            st.area_chart(eq - eq.cummax())

            st.subheader("Trades (all executed; fill_fallback = excluded from headline)")
            st.dataframe(trades_df, use_container_width=True)

            with open(result["trades_csv"], "rb") as f:
                st.download_button(
                    "Download trades CSV", data=f.read(),
                    file_name=os.path.basename(result["trades_csv"]),
                    mime="text/csv", key="ivhv_dl_trades")
        else:
            st.info("No trades generated in this window.")
