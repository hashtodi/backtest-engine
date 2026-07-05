"""
Streamlit dashboard for Options Backtester.

Entry point for the web UI. Run with:
    streamlit run app.py

Tabs:
  1. Dashboard     - Key metrics, equity curve, charts
  2. Trades        - Browse and filter individual trades
  3. Backtest      - Configure parameters and run new backtests
  4. Forward Test  - Paper trading with live data feed
"""

import streamlit as st

from ui.dashboard import render_dashboard
from ui.trades import render_trades
from ui.backtest_runner import render_backtest
from ui.forward_test import render_forward_test
from ui.dema_st_backtest_runner import render_dema_st_backtest
from ui.boom_sma_backtest_runner import render_boom_sma_backtest
from ui.boom_st_backtest_runner import render_boom_st_backtest
from ui.st_ema_backtest_runner import render_st_ema_backtest
from ui.straddle_vwap_backtest_runner import render_straddle_vwap_backtest
from ui.vwap_ema_rsi_backtest_runner import render_vwap_ema_rsi_backtest
from ui.bb_reversal_backtest_runner import render_bb_reversal_backtest
from ui.bb_reversal_pine_backtest_runner import render_bb_reversal_pine_backtest
from ui.bb_reversal_pine_exit_backtest_runner import render_bb_reversal_pine_exit_backtest
from ui.prt_backtest_runner import render_prt_backtest
from ui.ha_nr7_backtest_runner import render_ha_nr7_backtest
from ui.ema5_futures_backtest_runner import render_ema5_futures_backtest
from ui.gamma_blast_backtest_runner import render_gamma_blast_backtest
from ui.supertrend_low_band_backtest_runner import render_supertrend_low_band_backtest
from ui.debit_spread_backtest_runner import render_debit_spread_backtest
from ui.zero_credit_backtest_runner import render_zero_credit_backtest
from ui.oi_wall_backtest_runner import render_oi_wall_backtest
from ui.pcr_momentum_backtest_runner import render_pcr_momentum_backtest
from ui.zayn_smc_backtest_runner import render_zayn_smc_backtest
from ui.traffic_light_backtest_runner import render_traffic_light_backtest
from ui.iv_symmetry_backtest_runner import render_iv_symmetry_backtest
from ui.ema_spread_backtest_runner import render_ema_spread_backtest
from ui.dema_mtf_vwap_backtest_runner import render_dema_mtf_vwap_backtest
from ui.regime_rsi_spread_backtest_runner import render_regime_rsi_spread_backtest
from ui.bb_pivot_spread_backtest_runner import render_bb_pivot_spread_backtest
from ui.triple_st_ema_spread_backtest_runner import render_triple_st_ema_spread_backtest
from ui.st_pcr_vix_credit_spread_runner import render_st_pcr_vix_credit_spread_backtest
from ui.sensex_dual_short_runner import render_sensex_dual_short_backtest
from ui.iv_hv_condor_backtest_runner import render_iv_hv_condor_backtest

# ============================================
# PAGE CONFIG
# ============================================
st.set_page_config(
    page_title="Options Backtester",
    page_icon="📊",
    layout="wide",
)

st.title("Options Backtester")
st.caption("Configure, backtest, and analyze options trading strategies")

# ============================================
# TABS
# ============================================
tab_dashboard, tab_trades, tab_backtest, tab_forward, tab_dema_st, tab_st_ema, tab_straddle_vwap, tab_boom, tab_boom_st, tab_vwap_ema_rsi, tab_bb_reversal, tab_bb_reversal_pine, tab_bb_reversal_pine_exit, tab_prt, tab_ha_nr7, tab_ema5_fut, tab_gamma_blast, tab_st_low_band, tab_debit_spread, tab_zero_credit, tab_oi_wall, tab_pcr_momentum, tab_zayn_smc, tab_traffic_light, tab_iv_symmetry, tab_ema_spread, tab_dema_mtf_vwap, tab_regime_rsi_spread, tab_bb_pivot_spread, tab_triple_st_ema_spread, tab_st_pcr_vix_spread, tab_sensex_dual_short, tab_nifty_dual_short, tab_iv_hv_condor = st.tabs([
    "📊 Dashboard",
    "📋 Trade Explorer",
    "🚀 Run Backtest",
    "📡 Forward Test",
    "🔄 DEMA-ST Pullback",
    "🎯 ST+EMA Pullback",
    "📉 Straddle VWAP",
    "💥 Boom SMA",
    "💥 Boom ST",
    "📈 VWAP-EMA-RSI",
    "🎯 BB Reversal PE",
    "📌 BB Reversal PE-pinescript",
    "📌 BB Reversal PE-pinescript-exit",
    "📊 PRT Strategy",
    "🎲 HA-NR7",
    "📈 EMA5 Futures",
    "💥 Gamma Blast",
    "🎯 ST Low-Band",
    "🦋 Debit Spread",
    "🧬 Zero Credit",
    "🧱 OI Wall",
    "⚖️ PCR Momentum",
    "🧠 Zayn SMC",
    "🚦 Traffic Light",
    "🪞 IV Symmetry Straddle",
    "〰️ EMA Spread",
    "🧭 DEMA MTF VWAP",
    "🧭 Regime RSI Spread",
    "🎚️ BB-Pivot Credit Spread",
    "🔺 Triple ST + EMA Spread",
    "🌀 ST+PCR+VIX Credit Spread",
    "🎏 SENSEX Dual Short",
    "🪁 NIFTY Dual Short",
    "🦅 IV/HV Iron Condor",
])

with tab_dashboard:
    render_dashboard()

with tab_trades:
    render_trades()

with tab_backtest:
    render_backtest()

with tab_forward:
    render_forward_test()

with tab_dema_st:
    render_dema_st_backtest()

with tab_st_ema:
    render_st_ema_backtest()

with tab_straddle_vwap:
    render_straddle_vwap_backtest()

with tab_boom:
    render_boom_sma_backtest()

with tab_boom_st:
    render_boom_st_backtest()

with tab_vwap_ema_rsi:
    render_vwap_ema_rsi_backtest()

with tab_bb_reversal:
    render_bb_reversal_backtest()

with tab_bb_reversal_pine:
    render_bb_reversal_pine_backtest()

with tab_bb_reversal_pine_exit:
    render_bb_reversal_pine_exit_backtest()

with tab_prt:
    render_prt_backtest()

with tab_ha_nr7:
    render_ha_nr7_backtest()

with tab_ema5_fut:
    render_ema5_futures_backtest()

with tab_gamma_blast:
    render_gamma_blast_backtest()

with tab_st_low_band:
    render_supertrend_low_band_backtest()

with tab_debit_spread:
    render_debit_spread_backtest()

with tab_zero_credit:
    render_zero_credit_backtest()

with tab_oi_wall:
    render_oi_wall_backtest()

with tab_pcr_momentum:
    render_pcr_momentum_backtest()

with tab_zayn_smc:
    render_zayn_smc_backtest()

with tab_traffic_light:
    render_traffic_light_backtest()

with tab_iv_symmetry:
    render_iv_symmetry_backtest()

with tab_ema_spread:
    render_ema_spread_backtest()

with tab_dema_mtf_vwap:
    render_dema_mtf_vwap_backtest()

with tab_regime_rsi_spread:
    render_regime_rsi_spread_backtest()

with tab_bb_pivot_spread:
    render_bb_pivot_spread_backtest()

with tab_triple_st_ema_spread:
    render_triple_st_ema_spread_backtest()

with tab_st_pcr_vix_spread:
    render_st_pcr_vix_credit_spread_backtest()

with tab_sensex_dual_short:
    render_sensex_dual_short_backtest(instrument="SENSEX", key_prefix="sds_sx")

with tab_nifty_dual_short:
    render_sensex_dual_short_backtest(instrument="NIFTY", key_prefix="sds_nf")

with tab_iv_hv_condor:
    render_iv_hv_condor_backtest()
