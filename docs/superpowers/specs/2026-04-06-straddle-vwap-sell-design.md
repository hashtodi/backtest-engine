# ATM Straddle VWAP Sell Strategy

## Overview

A new backtest engine (`engine/straddle_vwap_backtest.py`) that sells NIFTY ATM straddles (CE + PE) at nearest weekly expiry when the straddle price touches VWAP. Entry at VWAP value (limit order). TP 2% / SL 3.5% on combined straddle price. Re-entry allowed.

## Instrument

NIFTY only. Lot size = 75 per leg, strike rounding = 50 (from `config.py`).

## Data Sources

| Data | Source | Purpose |
|------|--------|---------|
| 1-min options | Options parquet (`DATA_PATH['NIFTY']`) | CE and PE prices, volume |
| 1-min spot | Spot parquet (`SPOT_DATA_PATH['NIFTY']`) | ATM strike determination |

No 5-min resampling needed. Everything runs on 1-min options data.

## Straddle Price Construction

Each minute, for the ATM strike (nearest weekly, expiry_code=1):

```
straddle_close = CE_close + PE_close
straddle_open  = CE_open + PE_open
straddle_volume = CE_volume + PE_volume
```

ATM strike is determined from spot price: `round(spot / 50) * 50`.

**Important:** The ATM strike for VWAP calculation floats with spot (recalculated each minute). But once a trade is entered, the trade's strike is fixed.

## VWAP Calculation

VWAP is calculated on the straddle close price, using combined volume, with daily session reset (no bands).

```
source = straddle_close
volume = straddle_volume (CE vol + PE vol)

cumulative_pv += source * volume   (price × volume)
cumulative_v  += volume
VWAP = cumulative_pv / cumulative_v
```

- Resets at start of each trading day (09:15)
- No bands, just the VWAP line
- VWAP(T-1) = VWAP value at the end of the previous 1-min candle

## Time Window

| Window | Time |
|--------|------|
| VWAP calculation starts | 09:15 (market open, for warmup) |
| Entry window start | 11:00 |
| Entry window end | 14:30 |
| Force exit | 14:30 |

VWAP builds up from 09:15 but we only look for entries from 11:00. This gives ~105 minutes of VWAP warmup.

## Entry Signal

**Condition (checked each minute from 11:00 to 14:30):**

Straddle close crossed VWAP(T-1) from either direction:

```
cross_above = straddle_close[T-1] < VWAP[T-2]  AND  straddle_close[T] >= VWAP[T-1]
cross_below = straddle_close[T-1] > VWAP[T-2]  AND  straddle_close[T] <= VWAP[T-1]
entry_signal = cross_above OR cross_below
```

When either is true, the straddle price just touched/crossed the VWAP level.

**Entry execution:**
- Sell straddle at **VWAP(T-1) value** (limit order — price moved through this level during candle T)
- ATM strike = `round(spot_close_at_T / 50) * 50`, fixed for the trade
- Sell 1 lot CE + 1 lot PE at this strike, nearest weekly expiry

**Entry price for P&L:**
- `entry_price = VWAP(T-1)` (the straddle combined price at which we sold)

## TP / SL Levels

Set at entry, locked for the trade:

```
TP level = entry_price × (1 - 0.02)     # straddle drops 2% = profit
SL level = entry_price × (1 + 0.035)    # straddle rises 3.5% = loss
```

Example: entry at VWAP = 500.
- TP = 500 × 0.98 = 490 (straddle drops to 490 → profit ₹750)
- SL = 500 × 1.035 = 517.5 (straddle rises to 517.5 → loss ₹1,312.5)

## Exit Rules

Each minute, compute `straddle_close = CE_close + PE_close` at the trade's fixed strike.

Priority order:

### 1. SL Hit

`straddle_close >= SL level` → exit at exact SL level value.

### 2. TP Hit

`straddle_close <= TP level` → exit at exact TP level value.

### 3. EOD Force Exit (14:30)

At 14:30, if still in position → exit at `straddle_close` at that moment.

## P&L Calculation

Since we are **selling** the straddle:

```
pnl_points = entry_price - exit_price
pnl_inr = pnl_points × lot_size (75)
```

- TP hit: pnl_points = VWAP - TP_level = positive
- SL hit: pnl_points = VWAP - SL_level = negative
- EOD: pnl_points = VWAP - straddle_close_at_1430 = could be either

## Re-entry

After any exit (SL/TP/EOD), if still within 11:00-14:30, look for another VWAP crossover signal. Multiple trades per day allowed.

## Trade Record (Dataclass Fields)

```
date, strike, expiry_date
entry_time         — "HH:MM" when VWAP crossover detected (candle T)
entry_price        — VWAP(T-1) value (straddle combined)
vwap_at_entry      — same as entry_price
straddle_at_entry  — straddle_close at candle T (for reference)
tp_level           — entry_price × 0.98
sl_level           — entry_price × 1.035
exit_time
exit_price         — exact SL/TP level, or straddle_close at EOD
exit_reason        — "TP" / "SL" / "EOD"
pnl_points         — entry_price - exit_price
pnl_inr            — pnl_points × 75
ce_entry_price     — CE close at entry for reference
pe_entry_price     — PE close at entry for reference
```

## UI Runner (`ui/straddle_vwap_backtest_runner.py`)

Streamlit page with:

### Input Parameters

| Column 1 | Column 2 |
|-----------|----------|
| Start Date | TP % (2.0) |
| End Date | SL % (3.5) |
| Entry Start (11:00) | |
| Entry End / EOD (14:30) | |

### Results Display

- Key metrics: total trades, wins, losses, win rate, total P&L, avg P&L
- Exit reason breakdown: TP / SL / EOD counts and avg P&L
- Equity curve
- Trades table with filters, CSV download

## File Structure

```
engine/straddle_vwap_backtest.py      — engine class
ui/straddle_vwap_backtest_runner.py   — Streamlit UI page
```

## Edge Cases

1. **No options data for ATM strike at a minute:** Skip that minute, continue.
2. **VWAP volume = 0:** Skip VWAP update for that candle (stale VWAP carries forward).
3. **SL and TP both breached on same candle (straddle_close):** Not possible — straddle_close is a single value, can't be both >= SL and <= TP simultaneously (SL > entry > TP).
4. **ATM strike changes between VWAP signal and trade monitoring:** VWAP uses floating ATM. Trade uses fixed ATM from entry. These are independent.
5. **Multiple crossovers in quick succession:** Each one triggers a new trade (if no position is open). Position must be closed first before re-entry.
