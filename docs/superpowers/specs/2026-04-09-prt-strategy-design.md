# PRT Strategy — Design Spec

## Overview

Trend + momentum + mean-reversion + sentiment strategy on NIFTY spot (configurable 3/5-min timeframe), with option execution and partial exit logic.

**Entry (CE — bullish):** All 4 conditions on resampled spot candle:
1. SuperTrend bullish (direction == -1)
2. MACD histogram > 0 (MACD line above signal — state, not crossover)
3. Spot candle close < VWAP (pullback below average)
4. PCR > threshold OR PCR uptrending (pcr_prev - pcr_prev_30min > 0.1) — either suffices

**Entry (PE — bearish):** Vice versa of all 4 conditions.

**Execution:** 2 lots (configurable) of ATM option at next 1-min bar OPEN.

**Exits (whichever comes first):**
- ST flip → full exit (all remaining lots) — pending, next bar OPEN
- MACD flip (histogram sign change) → 50% exit (1 lot) — pending, next bar OPEN
- TP 30% → full exit at TP level — immediate, intra-bar
- SL 30% → full exit at SL level — immediate, intra-bar
- EOD → full exit at option close

**Priority:** SL/TP > indicator flips (SL/TP known intra-bar, indicators at bar end).

---

## No-Lookahead Timing Model

### Entry

At any resampled candle boundary where all 4 conditions are met:

```
Example with 5-min timeframe:
  Resampled candle T (e.g. 10:10-10:14:59) closes
  → ST, MACD, VWAP known (from candle close)
  → PCR at T+4 known (last 1-min of candle, using pcr_prev in code)
  → All 4 conditions met
  → Enter at T+5 OPEN (first 1-min bar after candle close)
  → ATM strike from spot OPEN at T+5
  → Option entry price = option OPEN at T+5 for that strike

General: candle closes at T+(timeframe-1):59 → enter at T+timeframe OPEN
```

Implementation: forward-fill resampled indicators with +timeframe shift. At the boundary bar, new indicators appear. Enter at current bar's OPEN.

### Indicator Exits (ST flip, MACD flip)

At any boundary bar where a flip is detected while in position:

```
Example with 5-min timeframe:
  Resampled candle T (e.g. 14:15-14:19:59) closes
  → ST flip / MACD flip detected at T+4:59
  → Queue pending exit
  → Exit at T+5 OPEN (next 1-min bar)

General: flip detected at candle close → exit at T+timeframe OPEN
```

Implementation: flip detected at the boundary bar where new indicators appear (after +timeframe shift). Since SL/TP is checked first on that bar's OHLC, the indicator exit fires only if SL/TP wasn't hit on the same bar.

### SL/TP Exits

Checked every 1-min bar on option OHLC:

```
SL (for long option):
  if option_open <= SL → exit at option_open (gap below SL)
  elif option_low <= SL → exit at SL level

TP:
  if option_open >= TP → exit at option_open (gap above TP)
  elif option_high >= TP → exit at TP level

If both SL and TP touched on same bar → SL wins (conservative)
```

### PCR No-Lookahead

PCR column is at 1-min granularity. The value for minute T includes data through T:59. To avoid lookahead, use `pcr_prev` (shift by 1). At bar 10:15, we use PCR from 10:14.

PCR change = `pcr_prev - pcr_prev.shift(30)` (always 30 1-min bars = 30 minutes).

---

## Processing Order (per 1-min bar)

```
1. FILL PENDING INDICATOR EXIT (queued from previous boundary bar)
   → ST_FLIP: exit all remaining lots at option OPEN
   → MACD_FLIP: exit 1 lot at option OPEN (once per trade)

2. SL/TP CHECK (every bar, if in position)
   → SL first, then TP. Exit at level or gap price.
   → If hit, takes priority over any indicator flip on this bar.

3. INDICATOR FLIP DETECTION (boundary bars only, if in position)
   → Compare current vs previous bar's forward-filled indicators
   → ST flip: queue pending full exit for next bar
   → MACD flip (histogram sign change): queue pending partial exit for next bar
   → ST checked before MACD (full exit > partial)

4. EOD CHECK (time >= trading_end)
   → Exit all remaining at option close

5. ENTRY CHECK (boundary bars only, if not in position, if didn't just exit this bar)
   → Check all 4 conditions using resampled indicators + pcr_prev
   → CE: st_dir == -1, histogram > 0, resampled_close < vwap, pcr_ok
   → PE: st_dir == +1, histogram < 0, resampled_close > vwap, pcr_ok
   → Enter at option OPEN, ATM strike from spot OPEN
```

"Boundary bar" = the 1-min bar where new resampled indicators become visible (after +timeframe shift). For 5-min: minutes 09:20, 09:25, 09:30, ... For 3-min: 09:18, 09:21, 09:24, ...

---

## Trade Lifecycle

```
NOT_IN_POSITION
  → boundary bar, all 4 conditions → enter 2 lots at option OPEN
  → state: FULL_POSITION

FULL_POSITION (2 lots)
  → SL/TP hit         → exit ALL at level         → CLOSED
  → ST flip (pending)  → exit ALL at next OPEN     → CLOSED
  → MACD flip (pending)→ exit 1 lot at next OPEN   → PARTIAL_POSITION
  → EOD               → exit ALL at option close   → CLOSED

PARTIAL_POSITION (1 lot, MACD partial already done)
  → SL/TP hit         → exit 1 lot at level        → CLOSED
  → ST flip (pending)  → exit 1 lot at next OPEN    → CLOSED
  → EOD               → exit 1 lot at option close  → CLOSED

CLOSED → can take new signals (subject to max_trades_per_day)
```

TP/SL stays at original entry price after partial exit (does not reset).

---

## Data Requirements

### Input: Spot+PCR Parquet

Same schema as existing `NIFTY_1m.parquet` plus a `pcr` column:

| Column | Type | Description |
|--------|------|-------------|
| datetime | string (ISO, tz=Asia/Kolkata) | Minute timestamp |
| open, high, low, close | float | Spot OHLC |
| volume | int | Spot volume |
| pcr | float | Put-Call Ratio (OI-based) |

Default path: `data/spot/nifty/NIFTY_1m_pcr.parquet` (configurable in UI).

### Input: Options Parquet

Existing `NIFTY_OPTIONS_1m.parquet` — loaded via `load_data()` with `expiry_mode="weekly"`.

---

## Architecture

### Files

| File | Purpose |
|------|---------|
| `engine/prt_backtest.py` | Engine class + PrtTrade dataclass |
| `ui/prt_backtest_runner.py` | Streamlit UI (params, run, results) |
| `app.py` | Add PRT tab |

### Engine Class

```python
class PrtBacktestEngine:
    def __init__(self, start_date, end_date, spot_pcr_path,
                 spot_timeframe, st_period, st_factor,
                 macd_fast, macd_slow, macd_signal,
                 tp_pct, sl_pct, lots_per_trade,
                 pcr_threshold, pcr_lookback,
                 trading_start, trading_end, max_trades_per_day,
                 instrument)

    def _load_spot_pcr(self) -> pd.DataFrame
    def _resample(self, spot_1m) -> pd.DataFrame
    def _calculate_indicators(self, resampled) -> pd.DataFrame
    def _forward_fill_to_1m(self, spot_1m, resampled) -> pd.DataFrame
    def _prepare_data(self)
    def run(self, progress_callback) -> List[PrtTrade]
    def _process_day(self, trading_date) -> List[PrtTrade]
```

### Trade Dataclass

```python
@dataclass
class PrtTrade:
    date: str
    option_type: str          # CE / PE
    strike: float
    expiry_date: str

    # Signal context
    signal_time: str          # HH:MM of boundary bar
    st_direction: float
    macd_histogram: float
    spot_close: float         # resampled candle close
    vwap_value: float
    pcr_value: float
    pcr_change: float

    # Entry
    entry_time: str
    entry_price: float        # option OPEN
    entry_lots: int           # 2

    # Partial exit (MACD flip) — empty strings / 0 if no partial
    partial_exit_time: str
    partial_exit_price: float
    partial_exit_lots: int
    partial_exit_pnl: float

    # Final exit
    exit_time: str
    exit_price: float
    exit_lots: int            # remaining lots
    exit_reason: str          # ST_FLIP, MACD_FLIP, TP, SL, EOD

    # Combined P&L
    pnl_points: float         # total_pnl / (entry_lots * lot_size)
    pnl_pct: float            # total_pnl / (entry_price * entry_lots * lot_size) * 100
    pnl_inr: float            # partial_pnl + final_pnl
```

---

## UI Parameters

| Parameter | Widget | Default | Key |
|-----------|--------|---------|-----|
| Start date | date_input | 2025-01-01 | prt_start |
| End date | date_input | 2026-04-02 | prt_end |
| Spot+PCR file | text_input | data/spot/nifty/NIFTY_1m_pcr.parquet | prt_file |
| Spot timeframe | selectbox [3, 5] | 5 | prt_tf |
| ST period | number_input | 10 | prt_st_p |
| ST factor | number_input | 3.0 | prt_st_f |
| MACD fast | number_input | 12 | prt_macd_f |
| MACD slow | number_input | 26 | prt_macd_s |
| MACD signal | number_input | 9 | prt_macd_sig |
| TP % | number_input | 30.0 | prt_tp |
| SL % | number_input | 30.0 | prt_sl |
| Lots per trade | number_input | 2 | prt_lots |
| PCR threshold | number_input | 1.0 | prt_pcr_th |
| PCR lookback (min) | number_input | 30 | prt_pcr_lb |
| Trading start | text_input | 09:30 | prt_t_start |
| Trading end | text_input | 14:30 | prt_t_end |
| Max trades/day | number_input | 0 (unlimited) | prt_max_trades |

### Results Display

- Key metrics: total trades, wins, losses, win rate, total P&L, avg P&L
- Exit reason breakdown: ST_FLIP, MACD_FLIP, TP, SL, EOD
- CE vs PE performance
- Partial exit stats (how many trades had MACD partial exits)
- Equity curve (cumulative P&L)
- Filterable trade table + CSV download
