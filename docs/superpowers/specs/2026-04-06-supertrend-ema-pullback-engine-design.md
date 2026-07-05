# Supertrend + EMA Pullback Backtest Engine

## Overview

A new backtest engine (`engine/st_ema_backtest.py`) that trades NIFTY ATM options using Supertrend for directional bias, EMA6/EMA12 for momentum confirmation, and EMA12 limit-order pullback entries on 1-min data. TP/SL are spot-point based, derived from same-day swing highs/lows.

## Instrument

NIFTY only. Lot size = 75, strike rounding = 50 (from `config.py`).

## Data Sources

| Data | Source | Purpose |
|------|--------|---------|
| 5-min spot | Resampled from 1-min spot parquet (`SPOT_DATA_PATH['NIFTY']`) | Indicator calculation |
| 1-min spot | Spot parquet directly | Entry detection, TP/SL/ST-flip monitoring |
| 1-min options | Options parquet (`DATA_PATH['NIFTY']`) | Option premium for entry/exit P&L |

**Warmup:** Load ~30 days of prior 5-min spot data so Supertrend and EMAs are stable from the first candle of each backtest day.

## Indicators (all on 5-min spot candles)

| Indicator | Default | Configurable | Purpose |
|-----------|---------|-------------|---------|
| Supertrend | ATR 12, Factor 3.0 | Yes (UI inputs) | Directional bias |
| EMA 6 | Period 6 | Yes | Momentum filter |
| EMA 12 | Period 12 | Yes | Pullback entry level |

**5-min to 1-min availability rule:** A 5-min bar timestamped at `T` (e.g., 12:45) represents candles from T to T+4min. Its indicators become available at `T+5min` (e.g., 12:50). The EMA12 value from the 12:45 bar is used to check for touches during 12:50-12:54.

## Signal Flow

### Stage 1 — Bias (5-min)

Supertrend flips bullish (direction changes from +1 to -1) → LONG bias.
Supertrend flips bearish (direction changes from -1 to +1) → SHORT bias.

Bias persists until the next Supertrend flip.

### Stage 2 — Readiness Check (5-min, per bar)

For each new 5-min bar, check:

- **Long ready:** Bias is LONG AND EMA6 > EMA12 AND TP >= 20 pts
- **Short ready:** Bias is SHORT AND EMA6 < EMA12 AND TP >= 20 pts

Where:
- **TP (long):** `swing_high - ema12` (swing high of last 12 same-day 5-min candles)
- **TP (short):** `ema12 - swing_low` (swing low of last 12 same-day 5-min candles)

**Same-day candle rule:** Only use 5-min candles from the current trading day for swing calculation. If fewer than 12 candles exist (early morning), use however many are available. No minimum required.

If TP < `min_target` (default 20 pts), do NOT look for entries during this 5-min window.

### Stage 3 — Entry (1-min monitoring)

When Stage 2 is ready, monitor 1-min spot candles within the current 5-min window:

- **Long entry:** 1-min candle `low <= EMA12` → fill at EMA12 value (limit order)
- **Short entry:** 1-min candle `high >= EMA12` → fill at EMA12 value (limit order)

**Option entry price:** Option premium close of the 1-min candle where spot touched EMA12.

### Strike Selection

ATM strike = `round(ema12_value / 50) * 50`

- Long → buy CE at ATM strike
- Short → buy PE at ATM strike
- Use nearest weekly expiry from `get_nearest_weekly_expiry(trading_date)`
- Filter options data: `expiry_type == 'WEEK'`, `expiry_code == 1`, matching strike and option_type

## Exit Rules

All exits checked on 1-min spot candles. Priority order:

### 1. SL Hit (highest priority)

- **Long SL level:** `entry_spot - (tp_distance / rr_ratio)`
  - Where `entry_spot = ema12_value`, `tp_distance = swing_high - ema12`, `rr_ratio = 1.25`
- **Short SL level:** `entry_spot + (tp_distance / rr_ratio)`
- **Trigger:** 1-min spot `low <= SL` (long) or `high >= SL` (short)
- **Exit price:** Option open of the next 1-min candle

### 2. TP Hit

- **Long TP level:** Swing high (same value used in readiness check)
- **Short TP level:** Swing low
- **Trigger:** 1-min spot `high >= TP` (long) or `low <= TP` (short)
- **Exit price:** Option open of the next 1-min candle

### 3. Supertrend Flip

- Supertrend direction changes on a new 5-min bar (detected at T+5min)
- **Exit price:** Option open of the next 1-min candle after detection
- **Re-entry:** Flip sets new bias immediately. If Stage 2 conditions met, can enter in opposite direction same day. Unlimited trades per day.

### 4. EOD Force Exit

- At 15:00 (3:00 PM), close any open position
- **Exit price:** Option close at 15:00

## P&L Calculation

- `pnl_points = exit_option_price - entry_option_price` (for CE and PE buys)
- `pnl_inr = pnl_points * lot_size` (lot_size = 75 for NIFTY)

## Time Windows

| Window | Time | Notes |
|--------|------|-------|
| Market open | 09:15 | Data starts here, indicators calculate |
| Entry start | 09:30 | First 5-min bar available at 09:20; first usable bar at 09:30 |
| Entry end | 14:55 | Last 5-min window is 14:50-14:54 |
| Force exit | 15:00 | EOD close |

## Trade Record (Dataclass Fields)

```
date, option_type (CE/PE), strike, expiry_date
signal_time        — when Supertrend set bias
ready_time         — when Stage 2 conditions met (5-min bar time)
touch_time         — when 1-min candle touched EMA12
entry_spot         — EMA12 value at entry
entry_option_price — option close at touch candle
tp_level           — swing high/low in spot points
sl_level           — computed SL in spot points
tp_distance        — TP distance in spot points
sl_distance        — SL distance in spot points
exit_time, exit_spot, exit_option_price
exit_reason        — "TP" | "SL" | "ST_FLIP" | "EOD"
pnl_points, pnl_inr
supertrend_dir, supertrend_val, ema6, ema12 — indicators at entry
```

## UI Runner (`ui/st_ema_backtest_runner.py`)

Streamlit page with:

### Input Parameters (3-column layout)

| Column 1 | Column 2 | Column 3 |
|-----------|----------|----------|
| Start Date | ST ATR Length (12) | Min Target pts (20) |
| End Date | ST Factor (3.0) | RR Ratio (1.25) |
| | EMA Short (6) | Swing Lookback (12) |
| | EMA Long (12) | |

### Results Display

- Key metrics: total trades, wins, losses, win rate, total P&L, avg P&L per trade
- Exit reason breakdown: TP / SL / ST_FLIP / EOD counts and avg P&L per reason
- CE vs PE breakdown
- Equity curve (cumulative P&L chart)
- Full trades table with filters (option_type, exit_reason)
- CSV download

## File Structure

```
engine/st_ema_backtest.py      — engine class
ui/st_ema_backtest_runner.py   — Streamlit UI page
```

## Edge Cases

1. **No options data for strike:** Skip entry, log warning. Move to next signal.
2. **TP = swing high but only 1 candle exists:** Use that candle's high. If TP < min_target, skip.
3. **SL and TP hit on same 1-min candle:** SL takes priority (conservative).
4. **ST flip while no position:** Just update bias, no exit needed.
5. **Multiple touches in same 5-min window:** Only first touch triggers entry. Subsequent touches ignored if already in position.
6. **Position open when new 5-min bar arrives:** TP/SL levels do NOT update. They are locked at entry time. Only ST flip can override.
