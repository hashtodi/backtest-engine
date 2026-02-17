"""
Streamlit dashboard for Options Backtester.

Entry point for the web UI. Run with:
    streamlit run app.py

Tabs:
  1. Dashboard  - Key metrics, equity curve, charts
  2. Trades     - Browse and filter individual trades
  3. Backtest   - Configure parameters and run new backtests
"""

import streamlit as st

from ui.dashboard import render_dashboard
from ui.trades import render_trades
from ui.backtest_runner import render_backtest

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
tab_dashboard, tab_trades, tab_backtest = st.tabs([
    "📊 Dashboard",
    "📋 Trade Explorer",
    "🚀 Run Backtest",
])

with tab_dashboard:
    render_dashboard()

with tab_trades:
    render_trades()

with tab_backtest:
    render_backtest()
