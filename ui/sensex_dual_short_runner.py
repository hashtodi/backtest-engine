"""Streamlit form + runner for the dual short-premium backtest (SENSEX / NIFTY)."""
import json
import math
import os
from datetime import date

import pandas as pd
import streamlit as st

from engine.sensex_dual_short_backtest import (
    SensexDualShortBacktest, summarize, bucket_summary,
)

DEFAULT_CONFIG_PATH = "saved_strategies/sensex_dual_short.json"
OUTPUT_DIR = "output/dual_short"

# Per-instrument default backtest windows (first day of options data -> end of dataset).
INSTRUMENT_DEFAULTS = {
    "SENSEX": ("2023-05-15", "2026-04-23"),
    "NIFTY": ("2020-08-03", "2026-05-22"),
}


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
    return f"{val * 100:.1f}%"


def render_sensex_dual_short_backtest(instrument: str = "SENSEX",
                                      key_prefix: str = "sds",
                                      default_start: str = None,
                                      default_end: str = None) -> None:
    """Render the dual-short backtest tab for one instrument.

    instrument: "SENSEX" or "NIFTY" (locked for this tab).
    key_prefix: unique Streamlit widget-key prefix (so two tabs don't collide).
    """
    instrument = instrument.upper()
    lot_size = 20 if instrument == "SENSEX" else 65
    pts = 600 if instrument == "SENSEX" else 300
    d_start, d_end = INSTRUMENT_DEFAULTS.get(instrument, ("2023-05-15", "2026-04-23"))
    default_start = default_start or d_start
    default_end = default_end or d_end

    st.header(f"Dual Short-Premium — {instrument}")
    st.caption(
        f"Two short-premium sub-strategies on {instrument} weekly options (DTE 0-3), "
        f"one 1-lot block (lot {lot_size}). **Part 1 (09:45):** sell the 0.25Δ CE "
        f"and PE (offset ±6 strikes = ±{pts} pts from ATM) at the 09:45 close. "
        "**Part 2 (after 11:45):** freeze the 09:45-11:45 spot High/Low and record "
        "the 09:45 ATM strikes; on a high break sell the recorded ATM PUT, on a low "
        "break sell the recorded ATM CALL (both can fire). Every leg: SL at +25% of "
        "entry cost, target at -90% (10% left), one re-entry at cost after an SL. "
        "Strike is locked at entry; SL/target monitor only that contract. No "
        "slippage. Hard square-off 15:28."
    )

    cfg = _load_default_config()
    entry_cfg = cfg.get("entry", {}) or {}
    mgmt_cfg = cfg.get("management", {}) or {}
    structure_cfg = cfg.get("structure", {}) or {}

    col_a, col_b = st.columns(2)
    start_date = col_a.date_input(
        "Backtest start", value=date.fromisoformat(default_start),
        key=f"{key_prefix}_start",
    )
    end_date = col_b.date_input(
        "Backtest end", value=date.fromisoformat(default_end),
        key=f"{key_prefix}_end",
    )

    col_c, col_d, col_e = st.columns(3)
    sl_pct = col_c.number_input(
        "Stop loss (% premium rise)",
        min_value=1.0, step=5.0,
        value=float(mgmt_cfg.get("sl_pct_rise", 25)),
        help="Exit a leg when its premium rises this % above entry cost "
             "(25 => SL at 1.25x cost).",
        key=f"{key_prefix}_sl",
    )
    tgt_pct = col_d.number_input(
        "Target (% premium captured)",
        min_value=1.0, max_value=99.0, step=5.0,
        value=float(mgmt_cfg.get("target_pct_capture", 90)),
        help="Exit when this % of the premium has decayed "
             "(90 => target at 0.10x cost).",
        key=f"{key_prefix}_tgt",
    )
    off_strikes = col_e.number_input(
        "0.25Δ offset (strikes from ATM)",
        min_value=1, max_value=10, step=1,
        value=int(entry_cfg.get("delta_025_offset_strikes", 6)),
        help=f"Part-1 strangle width in strikes. 6 => ±{pts} pts on {instrument}. "
             "Part-2 legs are always ATM.",
        key=f"{key_prefix}_off",
    )

    col_f, col_g = st.columns(2)
    lots = col_f.number_input(
        "Lots",
        min_value=1, step=1,
        value=int(structure_cfg.get("lots", 1)),
        help=f"Number of lots per leg (lot size {lot_size}). P&L scales linearly.",
        key=f"{key_prefix}_lots",
    )
    dte_max = col_g.number_input(
        "Max DTE to trade (0..N)",
        min_value=0, max_value=6, step=1,
        value=int(entry_cfg.get("dte_max", 3)),
        help="Trade only when the nearest weekly expiry is within this many "
             "days. Default 3 => trade the ~4 sessions up to & incl. expiry.",
        key=f"{key_prefix}_dte",
    )

    dte_mode = st.radio(
        "DTE counting",
        options=["trading", "calendar"],
        index=0, horizontal=True,
        help="'trading' counts real sessions to expiry (weekends/holidays skipped) "
             "-> ~4 sessions/week before every expiry. 'calendar' counts raw days "
             "-> Tuesday-expiry weeks only trade Mon+Tue (weekend inflates the count).",
        key=f"{key_prefix}_dtemode",
    )

    if st.button("Run backtest", type="primary", key=f"{key_prefix}_run_button"):
        eng = SensexDualShortBacktest(
            start_date.isoformat(), end_date.isoformat(),
            instrument=instrument,
            sl_mult=1.0 + float(sl_pct) / 100.0,
            tgt_mult=1.0 - float(tgt_pct) / 100.0,
            off_025=int(off_strikes),
            dte_max=int(dte_max),
            dte_mode=dte_mode,
            lots=int(lots),
        )
        with st.spinner(f"Running {instrument} backtest (reads full options history)..."):
            eng.run()

        s = summarize(eng.trades, eng.skips, eng.non_trading_days)

        st.subheader("Summary")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total P&L", _fmt_money(s["total_pnl_inr"]))
        m2.metric("Part 1 P&L", _fmt_money(s["p1_pnl_inr"]))
        m3.metric("Part 2 P&L", _fmt_money(s["p2_pnl_inr"]))
        m4.metric("Win rate (per leg)", _fmt_pct(s["win_rate"]))

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Legs", str(s["n_legs"]))
        m6.metric("Round-trips", str(s["n_round_trips"]))
        m7.metric("Re-entries", str(s["n_reentries"]))
        m8.metric("Non-trading days", str(s["non_trading_days"]))

        m9, m10 = st.columns(2)
        m9.metric("Avg win", _fmt_money(s["avg_win_inr"]))
        m10.metric("Avg loss", _fmt_money(s["avg_loss_inr"]))

        st.markdown("**Data-coverage split** — observed vs approximated P&L")
        b1, b2, b3 = st.columns(3)
        b1.metric("Fully-observed P&L", _fmt_money(s["observed_pnl_inr"]))
        b2.metric(
            "Blind P&L (approx)", _fmt_money(s["blind_pnl_inr"]),
            delta=f"{s['blind_pnl_pct'] * 100:.1f}% of total", delta_color="off",
        )
        b3.metric("Blind legs", f"{s['n_blind_legs']} / {s['n_legs']}")
        st.caption(
            "**Blind** = legs squared off at the last available price because the "
            "locked strike drifted outside the stored ±10-strike window before "
            "15:28 (exit reason `EOD_LAST_AVAILABLE`). Their P&L is an "
            "approximation — treat **fully-observed** as the demonstrable edge."
        )

        st.markdown("**Window buckets** — what happened to the locked strike vs the ±10 data window")
        bs = bucket_summary(eng.trades)
        bucket_rows = []
        for key, label in (("clean", "Never left window"),
                           ("recovered", "Left & came back (price reversed)"),
                           ("blind", "Blind (last-seen exit)")):
            d = bs.get(key)
            if not d:
                bucket_rows.append({"Bucket": label, "Legs": 0, "Win%": "—",
                                    "Total P&L": "—", "Mean": "—", "Median": "—",
                                    "Avg win / loss": "—", "Median hold": "—"})
                continue
            bucket_rows.append({
                "Bucket": label,
                "Legs": d["n_legs"],
                "Win%": _fmt_pct(d["win_rate"]),
                "Total P&L": _fmt_money(d["total_pnl_inr"]),
                "Mean": _fmt_money(d["mean_pnl_inr"]),
                "Median": _fmt_money(d["median_pnl_inr"]),
                "Avg win / loss": f"{_fmt_money(d['avg_win_inr'])} / {_fmt_money(d['avg_loss_inr'])}",
                "Median hold": f"{d['hold_median_min']:.0f} min",
            })
        st.table(pd.DataFrame(bucket_rows).set_index("Bucket"))
        st.caption(
            "**Never left** = strike stayed within ±10 strikes of ATM all day. "
            "**Left & came back** = drifted beyond ±10 (price moved away, we lost its "
            "quote) then reversed back into range and exited normally. "
            "**Blind** = still open when it left for good → squared off at last-seen price."
        )

        st.markdown("**Exit breakdown** (count)")
        ex = s["exit_reason_counts"]
        ec = st.columns(4)
        for col, reason in zip(ec, ("TARGET", "SL", "EOD", "EOD_LAST_AVAILABLE")):
            col.metric(reason, str(ex.get(reason, 0)))

        if s["skips"]:
            st.write("**Skip ledger** (legs skipped — missing strike at needed minute):", s["skips"])

        trades_df = pd.DataFrame(eng.trades)
        if not trades_df.empty:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            fname = f"{instrument.lower()}_dual_short_trades.csv"
            out_csv = os.path.join(OUTPUT_DIR, fname)
            trades_df.to_csv(out_csv, index=False)

            st.subheader("Cumulative P&L")
            eq = trades_df.sort_values("exit_time").copy()
            eq["cum_pnl_inr"] = eq["pnl_inr"].cumsum()
            eq = eq.reset_index(drop=True)
            st.line_chart(eq["cum_pnl_inr"])

            st.subheader("Trades")
            st.dataframe(trades_df, use_container_width=True)

            with open(out_csv, "rb") as f:
                st.download_button(
                    "Download trades CSV",
                    data=f.read(),
                    file_name=fname,
                    mime="text/csv",
                    key=f"{key_prefix}_dl_trades",
                )
        else:
            st.info("No trades generated in this window.")
