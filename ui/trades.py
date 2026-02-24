"""
Trade Explorer tab: browse and filter individual trades.

Shows:
  - Strategy selector (must pick a saved strategy)
  - Filter bar: instrument, option type, exit reason, P&L direction
  - Interactive data table with formatted columns
  - Summary caption with filtered count
"""

import pandas as pd
import streamlit as st

from ui.strategy_store import render_strategy_selector, get_output_dir


# Columns to display in the trade table
DISPLAY_COLS = [
    'instrument', 'option_type', 'strike', 'signal_time',
    'direction', 'parts_filled', 'avg_entry_price',
    'exit_time', 'exit_price', 'exit_reason',
    'pnl_pct', 'money_pnl',
]


def _load_results_from_output(strategy: dict, inst: str) -> pd.DataFrame:
    """Load results CSV from a saved strategy's output folder."""
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


def render_trades():
    """Render the Trade Explorer tab."""
    # ---- Strategy selector (no pre-selection) ----
    strategy, slug = render_strategy_selector("te")
    if strategy is None:
        st.info("Select a strategy above to explore trades.")
        return

    instruments = strategy.get('instruments', ['NIFTY', 'SENSEX'])

    # Load trades from saved strategy output
    frames = []
    for inst in instruments:
        df = _load_results_from_output(strategy, inst)
        if not df.empty:
            frames.append(df)
    if not frames:
        combined = pd.DataFrame()
    else:
        combined = pd.concat(frames, ignore_index=True).sort_values(
            'signal_time'
        ).reset_index(drop=True)

    if combined.empty:
        st.warning("No backtest results found for this strategy. Run a backtest first!")
        return

    # ---- Filter bar ----
    c1, c2, c3, c4 = st.columns(4)

    with c1:
        inst_filter = st.multiselect(
            "Instrument", instruments, default=instruments
        )
    with c2:
        opt_filter = st.multiselect(
            "Option Type", ['CE', 'PE'], default=['CE', 'PE']
        )
    with c3:
        all_reasons = combined['exit_reason'].unique().tolist()
        exit_filter = st.multiselect(
            "Exit Reason", all_reasons, default=all_reasons
        )
    with c4:
        pnl_filter = st.radio(
            "P&L", ["All", "Winners", "Losers"], horizontal=True
        )

    # ---- Apply filters ----
    filtered = combined[
        (combined['instrument'].isin(inst_filter)) &
        (combined['option_type'].isin(opt_filter)) &
        (combined['exit_reason'].isin(exit_filter))
    ]

    if pnl_filter == "Winners":
        filtered = filtered[filtered['money_pnl'] > 0]
    elif pnl_filter == "Losers":
        filtered = filtered[filtered['money_pnl'] < 0]

    st.caption(f"Showing {len(filtered)} of {len(combined)} trades")

    # ---- Data table ----
    st.dataframe(
        filtered[DISPLAY_COLS].sort_values('signal_time', ascending=False),
        width="stretch",
        height=600,
        column_config={
            "money_pnl": st.column_config.NumberColumn(
                "P&L (₹)", format="₹%.0f"
            ),
            "pnl_pct": st.column_config.NumberColumn(
                "P&L %", format="%.2f%%"
            ),
            "avg_entry_price": st.column_config.NumberColumn(
                "Avg Entry", format="%.2f"
            ),
            "exit_price": st.column_config.NumberColumn(
                "Exit Price", format="%.2f"
            ),
            "strike": st.column_config.NumberColumn(
                "Strike", format="%.0f"
            ),
        },
    )
