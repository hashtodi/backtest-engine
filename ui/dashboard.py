"""
Dashboard tab: key metrics, equity curve, and charts.

Shows:
  - Saved strategies selector
  - Strategy summary (collapsible)
  - Combined metrics: total P&L, return, win rate, trades
  - Per-instrument breakdown
  - Equity curve (cumulative portfolio value)
  - P&L distribution histogram
  - Exit reason pie chart
"""

import streamlit as st
import plotly.express as px
from pathlib import Path

import config
from ui.helpers import load_results, load_strategy, get_combined_trades
from ui.strategy_store import list_saved_strategies, load_saved_strategy, get_output_dir


def _load_results_from_output(strategy: dict, inst: str):
    """Load results CSV from a saved strategy's output folder."""
    import pandas as pd
    out_dir = get_output_dir(strategy)
    csv_path = out_dir / f"results_{inst}.csv"
    if not csv_path.exists():
        return pd.DataFrame()
    df = pd.read_csv(csv_path)
    if 'signal_time' in df.columns:
        df['signal_time'] = pd.to_datetime(df['signal_time'])
    if 'exit_time' in df.columns:
        df['exit_time'] = pd.to_datetime(df['exit_time'])
    return df


def _get_combined_from_output(strategy: dict, instruments: list):
    """Combine results across instruments from a saved strategy's output."""
    import pandas as pd
    frames = []
    for inst in instruments:
        df = _load_results_from_output(strategy, inst)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(
        'signal_time'
    ).reset_index(drop=True)


def render_dashboard():
    """Render the Dashboard tab."""

    # ---- Strategy selector ----
    saved = list_saved_strategies()
    strategy = None
    use_saved = False

    if saved:
        names = ["Latest run"] + [s["name"] for s in saved]
        slugs = [""] + [s["slug"] for s in saved]
        choice = st.selectbox("Strategy", names, key="dash_strategy_select")
        idx = names.index(choice)
        if idx > 0:
            strategy = load_saved_strategy(slugs[idx])
            use_saved = True

    # Fall back to last run or default
    if strategy is None:
        strategy = st.session_state.get("last_strategy") or load_strategy("rsi_70_sell")

    instruments = strategy.get('instruments', ['NIFTY', 'SENSEX'])

    # ---- Strategy summary (collapsed by default) ----
    conditions = strategy.get('signal_conditions', [])
    logic = strategy.get('signal_logic', 'AND')
    cond_parts = []
    for c in conditions:
        val = c.get('value', c.get('other', ''))
        cond_parts.append(f"{c['indicator']} {c['compare']} {val}")
    signal_str = f" {logic} ".join(cond_parts) if cond_parts else "—"

    levels = strategy.get('entry_levels', [])
    if len(levels) == 1 and levels[0].get('pct_above_base', 0) == 0:
        entry_str = "Direct entry (100%)"
    else:
        entry_str = " / ".join(
            f"+{lvl['pct_above_base']}% ({lvl['capital_pct']}%)"
            for lvl in levels
        ) + " (staggered)"

    sl_val = strategy.get('stop_loss_pct', 0)
    tp_val = strategy.get('target_pct', 0)
    sl_str = "Off" if sl_val >= 9999 else f"{sl_val}%"
    tp_str = "Off" if tp_val >= 9999 else f"{tp_val}%"

    with st.expander("Strategy Details", expanded=False):
        st.markdown(f"""
**{strategy.get('name', 'Strategy')}** — {strategy.get('description', '')}

| Parameter | Value |
|-----------|-------|
| Direction | {strategy.get('direction', 'sell')} |
| Signal | {signal_str} |
| Entry Levels | {entry_str} |
| Stop Loss | {sl_str} |
| Target | {tp_str} |
| Hours | {strategy.get('trading_start', '09:30')} - {strategy.get('trading_end', '14:30')} IST |
""")

    # ---- Load combined trades ----
    # If viewing a saved strategy, load from its output folder
    if use_saved:
        combined = _get_combined_from_output(strategy, instruments)
    else:
        combined = get_combined_trades(instruments)

    if combined.empty:
        st.warning("No backtest results found. Run a backtest first!")
        return

    # ---- Top-level metrics ----
    total_pnl = combined['money_pnl'].sum()
    total_trades = len(combined)
    wins = len(combined[combined['money_pnl'] > 0])
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    initial_capital = strategy.get('initial_capital', 200000) * len(instruments)
    return_pct = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total P&L", f"₹{total_pnl:,.0f}")
    c2.metric("Return", f"{return_pct:.1f}%")
    c3.metric("Win Rate", f"{win_rate:.1f}%")
    c4.metric("Total Trades", f"{total_trades}")

    st.divider()

    # ---- Per-instrument metrics ----
    for inst in instruments:
        if use_saved:
            df = _load_results_from_output(strategy, inst)
        else:
            df = load_results(inst)
        if df.empty:
            continue

        lot_size = config.LOT_SIZE.get(inst, 1)
        inst_pnl = df['money_pnl'].sum()
        inst_wins = len(df[df['money_pnl'] > 0])
        inst_total = len(df)

        st.subheader(f"{inst} (Lot: {lot_size})")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("P&L", f"₹{inst_pnl:,.0f}")
        m2.metric("Trades", inst_total)
        m3.metric("Wins", f"{inst_wins} ({inst_wins / inst_total * 100:.0f}%)")
        # Guard against NaN when no wins or no losses exist
        avg_win = df[df['money_pnl'] > 0]['money_pnl'].mean()
        avg_loss = df[df['money_pnl'] < 0]['money_pnl'].mean()
        m4.metric("Avg Win", f"₹{avg_win:,.0f}" if avg_win == avg_win else "₹0")
        m5.metric("Avg Loss", f"₹{avg_loss:,.0f}" if avg_loss == avg_loss else "₹0")

    st.divider()

    # ---- Equity curve ----
    st.subheader("Equity Curve")
    combined['cumulative_pnl'] = combined['money_pnl'].cumsum()
    combined['capital'] = initial_capital + combined['cumulative_pnl']

    fig_eq = px.line(
        combined, x='signal_time', y='capital',
        title='Portfolio Value Over Time',
        labels={'signal_time': 'Date', 'capital': 'Portfolio Value (₹)'},
    )
    fig_eq.update_layout(hovermode='x unified')
    st.plotly_chart(fig_eq, width="stretch")

    # ---- Bottom row: histogram + pie chart ----
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("P&L Distribution")
        fig_hist = px.histogram(
            combined, x='money_pnl', nbins=50,
            title='Trade P&L Distribution',
            labels={'money_pnl': 'P&L (₹)'},
            color_discrete_sequence=['#636EFA'],
        )
        st.plotly_chart(fig_hist, width="stretch")

    with col_right:
        st.subheader("Exit Reasons")
        exit_counts = combined['exit_reason'].value_counts()
        fig_pie = px.pie(
            values=exit_counts.values,
            names=exit_counts.index,
            title='Exit Reason Breakdown',
        )
        st.plotly_chart(fig_pie, width="stretch")
