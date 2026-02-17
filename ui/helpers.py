"""
Shared helpers for the Streamlit UI.

Provides:
  - load_results(): reads backtest CSV into DataFrame
  - load_strategy(): imports strategy config from strategies/ folder
  - get_combined_trades(): merges results across instruments
"""

import importlib
from pathlib import Path

import pandas as pd
import streamlit as st


def load_results(instrument: str) -> pd.DataFrame:
    """
    Load backtest results CSV for one instrument.

    Returns empty DataFrame if file doesn't exist.
    Parses datetime columns automatically.

    NOTE: No @st.cache_data here. CSVs are small (fast to read)
    and caching prevents Dashboard/Trade Explorer from picking up
    new results after a backtest runs.
    """
    path = Path(f"backtest_results_{instrument}.csv")
    if not path.exists():
        return pd.DataFrame()

    df = pd.read_csv(path)
    df['signal_time'] = pd.to_datetime(df['signal_time'])
    df['exit_time'] = pd.to_datetime(df['exit_time'])
    return df


def load_strategy(name: str) -> dict:
    """Load strategy config dict from strategies/ folder."""
    module = importlib.import_module(f"strategies.{name}")
    return module.STRATEGY


def get_combined_trades(instruments: list) -> pd.DataFrame:
    """
    Load and merge trades from all instruments.

    Returns a combined DataFrame sorted by signal_time.
    Returns empty DataFrame if no results exist.
    """
    frames = []
    for inst in instruments:
        df = load_results(inst)
        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True).sort_values(
        'signal_time'
    ).reset_index(drop=True)
