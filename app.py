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
tab_dashboard, tab_trades, tab_backtest, tab_forward = st.tabs([
    "📊 Dashboard",
    "📋 Trade Explorer",
    "🚀 Run Backtest",
    "📡 Forward Test",
])

with tab_dashboard:
    render_dashboard()

with tab_trades:
    render_trades()

with tab_backtest:
    render_backtest()

with tab_forward:
    render_forward_test()
