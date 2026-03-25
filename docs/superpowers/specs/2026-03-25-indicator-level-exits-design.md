# Indicator-Level Exits + Ratio-Based TP/SL

**Date:** 2026-03-25
**Status:** Approved

## Problem

Exit conditions (SL/TP) are fixed percentages from average entry price. Entry supports indicator-level dynamic pricing, but exit does not. There is no way to express risk-reward relationships like TP = 2x SL.

## Solution

Redesign exit config so SL and TP each have an independent **source**: `percentage`, `indicator`, or `ratio`. Auto-migrate old configs so existing strategies produce identical results.

## Exit Config Structure

Only the field relevant to the chosen `source` is required; others are ignored.

```json
// Example 1: Both percentage (equivalent to current behavior)
"exit": {
  "stop_loss": { "source": "percentage", "value": 20.0 },
  "target":    { "source": "percentage", "value": 10.0 }
}

// Example 2: SL = indicator, TP = 2x SL distance
"exit": {
  "stop_loss": { "source": "indicator", "indicator": "opt_st_3_10_value" },
  "target":    { "source": "ratio", "multiplier": 2.0 }
}

// Example 3: SL = percentage, TP = indicator
"exit": {
  "stop_loss": { "source": "percentage", "value": 20.0 },
  "target":    { "source": "indicator", "indicator": "opt_bb_20_2_lower" }
}
```

### Source Types

**percentage** (current behavior): Fixed % from avg_entry.
- `value` field required (e.g., 20.0 means 20%).

**indicator**: Dynamic level from a pre-computed indicator column.
- `indicator` field required (must match a configured indicator name).
- Level updates every minute as indicator value changes.
- If indicator is NaN (warmup), skip exit check that minute.
- **Wrong-side guard**: If indicator value is on the wrong side of avg_entry (e.g., SL indicator below entry for a SELL), skip that exit check for that minute. This naturally happens with SuperTrend during trend-flip candles.

**ratio**: Derived from the other exit's distance.
- `multiplier` field required (e.g., 2.0 means TP = 2x SL distance).
- Both SL and TP cannot be ratio (circular — validated at config load).
- Recomputed every minute if anchor is dynamic.
- If anchor returns None (NaN or wrong-side skip), ratio also returns None that minute.

### Auto-Migration Shim

Old flat configs convert transparently:

```python
if "stop_loss_pct" in config and "exit" not in config:
    config["exit"] = {
        "stop_loss": {"source": "percentage", "value": config.get("stop_loss_pct", 20)},
        "target": {"source": "percentage", "value": config.get("target_pct", 10)}
    }
```

Existing saved strategy JSONs are never modified on disk. Conversion happens at runtime in config parsing.

## Exit Check Logic

### Level Resolution (every minute)

```
resolve_exit_levels(avg_entry, direction, exit_config, indicator_row):
    # 1. Resolve the non-ratio exit first (anchor)
    # 2. Resolve the ratio exit from anchor distance

    For percentage:
        SELL SL = avg_entry * (1 + value/100)
        SELL TP = avg_entry * (1 - value/100)
        BUY SL = avg_entry * (1 - value/100)
        BUY TP = avg_entry * (1 + value/100)

    For indicator:
        level = indicator_row[indicator_name]
        if NaN: return None (skip check)
        Wrong-side check:
            SELL SL: level must be > avg_entry, else return None
            SELL TP: level must be < avg_entry, else return None
            BUY SL: level must be < avg_entry, else return None
            BUY TP: level must be > avg_entry, else return None

    For ratio:
        if anchor_level is None: return None (skip check)
        anchor_distance = abs(avg_entry - anchor_level)
        SELL SL = avg_entry + (anchor_distance * multiplier)
        SELL TP = avg_entry - (anchor_distance * multiplier)
        BUY SL = avg_entry - (anchor_distance * multiplier)
        BUY TP = avg_entry + (anchor_distance * multiplier)
```

### Trigger Conditions

Same directional logic as current fixed exits:

| Direction | SL triggers when | TP triggers when |
|-----------|-----------------|-----------------|
| SELL | candle HIGH >= SL level | candle LOW <= TP level |
| BUY | candle LOW <= SL level | candle HIGH >= TP level |

Exit price = the computed level (not candle close). Same convention as current percentage exits.

### Same-Candle SL+TP Conflict

If both SL and TP trigger on the same candle, SL wins (conservative, matches current behavior).

### Dynamic Ratio Example

SELL trade, entry at 100, SL = indicator, TP = 2x SL:

| Minute | Indicator (SL) | SL dist | TP level (2x) | TP dist |
|--------|---------------|---------|----------------|---------|
| 1 | 110 | 10 | 80 | 20 |
| 2 | 108 | 8 | 84 | 16 |
| 3 | 106 | 6 | 88 | 12 |

Both levels move every minute. Ratio stays constant.

## What Changes

| Component | Changes? | Details |
|-----------|----------|---------|
| `config.py` | Yes | Parse new exit config, auto-migration shim, validation |
| `engine/backtest.py` check_exit() | Yes | Resolve exit levels from config + contract candle row |
| `engine/backtest.py` _get_track_status() | Yes | Display current dynamic SL/TP levels in logs |
| `engine/backtest.py` __init__() | Yes | Replace `self.stop_loss_pct`/`self.target_pct` with `self.exit_config` |
| `engine/backtest.py` main loop | Minimal | Pass contract candle row to check_exit |
| `ui/form_config.py` | Yes | UI fields for exit source selection |
| `engine/signals.py` | No | |
| `engine/trade.py` | No | |
| `indicators/` | No | |
| Saved strategy JSONs | No | Auto-migration at runtime |

## Scope & Constraints

- Old strategy configs must produce byte-identical backtest output.
- EOD safety net unchanged — always closes at trading_end regardless of exit config.
- Exit not checked on same candle as entry (existing guard preserved).
- `zone_rsi_backtest.py` is a separate engine — not modified in this change.
- **Straddle mode out of scope**: Straddle uses combined CE+PE close-to-close exit semantics, which differs from single-leg high/low triggers. Config validator rejects `source: "indicator"` or `source: "ratio"` when `trade_mode == "straddle"`.
- **Indicator row source**: For exit checks, indicator values are read from the same contract candle row used for high/low trigger checks (`_get_contract_candle()`), consistent with indicator-level entry.
- **Staggered entry interaction**: When staggered entry parts fill, avg_entry shifts. Exit levels (all sources) recompute from the new avg_entry each minute. This is consistent with current percentage exit behavior.

## Verification Plan

1. Run existing strategy backtest, save output as baseline.
2. Implement changes.
3. Re-run same strategy — diff must be empty.
4. Test indicator exit with a SuperTrend-based SL config.
5. Test ratio exit with TP = 2x SL (percentage anchor).
6. Test ratio + indicator combo (SL = indicator, TP = 2x SL).
7. Test NaN indicator during warmup — verify no premature exits.
8. Test wrong-side indicator value — verify skip behavior.
9. Test straddle mode with indicator exit — verify config validation rejects it.
10. Test staggered entry + indicator exit — verify levels recompute on avg_entry shift.
