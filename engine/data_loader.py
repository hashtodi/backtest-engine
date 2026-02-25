"""
Data loading and indicator calculation.

Loads parquet options data, applies filters (date range, weekly expiry),
and calculates all indicators defined in the strategy config.

Two price sources for indicators:
  - "option" (default): calculated on option close price, per unique contract
    (contract = strike + option_type + expiry_type + expiry_code).
    Resets on new expiry.
  - "spot": calculated on the underlying/spot price. One value per minute,
    shared across all contracts. Does NOT reset on expiry.
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
    Calculate all indicators and add them as columns.

    Supports two price sources:
      - "option": calculated per contract on option close price (existing behavior)
      - "spot":   calculated once on underlying spot price, merged by datetime

    A "_prev" column is added for each indicator (for crossover detection).

    For multi-output indicators (MACD, Bollinger), each output gets its own column:
      e.g., "opt_macd_12_26_9_macd", "spot_bb_20_2_upper"

    Args:
        df: DataFrame from load_data()
        indicator_configs: list of dicts from strategy, e.g.:
            [{"type": "RSI", "period": 14, "name": "spot_rsi_14", "price_source": "spot"}]

    Returns:
        DataFrame with indicator columns added
    """
    if not indicator_configs:
        logger.warning("No indicators configured")
        return df

    # Add _prev columns per contract for price field comparisons.
    # close_prev is needed for price_crosses_above/below signals.
    # high_prev, low_prev, open_prev enable wick-based crossover detection.
    for col in ('close', 'high', 'low', 'open'):
        if col in df.columns:
            df[f'{col}_prev'] = df.groupby(CONTRACT_GROUP_COLS)[col].shift(1)

    # Create indicator instances
    indicators: List[Indicator] = []
    for cfg in indicator_configs:
        ind_type = cfg['type']
        ind_name = cfg['name']
        # Pass all params except type, name, and price_source to the indicator class
        params = {k: v for k, v in cfg.items() if k not in ('type', 'name', 'price_source')}
        ind = get_indicator(ind_type, name=ind_name, **params)
        indicators.append(ind)

    # Split into spot and option indicators
    spot_pairs = [(ind, cfg) for ind, cfg in zip(indicators, indicator_configs)
                  if cfg.get('price_source', 'option') == 'spot']
    option_pairs = [(ind, cfg) for ind, cfg in zip(indicators, indicator_configs)
                    if cfg.get('price_source', 'option') != 'spot']

    # --- Spot indicators: one calculation on the spot price timeline ---
    if spot_pairs:
        logger.info(f"Calculating {len(spot_pairs)} spot indicator(s) on underlying price...")
        # Extract unique spot price per minute (same for all contracts at a given time)
        spot_df = (df.drop_duplicates('datetime')[['datetime', 'spot']]
                   .sort_values('datetime').reset_index(drop=True))

        for ind, cfg in spot_pairs:
            result = ind.calculate(spot_df['spot'])
            _merge_spot_result(df, spot_df, ind, result)

    # --- Option indicators: per-contract calculation on option close ---
    if option_pairs:
        logger.info(f"Calculating {len(option_pairs)} option indicator(s) per contract...")

        for ind, cfg in option_pairs:
            # VWAP resets daily — group by contract + date.
            # All other indicators are continuous per contract.
            if cfg['type'] == 'VWAP':
                group_cols = CONTRACT_GROUP_COLS + ['date']
            else:
                group_cols = CONTRACT_GROUP_COLS

            groups = df.groupby(group_cols)
            result_parts = []

            for _, group in groups:
                group = group.sort_values('datetime')
                # SuperTrend needs high/low for proper True Range calculation.
                # Other indicators just get close + volume.
                if cfg['type'] == 'SUPERTREND':
                    result = ind.calculate(
                        group['close'], group.get('volume'),
                        high=group.get('high'), low=group.get('low'),
                    )
                else:
                    result = ind.calculate(group['close'], group.get('volume'))
                result_parts.append(result)

            _merge_option_result(df, ind, result_parts)

    return df


def _merge_spot_result(df, spot_df, ind, result):
    """Merge a spot indicator result back into the main DataFrame by datetime.

    Uses dict-based mapping to avoid timezone mismatch issues with pd.Series.map().
    """
    if isinstance(result, dict):
        # Multi-output (MACD, Bollinger on spot)
        for key, series in result.items():
            col_name = f"{ind.name}_{key}"
            spot_map = dict(zip(spot_df['datetime'], series.values))
            df[col_name] = df['datetime'].map(spot_map)
            # _prev per contract (for crossover detection within a contract's timeline)
            df[f"{col_name}_prev"] = df.groupby(CONTRACT_GROUP_COLS)[col_name].shift(1)
        logger.info(f"  {ind.name} [spot]: {', '.join(result.keys())} calculated")
    else:
        # Single-output (RSI, EMA, SMA on spot)
        col_name = ind.name
        spot_map = dict(zip(spot_df['datetime'], result.values))
        df[col_name] = df['datetime'].map(spot_map)
        df[f"{col_name}_prev"] = df.groupby(CONTRACT_GROUP_COLS)[col_name].shift(1)
        non_null = df[col_name].notna().sum()
        logger.info(f"  {col_name} [spot]: {non_null:,} non-null values")


def _merge_option_result(df, ind, result_parts):
    """Merge per-contract option indicator results back into the main DataFrame."""
    if isinstance(result_parts[0], dict):
        # Multi-output (MACD, Bollinger)
        sub_keys = result_parts[0].keys()
        for key in sub_keys:
            col_name = f"{ind.name}_{key}"
            combined = pd.concat([r[key] for r in result_parts]).sort_index()
            df[col_name] = combined
            df[f"{col_name}_prev"] = df.groupby(CONTRACT_GROUP_COLS)[col_name].shift(1)
        logger.info(f"  {ind.name} [option]: {', '.join(sub_keys)} calculated")
    else:
        # Single-output (RSI, EMA, SMA, VWAP)
        col_name = ind.name
        combined = pd.concat(result_parts).sort_index()
        df[col_name] = combined
        df[f"{col_name}_prev"] = df.groupby(CONTRACT_GROUP_COLS)[col_name].shift(1)
        non_null = df[col_name].notna().sum()
        logger.info(f"  {col_name} [option]: {non_null:,} non-null values")
