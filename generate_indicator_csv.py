"""
Generate indicator verification CSV for SENSEX.

Calculates SMA(9), SMA(21), EMA(9), EMA(20), RSI(14), RSI(7)
on ATM option close prices for Feb 13-18, 2026.

Uses the exact same indicator classes and per-contract grouping
as the main backtest engine (engine/data_loader.py).
"""

import pandas as pd
from indicators.sma import SMA
from indicators.ema import EMA
from indicators.rsi import RSI

# === Config ===
DATA_PATH = 'data/options/sensex/SENSEX_OPTIONS_1m.parquet'
START_DATE = '2026-02-13'
END_DATE = '2026-02-18'
OUTPUT_CSV = 'sensex_indicator_verification.csv'

# Contract grouping — same as engine/data_loader.py
CONTRACT_GROUP_COLS = ['strike', 'option_type', 'expiry_type', 'expiry_code']


def main():
    # --- Load data ---
    print("Loading data...")
    df = pd.read_parquet(DATA_PATH)
    df['datetime'] = pd.to_datetime(df['datetime'])

    # Filter: date range (same logic as engine/data_loader.py)
    start = pd.to_datetime(START_DATE).tz_localize('Asia/Kolkata')
    end = pd.to_datetime(END_DATE).tz_localize('Asia/Kolkata') + pd.Timedelta(days=1)
    df = df[(df['datetime'] >= start) & (df['datetime'] < end)]

    # Filter: nearest weekly expiry only (same as data_loader)
    df = df[(df['expiry_code'] == 1) & (df['expiry_type'] == 'WEEK')]

    # Sort chronologically
    df = df.sort_values('datetime').reset_index(drop=True)

    print(f"Loaded {len(df):,} rows")
    print(f"Range: {df['datetime'].min()} to {df['datetime'].max()}")
    print(f"Trading days: {df['datetime'].dt.date.nunique()}")

    # --- Calculate indicators per contract ---
    # Exact same classes from indicators/ folder
    indicators = [
        SMA(name='sma_9', period=9),
        SMA(name='sma_21', period=21),
        EMA(name='ema_9', period=9),
        EMA(name='ema_20', period=20),
        RSI(name='rsi_14', period=14),
        RSI(name='rsi_7', period=7),
    ]

    print(f"\nCalculating {len(indicators)} indicators per contract...")

    for ind in indicators:
        # Group by contract — each contract gets a fresh calculation
        result_parts = []
        for _, group in df.groupby(CONTRACT_GROUP_COLS):
            group = group.sort_values('datetime')
            result = ind.calculate(group['close'], group.get('volume'))
            result_parts.append(result)

        # Merge all groups back
        combined = pd.concat(result_parts).sort_index()
        df[ind.name] = combined

        non_null = df[ind.name].notna().sum()
        print(f"  {ind.name}: {non_null:,} non-null values")

    # --- Filter to ATM only ---
    atm_df = df[df['moneyness'] == 'ATM'].copy()

    # Select output columns
    output_cols = [
        'datetime', 'strike', 'option_type', 'spot', 'close',
        'sma_9', 'sma_21', 'ema_9', 'ema_20', 'rsi_14', 'rsi_7',
    ]
    atm_df = atm_df[output_cols].sort_values(['datetime', 'option_type'])

    # Round indicator values for readability
    indicator_cols = ['sma_9', 'sma_21', 'ema_9', 'ema_20', 'rsi_14', 'rsi_7']
    atm_df[indicator_cols] = atm_df[indicator_cols].round(2)

    # --- Save CSV ---
    atm_df.to_csv(OUTPUT_CSV, index=False)
    print(f"\nSaved {len(atm_df):,} rows to {OUTPUT_CSV}")
    print(f"Unique strikes: {atm_df['strike'].nunique()}")
    print(f"\nFirst 10 rows:")
    print(atm_df.head(10).to_string(index=False))


if __name__ == '__main__':
    main()
