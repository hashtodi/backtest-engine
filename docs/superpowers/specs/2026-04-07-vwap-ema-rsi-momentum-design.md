# VWAP-EMA-RSI Momentum Strategy — Design Spec

## Overview

Rule-based intraday NIFTY options strategy that captures momentum moves using VWAP, EMA crossover, and RSI confirmation on 5-min candles, with entry on the next 1-min candle open. SL/TP tracked on spot price; P&L realized from option premiums.

## Entry Conditions

### Buy CE (Bullish)
- Spot > VWAP + 0.2% (configurable offset)
- EMA(9) > EMA(20) on 5-min spot
- RSI(14) > 55 on 5-min spot
- All conditions checked on **closed** 5-min bar

### Buy PE (Bearish)
- Spot < VWAP - 0.2%
- EMA(9) < EMA(20) on 5-min spot
- RSI(14) < 45 on 5-min spot
- All conditions checked on **closed** 5-min bar

### Entry Execution
- Signal on closed 5-min bar → enter at **open of next 1-min candle**
- ATM strike = round(spot / 50) * 50 at entry time
- Weekly expiry, 1 lot

## Exit Conditions

All SL/TP levels tracked on **spot price**, not option premium.

### Stop Loss
- 15 points from entry spot (configurable)
- CE: SL = entry_spot - 15
- PE: SL = entry_spot + 15

### Target Profit
- 30 points from entry spot (configurable)
- CE: TP = entry_spot + 30
- PE: TP = entry_spot - 30

### Trailing Stop Loss
- Trigger: spot reaches +15 points profit (configurable)
- Action: SL moves to entry_spot (cost/breakeven)
- Moves **once only**, does not continue trailing

### Same-Candle Ambiguity
- If a 1-min candle's range hits both SL and TP: **assume SL hit first** (conservative)
- If a 1-min candle hits both trail trigger and original SL: **assume SL hit first**

### EOD
- Force exit all positions at 15:00

## Avoidance Filters (Configurable, Placeholder)

Skip signal when:
- EMA(9) and EMA(20) flat/overlapping — threshold configurable later
- Price within +/-0.2% of VWAP — already handled by entry condition
- RSI between 45-55 — already handled by entry condition

### Strong Candle Filter
- Placeholder, off by default
- When enabled: bullish candle close in top N% of range, bearish in bottom N%

## Risk Management

- **Max 3 trades per day** (configurable)
- **2 consecutive losses → stop trading for the day** (configurable)
  - Resets daily
  - Breakeven exits (trailing SL at cost) do NOT count as losses
  - Only real losses (exit below entry for buys) count

## Indicators

All computed on **5-min resampled spot data**, forward-filled to 1-min grid with +5min shift (no lookahead).

| Indicator | Source | Parameters |
|-----------|--------|------------|
| VWAP | Spot close + volume, 5-min | Resets daily at 09:15 |
| EMA 9 | Spot close, 5-min | period=9 |
| EMA 20 | Spot close, 5-min | period=20 |
| RSI 14 | Spot close, 5-min | period=14 (Wilder's smoothing) |

## Data Flow

```
1-min spot parquet → resample to 5-min
→ compute VWAP (with daily reset), EMA9, EMA20, RSI14
→ shift +5min and forward-fill back to 1-min grid
→ day-by-day loop:
    Every 5-min boundary (closed bar):
      Check CE/PE signal conditions
      If signal → mark pending entry
    Next 1-min candle:
      Enter at open, pick ATM strike, fetch option premium
    Every 1-min bar (open positions):
      Check SL/TP on spot
      Check trail trigger on spot
      Same-candle: SL priority
      15:00: force exit
    Daily state: trade count, consecutive losses
```

## Architecture

### New Files
- `engine/vwap_ema_rsi_backtest.py` — Core engine
- `ui/vwap_ema_rsi_backtest_runner.py` — Streamlit tab

### Modified Files
- `app.py` — Add new tab

### Pattern
Follows existing custom engine pattern (st_ema_backtest.py, dema_st_backtest.py):
- Standalone engine with own Trade dataclass
- Own data loading, indicator calculation, forward-fill pipeline
- Progress callback for UI integration
- `trades_to_dataframe()` helper for results

## Trade Dataclass Fields

- date, option_type, strike, expiry_date
- vwap, ema9, ema20, rsi, spot_at_entry
- signal_time, entry_time, entry_price (option premium), entry_spot
- sl_level, tp_level, trail_triggered (bool)
- exit_time, exit_price, exit_reason (SL/TP/TRAIL_SL/EOD)
- pnl_points, pnl_pct, pnl_inr

## UI Parameters

| Parameter | Default | Key |
|-----------|---------|-----|
| Date range | 2025-01-20 to 2026-03-12 | ver_start, ver_end |
| VWAP offset % | 0.2 | ver_vwap_offset |
| EMA short period | 9 | ver_ema_short |
| EMA long period | 20 | ver_ema_long |
| RSI period | 14 | ver_rsi_period |
| RSI upper threshold | 55 | ver_rsi_upper |
| RSI lower threshold | 45 | ver_rsi_lower |
| SL points | 15 | ver_sl_pts |
| TP points | 30 | ver_tp_pts |
| Trail trigger points | 15 | ver_trail_pts |
| Max trades/day | 3 | ver_max_trades |
| Max consecutive losses | 2 | ver_max_consec |
| Entry start | 09:30 | ver_entry_start |
| Force exit | 15:00 | ver_force_exit |
