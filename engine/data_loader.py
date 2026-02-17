"""
Data loading and indicator calculation.

Loads parquet options data, applies filters (date range, weekly expiry),
and calculates all indicators defined in the strategy config.

Indicators are calculated per unique contract:
  contract = strike + option_type + expiry_type + expiry_code
Each contract gets a fresh indicator calculation (resets on new expiry).
"""

import pandas as pd
import logging
from typing import List, Dict

from indicators import get_indicator
from indicators.base import Indicator

logger = logging.getLogger(__name__)

# Contract grouping columns.
# Each unique combo is a separate contract with its own indicator history.
CONTRACT_GROUP_COLS = ['strike', 'option_type', 'expiry_type', 'expiry_code']


def load_data(data_path: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    Load parquet file and apply standard filters.

    Filters applied:
      - Date range (start_date to end_date inclusive)
      - Nearest weekly expiry only (expiry_type=WEEK, expiry_code=1)
      - No moneyness filter (we need all strikes to track contracts post-ATM)

    Args:
        data_path: path to parquet file
        start_date: "YYYY-MM-DD" start date
        end_date: "YYYY-MM-DD" end date

    Returns:
        Filtered and sorted DataFrame with helper columns (date, time_only)
    """
    logger.info(f"Loading {data_path}...")
    df = pd.read_parquet(data_path)

    # Parse datetime
    df['datetime'] = pd.to_datetime(df['datetime'])

    # Filter: date range
    start = pd.to_datetime(start_date).tz_localize('Asia/Kolkata')
    end = pd.to_datetime(end_date).tz_localize('Asia/Kolkata') + pd.Timedelta(days=1)
    df = df[(df['datetime'] >= start) & (df['datetime'] < end)]

    # Filter: nearest weekly expiry only (expiry_type=WEEK, expiry_code=1)
    # Monthly contracts also have expiry_code=1, so we must filter both.
    df = df[(df['expiry_code'] == 1) & (df['expiry_type'] == 'WEEK')]

    # NOTE: We do NOT filter by moneyness here.
    # We need all strikes so we can track a contract's price
    # even after it stops being ATM (underlying moved).
    # ATM filter is applied only during signal detection.

    # Sort chronologically
    df = df.sort_values('datetime').reset_index(drop=True)

    # Add helper columns for faster day/time lookups
    df['date'] = df['datetime'].dt.date
    df['time_only'] = df['datetime'].dt.time

    logger.info(
        f"Loaded {len(df):,} rows | "
        f"{df['date'].nunique()} trading days | "
        f"Range: {df['datetime'].min()} to {df['datetime'].max()}"
    )
    return df


def calculate_indicators(df: pd.DataFrame, indicator_configs: List[Dict]) -> pd.DataFrame:
    """
    Calculate all indicators per unique contract.

    Each indicator is calculated on the contract's close prices (and volume for VWAP).
    Results are added as new columns to the DataFrame.
    A "_prev" column is also added for each indicator (for crossover detection).

    For multi-output indicators (MACD, Bollinger), each output gets its own column:
      e.g., "macd_12_26_9_macd", "macd_12_26_9_signal", "macd_12_26_9_histogram"

    Args:
        df: DataFrame from load_data()
        indicator_configs: list of dicts from strategy, e.g.:
            [{"type": "RSI", "period": 14, "name": "rsi_14"}]

    Returns:
        DataFrame with indicator columns added
    """
    if not indicator_configs:
        logger.warning("No indicators configured")
        return df

    # Add close_prev per contract (needed for price_crosses_above/below signals).
    # Must exist before indicator calculations so signal checks can use it.
    df['close_prev'] = df.groupby(CONTRACT_GROUP_COLS)['close'].shift(1)

    # Create indicator instances
    indicators: List[Indicator] = []
    for cfg in indicator_configs:
        # Extract type and name, pass rest as params
        ind_type = cfg['type']
        ind_name = cfg['name']
        params = {k: v for k, v in cfg.items() if k not in ('type', 'name')}
        ind = get_indicator(ind_type, name=ind_name, **params)
        indicators.append(ind)

    logger.info(f"Calculating {len(indicators)} indicator(s) per contract...")

    for ind, cfg in zip(indicators, indicator_configs):
        # VWAP resets daily — group by contract + date.
        # All other indicators are continuous per contract.
        if cfg['type'] == 'VWAP':
            group_cols = CONTRACT_GROUP_COLS + ['date']
        else:
            group_cols = CONTRACT_GROUP_COLS

        groups = df.groupby(group_cols)

        # Calculate indicator for each group
        result_parts = []

        for _, group in groups:
            group = group.sort_values('datetime')
            result = ind.calculate(group['close'], group.get('volume'))

            if isinstance(result, dict):
                # Multi-output indicator (MACD, Bollinger)
                result_parts.append(result)
            else:
                # Single-output indicator (RSI, EMA, SMA, VWAP)
                result_parts.append(result)

        if isinstance(result_parts[0], dict):
            # Multi-output: merge each sub-series
            sub_keys = result_parts[0].keys()
            for key in sub_keys:
                col_name = f"{ind.name}_{key}"
                combined = pd.concat([r[key] for r in result_parts]).sort_index()
                df[col_name] = combined
                # Previous value for crossover detection
                df[f"{col_name}_prev"] = df.groupby(CONTRACT_GROUP_COLS)[col_name].shift(1)
            logger.info(f"  {ind.name}: {', '.join(sub_keys)} calculated")
        else:
            # Single-output: merge all groups
            col_name = ind.name
            combined = pd.concat(result_parts).sort_index()
            df[col_name] = combined
            # Previous value for crossover detection
            df[f"{col_name}_prev"] = df.groupby(CONTRACT_GROUP_COLS)[col_name].shift(1)
            non_null = df[col_name].notna().sum()
            logger.info(f"  {col_name}: {non_null:,} non-null values")

    return df
