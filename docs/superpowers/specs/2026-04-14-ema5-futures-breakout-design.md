# EMA5 Futures Breakout — Custom Backtest Engine Design

**Date:** 2026-04-14
**Strategy source:** ema5-ansu.md (5 EMA Alert + Breakout on Nifty Futures)

---

## Overview

A momentum-continuation strategy on Nifty Futures. When price separates from EMA(5) without a pullback (alert candle), and the next candle confirms a breakout/breakdown by closing beyond the alert candle's range, we buy an ITM option in the direction of the move.

SL/TP are tracked on futures prices. P&L is computed from option premiums.

---

## Data Sources

| Source | Path | Usage |
|--------|------|-------|
| Nifty Futures 1-min | `data/futures/NIFTY_FUT_1m.parquet` | EMA calc, alert/confirm detection, SL/TP monitoring |
| Nifty Options 1-min | `data/options/nifty/NIFTY_OPTIONS_1m.parquet` | Option premium at entry/exit for P&L |

Futures columns: `ts, datetime, expiry_date, open, high, low, close, volume, oi`

---

## Indicator

- **EMA(5)** computed on futures `close`, 1-min candles
- No resampling needed — everything runs on 1-min

---

## Signal Logic

### Alert State

A candle is in **alert state** when the EMA(5) line is completely outside the candle's high-low range:

- **Bullish alert:** `ema < candle_low` (price running above EMA)
- **Bearish alert:** `ema > candle_high` (price running below EMA)

The alert is identified at the **close** of that candle (i.e., when the candle is complete).

### Confirmation (strictly the NEXT candle only)

Given an alert candle at time T:

- **Bullish confirmation:** candle T+1 **closes** above alert candle's `high`
- **Bearish confirmation:** candle T+1 **closes** below alert candle's `low`

If T+1 does NOT close beyond the level → alert dies. No carryover.

Note: T+1 can simultaneously be a new alert candle (checked after confirmation logic).

### Entry

Entry at the **open** of candle T+2 (the candle after the confirmation candle closes).

---

## Trade Execution

### ITM Strike Selection

At entry, using the futures close at confirmation candle (T+1):

- **Bullish (CE):** `strike = floor(futures_price / 50) * 50` (1 strike ITM)
- **Bearish (PE):** `strike = ceil(futures_price / 50) * 50` (1 strike ITM)

Strike interval: 50 (Nifty). Expiry: nearest weekly.

### Entry Price

- **Futures entry price:** open of candle T+2 (used for SL/TP reference)
- **Option entry price:** open of candle T+2 for the selected ITM strike (used for P&L)

### Stop Loss (on futures price)

- **Bullish trade:** `SL = alert_candle_close - sl_buffer` (default 5 points)
- **Bearish trade:** `SL = alert_candle_close + sl_buffer` (default 5 points)

### Target (on futures price)

- Risk = `|entry_price_futures - SL|`
- **Bullish:** `TP = entry_price_futures + risk * rr_ratio`
- **Bearish:** `TP = entry_price_futures - risk * rr_ratio`

Default `rr_ratio = 1.0` (1:1 R:R).

---

## Exit Logic (priority order, checked each 1-min candle)

1. **SL hit:** 
   - Bullish: futures `low <= SL` → exit at SL level
   - Bearish: futures `high >= SL` → exit at SL level
2. **TP hit:**
   - Bullish: futures `high >= TP` → exit at TP level
   - Bearish: futures `low <= TP` → exit at TP level
3. **Same-candle SL + TP:** take SL (SL has priority)
4. **EOD force exit:** at the **close** of the `force_exit_time` candle (default 15:00)
   - Exit at futures close price; option P&L uses option close at that timestamp

### Option Exit Price Lookup

When an exit signal fires on futures:
- If SL/TP: look up the option candle at that same timestamp. Use `close` as exit price (we can't pin exact intra-candle option price from futures trigger).
- If EOD: use option `close` at force_exit_time.

---

## Position Management

- **Multiple trades per day:** Yes. After exit (SL/TP/EOD), immediately re-scan for new alerts starting from the very next candle.
- **One position at a time:** No overlapping trades.
- **No cooldown:** New alert can form on the candle immediately after exit.

---

## Configurable Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ema_period` | int | 5 | EMA period on futures close |
| `sl_buffer` | float | 5.0 | Points buffer on alert candle close for SL |
| `rr_ratio` | float | 1.0 | Risk:Reward ratio |
| `entry_start` | str | "09:30" | Earliest time to detect alerts |
| `force_exit_time` | str | "15:00" | EOD force exit (at close of this candle) |
| `strike_depth` | int | 1 | Number of strikes ITM |
| `instrument` | str | "NIFTY" | Instrument |
| `start_date` | str | — | Backtest start date |
| `end_date` | str | — | Backtest end date |

---

## Trade Dataclass Fields

```
date, direction (CE/PE), strike, expiry_date,
alert_time, alert_high, alert_low, alert_close, alert_ema,
confirm_time, confirm_close,
entry_time, entry_price_futures, entry_price_option,
sl_level, tp_level,
exit_time, exit_price_futures, exit_price_option, exit_reason,
pnl_futures_points, pnl_option_points, pnl_option_pct, pnl_inr
```

---

## Output

- `List[Ema5FuturesTrade]` → `pd.DataFrame` → CSV
- Same pattern as `dema_st_backtest.py`

---

## Architecture

Single file: `engine/ema5_futures_backtest.py`

```
Ema5FuturesBacktestEngine
├── __init__(params)
├── _load_futures() → DataFrame
├── _load_options() → DataFrame
├── _calculate_ema(futures_df) → DataFrame (adds ema column)
├── run(progress_callback) → List[Trade]
│   ├── per day: _process_day(date) → List[Trade]  # multiple trades/day
│   │   ├── scan candles sequentially
│   │   ├── state machine: IDLE → ALERT → entry → IN_POSITION → exit → IDLE
│   │   └── option price lookup on entry/exit
```

No changes to `data_loader.py` — futures loading is self-contained in the engine (different schema from options data).

---

## Edge Cases

1. **Alert candle is also confirmation for previous alert:** Not possible — previous alert dies if T+1 doesn't confirm, and T+1 is checked for confirmation before being checked as a new alert. But T+1 CAN be a new alert candle after being evaluated as confirmation.

2. **EMA exactly equals High or Low:** NOT an alert (EMA touches the range). Alert requires `ema < low` or `ema > high` (strict inequality).

3. **Confirmation candle close exactly equals alert High/Low:** NOT confirmed (must be strictly beyond). `close > high` for bullish, `close < low` for bearish.

4. **Entry candle (T+2) doesn't exist (end of day):** Skip this trade.

5. **No options data for the selected strike at entry time:** Skip this trade.

6. **Force exit time reached while in ALERT state (not yet in position):** Discard the alert, no trade.
