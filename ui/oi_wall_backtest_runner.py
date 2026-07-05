"""Streamlit form + runner for the OI Wall monthly debit-spread backtest."""
import json
import math
import os
from datetime import date

import pandas as pd
import streamlit as st

from engine.oi_wall_backtest import run


DEFAULT_CONFIG_PATH = "saved_strategies/oi_wall.json"
DEFAULT_OPTIONS_PATH = "data/options/nifty/NIFTY_OPTIONS_1m.parquet"
DEFAULT_OUTPUT_DIR = "output/oi_wall"


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


def render_oi_wall_backtest() -> None:
    st.header("OI Wall - NIFTY credit spread")
    st.caption(
        "At 10:00 IST scan 20 OTM contracts (CE +1..+10, PE -1..-10) on the "
        "nearest weekly or monthly expiry, pick the single highest-OI strike "
        "(the WALL). At the chosen SIGNAL time, if at least "
        "`min conditions to enter` of [price <= 10:00, OI >= 10:00] hold, "
        "fill an N-lot credit spread on the wall side at the next minute "
        "close (T+1; ATM and leg prices come from the fill bar, not the "
        "signal bar). Between fill and force-exit time, scan each minute "
        "close for three signals: BREACH (spot crosses wall), SL (live P&L "
        "<= -sl_per_lot * lots), TP (live P&L >= tp_per_lot * lots). "
        "Priority BREACH > SL > TP within the same close; signal at T, fill "
        "at T+1 close. If no signal hits, exit both legs at force-exit-time "
        "close (TIME)."
    )

    cfg = _load_default_config()
    expiry_cfg = cfg.get("expiry", {}) or {}
    exit_cfg = cfg.get("exit", {}) or {}
    structure_cfg = cfg.get("structure", {}) or {}
    default_expiry_type = str(expiry_cfg.get("expiry_type", "MONTH")).upper()
    expiry_options = ["WEEK", "MONTH"]
    default_idx = expiry_options.index(default_expiry_type) if default_expiry_type in expiry_options else 1
    default_minc = int(cfg["entry"].get("min_conditions_to_enter", 1))
    default_minc = max(1, min(default_minc, 2))
    default_tp = float(exit_cfg.get("tp_inr", 1000.0))
    default_sl = float(exit_cfg.get("sl_inr", 2000.0))
    default_lots = int(structure_cfg.get("lots", 4))

    entry_time_options = ["10:05", "10:10", "10:15", "10:20", "10:25",
                          "10:30", "10:35", "10:40", "10:45", "11:00"]
    default_entry = str(cfg["entry"].get("entry_time", "10:30"))[:5]
    if default_entry not in entry_time_options:
        entry_time_options = sorted(set(entry_time_options) | {default_entry})
    entry_idx = entry_time_options.index(default_entry)

    force_exit_options = ["14:00", "14:15", "14:30", "14:45",
                          "15:00", "15:15", "15:25"]
    default_force = str(exit_cfg.get("force_exit_time", "15:00"))[:5]
    if default_force not in force_exit_options:
        force_exit_options = sorted(set(force_exit_options) | {default_force})
    force_idx = force_exit_options.index(default_force)

    col_a, col_b, col_c = st.columns(3)
    start_date = col_a.date_input(
        "Backtest start", value=date.fromisoformat(cfg["backtest_start"]),
        key="oiw_start",
    )
    end_date = col_b.date_input(
        "Backtest end", value=date.fromisoformat(cfg["backtest_end"]),
        key="oiw_end",
    )
    expiry_type = col_c.selectbox(
        "Expiry type",
        options=expiry_options,
        index=default_idx,
        help="Nearest weekly or monthly expiry (expiry_code stays 1).",
        key="oiw_expiry_type",
    )

    col_d, col_e, col_lots = st.columns(3)
    min_conds = col_d.number_input(
        "Min conditions to enter (of 2)",
        min_value=1, max_value=2, step=1,
        value=default_minc,
        help="Conditions: C1 price(10:30) <= price(10:00); C2 OI(10:30) >= OI(10:00).",
        key="oiw_minc",
    )
    capital = col_e.number_input(
        "Reference capital (Rs)",
        min_value=1, step=10000,
        value=int(cfg["sizing"]["reference_capital"]),
        key="oiw_capital",
    )
    lots = col_lots.number_input(
        "Lots",
        min_value=1, step=1,
        value=default_lots,
        help="Number of lots per spread. P&L scales linearly; TP/SL thresholds are per-lot.",
        key="oiw_lots",
    )

    col_so, col_bo = st.columns(2)
    sell_offset_abs = col_so.number_input(
        "Sell-leg offset (|strikes| from ATM)",
        min_value=1, max_value=10, step=1,
        value=int(structure_cfg.get("sell_offset_abs", 2)),
        help="Short (sell) leg's |strike_offset| from ATM. CE wall sells ATM+N CE; "
             "PE wall sells ATM-N PE. Must be smaller than the buy-leg offset.",
        key="oiw_sell_off",
    )
    buy_offset_abs = col_bo.number_input(
        "Buy-leg offset (|strikes| from ATM)",
        min_value=1, max_value=10, step=1,
        value=int(structure_cfg.get("buy_offset_abs", 6)),
        help="Long (buy) leg's |strike_offset| from ATM (further OTM than the sell leg). "
             "Wider gap = wider strike width, higher structural max-loss, smaller net credit.",
        key="oiw_buy_off",
    )

    col_et, col_fe = st.columns(2)
    entry_time = col_et.selectbox(
        "Signal time",
        options=entry_time_options,
        index=entry_idx,
        help="Conditions are checked at this bar (price/OI vs 10:00 wall-pick). "
             "If they pass, the spread is filled at the next minute close (T+1).",
        key="oiw_entry_time",
    )
    force_exit_time = col_fe.selectbox(
        "Force exit time",
        options=force_exit_options,
        index=force_idx,
        help="Forced TIME exit at this minute close if no TP/SL hit.",
        key="oiw_force_exit",
    )

    col_f, col_g, col_wb = st.columns(3)
    tp_inr = col_f.number_input(
        "Take profit (Rs / lot, 0 = off)",
        min_value=0.0, step=100.0,
        value=default_tp,
        help="Threshold = this value * lots. Exit when live spread P&L >= threshold.",
        key="oiw_tp",
    )
    sl_inr = col_g.number_input(
        "Stop loss (Rs / lot, 0 = off)",
        min_value=0.0, step=100.0,
        value=default_sl,
        help="Threshold = this value * lots. Exit when live spread P&L <= -threshold.",
        key="oiw_sl",
    )
    wall_breach_enabled = col_wb.checkbox(
        "Wall breach exit",
        value=bool(exit_cfg.get("wall_breach_enabled", True)),
        help="Exit when spot crosses the wall: CE -> spot >= wall_strike, "
             "PE -> spot <= wall_strike. Signal at T, fill at T+1. "
             "Priority over SL and TP if multiple fire same minute.",
        key="oiw_wall_breach",
    )

    col_ta, col_tl = st.columns(2)
    trail_arm_inr = col_ta.number_input(
        "Trail arm (Rs / lot profit, 0 = off)",
        min_value=0.0, step=100.0,
        value=float(exit_cfg.get("trail_arm_inr", 0.0)),
        help="One-time profit-lock step. When live spread P&L first reaches "
             "this value * lots, the stop is re-pinned UP to the locked level "
             "below for the rest of the day (sticky, not a ratchet). "
             "0 disables it.",
        key="oiw_trail_arm",
    )
    trail_lock_inr = col_tl.number_input(
        "Trail locked SL (Rs / lot)",
        step=100.0,
        value=float(exit_cfg.get("trail_lock_inr", 200.0)),
        help="Once armed, the SL sits at this profit level (* lots). "
             "Exit reason is 'TSL'. Keep it below the arm threshold and "
             "below TP; a negative value locks a small loss / break-even.",
        key="oiw_trail_lock",
    )
    st.caption(
        "Profit-lock: below the arm threshold the hard SL applies; once live "
        "P&L touches the arm, the stop jumps to the locked level (TP still "
        "caps profits above it)."
    )

    if st.button("Run backtest", type="primary", key="oiw_run_button"):
        run_config = dict(cfg)
        run_config["backtest_start"] = start_date.isoformat()
        run_config["backtest_end"] = end_date.isoformat()
        run_config["entry"] = {
            **cfg["entry"],
            "min_conditions_to_enter": int(min_conds),
            "entry_time": f"{entry_time}:00",
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
            "trail_arm_inr": float(trail_arm_inr),
            "trail_lock_inr": float(trail_lock_inr),
            "force_exit_time": f"{force_exit_time}:00",
            "wall_breach_enabled": bool(wall_breach_enabled),
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

        # Exit-reason breakdown: count (value) + total P&L for that bucket (delta).
        # TIME is shown as EOD (end-of-day forced exit).
        ex_counts = summary.get("exit_reason_counts", {})
        ex_pnl = summary.get("exit_reason_pnl", {})
        st.markdown("**Exit breakdown** (count · total P&L)")
        ec1, ec2, ec3, ec4 = st.columns(4)
        for col, label, reason in (
            (ec1, "TP", "TP"), (ec2, "SL", "SL"),
            (ec3, "TSL", "TSL"), (ec4, "EOD", "TIME"),
        ):
            col.metric(
                label, str(ex_counts.get(reason, 0)),
                delta=_fmt_money(ex_pnl.get(reason, 0.0)),
                delta_color="off" if not ex_counts.get(reason, 0) else "normal",
            )
        if ex_counts.get("BREACH", 0):
            st.metric(
                "BREACH", str(ex_counts["BREACH"]),
                delta=_fmt_money(ex_pnl.get("BREACH", 0.0)),
            )

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
                key="oiw_dl_trades",
            )
        with open(result["equity_csv"], "rb") as f:
            d2.download_button(
                "Download equity CSV",
                data=f.read(),
                file_name=os.path.basename(result["equity_csv"]),
                mime="text/csv",
                key="oiw_dl_equity",
            )
