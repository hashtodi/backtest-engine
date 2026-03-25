# Indicator-Level Exits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add indicator-based and ratio-based exit sources (SL/TP) alongside existing percentage exits, with auto-migration for old configs.

**Architecture:** New `parse_exit_config()` function in `engine/trade.py` normalizes all exit config formats (old flat fields, new structured dict) into a standard `exit_config` dict. A new `resolve_exit_levels()` function in `engine/backtest.py` computes SL/TP prices each minute from the config + current indicator values. The existing `_check_exit()` method calls `resolve_exit_levels()` instead of hardcoding percentage math.

**Tech Stack:** Python, pandas (existing stack, no new dependencies)

**Spec:** `docs/superpowers/specs/2026-03-25-indicator-level-exits-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `engine/trade.py` | Modify | Add `parse_exit_config()` to normalize exit config from strategy dict |
| `engine/backtest.py` | Modify | Add `resolve_exit_levels()`, refactor `_check_exit()`, update `_get_track_status()`, update `__init__()` |
| `engine/reporter.py` | Modify | Update SL/TP display in trade log and summary to read new exit config |
| `engine/detailed_logger.py` | Modify | Update SL/TP header display to read new exit config |
| `ui/strategy_form.py` | Modify | New `render_risk()` with source selection (percentage/indicator/ratio) |
| `ui/backtest_runner.py` | Modify | Update `_build_strategy()` and `_load_strategy_into_form()` for exit config |
| `ui/strategy_store.py` | Modify | Update `render_strategy_description()` for new exit display |
| `tests/test_exit_config.py` | Create | Unit tests for `parse_exit_config()` and `resolve_exit_levels()` |

**Out of scope (follow-up):** `forward/engine.py`, `forward/tick_checker.py`, `forward_test_runner.py`, `telegram_notifier.py` — these read flat `stop_loss_pct`/`target_pct`. Strategy dict always includes backward-compat flat fields, so they keep working for percentage exits. Indicator/ratio exits in forward testing require a separate change.

---

### Task 1: Baseline — capture current backtest output

Before any code changes, save a baseline output to diff against later.

**Files:**
- None modified

- [ ] **Step 1: Run existing strategy backtest and save output**

```bash
cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy
python run_backtest.py --strategy rsi_70_sell --instrument NIFTY 2>&1 | tail -20
```

- [ ] **Step 2: Copy baseline output**

```bash
cp backtest_results_NIFTY.csv backtest_results_NIFTY_baseline.csv
```

This baseline will be diffed after implementation to verify zero regression.

---

### Task 2: Add `parse_exit_config()` to `engine/trade.py`

**Files:**
- Modify: `engine/trade.py` (add function after `parse_entry_config()`, around line 52)
- Test: `tests/test_exit_config.py`

- [ ] **Step 1: Write failing tests for parse_exit_config**

Create `tests/test_exit_config.py`:

```python
"""Tests for exit config parsing and migration."""
import pytest
from engine.trade import parse_exit_config


class TestParseExitConfig:
    """Test parse_exit_config() normalizes all config formats."""

    def test_old_flat_config_migrates(self):
        """Old strategies with stop_loss_pct/target_pct auto-migrate."""
        strategy = {"stop_loss_pct": 20.0, "target_pct": 10.0}
        cfg = parse_exit_config(strategy)
        assert cfg["stop_loss"]["source"] == "percentage"
        assert cfg["stop_loss"]["value"] == 20.0
        assert cfg["target"]["source"] == "percentage"
        assert cfg["target"]["value"] == 10.0

    def test_old_config_with_defaults(self):
        """Old strategy with missing fields uses defaults."""
        strategy = {}
        cfg = parse_exit_config(strategy)
        assert cfg["stop_loss"]["source"] == "percentage"
        assert cfg["stop_loss"]["value"] == 20
        assert cfg["target"]["source"] == "percentage"
        assert cfg["target"]["value"] == 10

    def test_new_percentage_config(self):
        """New-format percentage config passes through."""
        strategy = {
            "exit": {
                "stop_loss": {"source": "percentage", "value": 15.0},
                "target": {"source": "percentage", "value": 8.0},
            }
        }
        cfg = parse_exit_config(strategy)
        assert cfg["stop_loss"]["value"] == 15.0
        assert cfg["target"]["value"] == 8.0

    def test_indicator_config(self):
        """Indicator source preserves indicator name."""
        strategy = {
            "exit": {
                "stop_loss": {"source": "indicator", "indicator": "opt_st_3_10_value"},
                "target": {"source": "ratio", "multiplier": 2.0},
            }
        }
        cfg = parse_exit_config(strategy)
        assert cfg["stop_loss"]["source"] == "indicator"
        assert cfg["stop_loss"]["indicator"] == "opt_st_3_10_value"
        assert cfg["target"]["source"] == "ratio"
        assert cfg["target"]["multiplier"] == 2.0

    def test_both_ratio_raises(self):
        """Both SL and TP as ratio is invalid (circular)."""
        strategy = {
            "exit": {
                "stop_loss": {"source": "ratio", "multiplier": 0.5},
                "target": {"source": "ratio", "multiplier": 2.0},
            }
        }
        with pytest.raises(ValueError, match="both.*ratio"):
            parse_exit_config(strategy)

    def test_old_config_with_exit_key_uses_exit(self):
        """If 'exit' key exists, ignore flat stop_loss_pct/target_pct."""
        strategy = {
            "stop_loss_pct": 20.0,
            "target_pct": 10.0,
            "exit": {
                "stop_loss": {"source": "percentage", "value": 30.0},
                "target": {"source": "percentage", "value": 5.0},
            }
        }
        cfg = parse_exit_config(strategy)
        assert cfg["stop_loss"]["value"] == 30.0
        assert cfg["target"]["value"] == 5.0

    def test_straddle_rejects_indicator_exit(self):
        """Straddle mode cannot use indicator/ratio exits."""
        strategy = {
            "trade_mode": "straddle",
            "exit": {
                "stop_loss": {"source": "indicator", "indicator": "opt_st_3_10_value"},
                "target": {"source": "percentage", "value": 10.0},
            }
        }
        with pytest.raises(ValueError, match="straddle"):
            parse_exit_config(strategy)

    def test_straddle_allows_percentage_exit(self):
        """Straddle mode works fine with percentage exits."""
        strategy = {
            "trade_mode": "straddle",
            "exit": {
                "stop_loss": {"source": "percentage", "value": 20.0},
                "target": {"source": "percentage", "value": 10.0},
            }
        }
        cfg = parse_exit_config(strategy)
        assert cfg["stop_loss"]["source"] == "percentage"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_exit_config.py -v
```

Expected: FAIL — `parse_exit_config` not found.

- [ ] **Step 3: Implement parse_exit_config**

Add to `engine/trade.py` after `parse_entry_config()` (after line 51):

```python
def parse_exit_config(strategy: Dict) -> Dict:
    """
    Parse exit config from strategy, with auto-migration for old format.

    Old format (flat fields):
        {"stop_loss_pct": 20, "target_pct": 10}

    New format (structured):
        {"exit": {"stop_loss": {"source": "percentage", "value": 20}, ...}}

    Returns normalized dict:
        {"stop_loss": {"source": ..., ...}, "target": {"source": ..., ...}}
    """
    if "exit" in strategy:
        cfg = strategy["exit"]
    else:
        # Auto-migrate old flat config
        cfg = {
            "stop_loss": {
                "source": "percentage",
                "value": strategy.get("stop_loss_pct", 20),
            },
            "target": {
                "source": "percentage",
                "value": strategy.get("target_pct", 10),
            },
        }

    sl = cfg.get("stop_loss", {"source": "percentage", "value": 20})
    tp = cfg.get("target", {"source": "percentage", "value": 10})

    # Validate: both can't be ratio (circular dependency)
    if sl.get("source") == "ratio" and tp.get("source") == "ratio":
        raise ValueError("Exit config invalid: both stop_loss and target cannot be 'ratio' (circular)")

    # Validate: straddle mode cannot use indicator/ratio exits
    if strategy.get("trade_mode") == "straddle":
        for label, side in [("stop_loss", sl), ("target", tp)]:
            if side.get("source") in ("indicator", "ratio"):
                raise ValueError(
                    f"Exit config invalid: {label} source '{side['source']}' "
                    f"not supported in straddle mode"
                )

    return {"stop_loss": sl, "target": tp}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_exit_config.py -v
```

Expected: All PASS.

---

### Task 3: Add `resolve_exit_levels()` to `engine/backtest.py`

**Files:**
- Modify: `engine/backtest.py` (add standalone function before the class)
- Test: `tests/test_exit_config.py` (add new test class)

- [ ] **Step 1: Write failing tests for resolve_exit_levels**

Append to `tests/test_exit_config.py`:

```python
from engine.backtest import resolve_exit_levels
import math


class TestResolveExitLevels:
    """Test resolve_exit_levels() computes SL/TP prices correctly."""

    def test_percentage_sell(self):
        """Percentage exit for sell direction."""
        cfg = {
            "stop_loss": {"source": "percentage", "value": 20.0},
            "target": {"source": "percentage", "value": 10.0},
        }
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, {})
        assert sl == pytest.approx(120.0)
        assert tp == pytest.approx(90.0)

    def test_percentage_buy(self):
        """Percentage exit for buy direction."""
        cfg = {
            "stop_loss": {"source": "percentage", "value": 20.0},
            "target": {"source": "percentage", "value": 10.0},
        }
        sl, tp = resolve_exit_levels(100.0, "buy", cfg, {})
        assert sl == pytest.approx(80.0)
        assert tp == pytest.approx(110.0)

    def test_indicator_sell_sl(self):
        """Indicator SL for sell — returns indicator value."""
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "percentage", "value": 10.0},
        }
        row = {"opt_st_value": 115.0}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert sl == pytest.approx(115.0)
        assert tp == pytest.approx(90.0)

    def test_indicator_nan_returns_none(self):
        """NaN indicator value returns None for that side."""
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "percentage", "value": 10.0},
        }
        row = {"opt_st_value": float("nan")}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert sl is None
        assert tp == pytest.approx(90.0)

    def test_indicator_wrong_side_returns_none(self):
        """Indicator on wrong side of entry returns None."""
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "percentage", "value": 10.0},
        }
        # For sell, SL must be > avg_entry. 95 < 100, so wrong side.
        row = {"opt_st_value": 95.0}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert sl is None

    def test_ratio_tp_from_percentage_sl(self):
        """TP = 2x SL distance (SL is percentage)."""
        cfg = {
            "stop_loss": {"source": "percentage", "value": 10.0},
            "target": {"source": "ratio", "multiplier": 2.0},
        }
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, {})
        # SL = 110 (10% above), distance = 10, TP = 100 - 20 = 80
        assert sl == pytest.approx(110.0)
        assert tp == pytest.approx(80.0)

    def test_ratio_tp_from_indicator_sl(self):
        """TP = 2x SL distance (SL is indicator)."""
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "ratio", "multiplier": 2.0},
        }
        row = {"opt_st_value": 110.0}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        # distance = 10, TP = 100 - 20 = 80
        assert sl == pytest.approx(110.0)
        assert tp == pytest.approx(80.0)

    def test_ratio_returns_none_when_anchor_none(self):
        """Ratio returns None when anchor indicator is NaN."""
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "ratio", "multiplier": 2.0},
        }
        row = {"opt_st_value": float("nan")}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert sl is None
        assert tp is None

    def test_ratio_sl_from_tp_buy(self):
        """SL = TP_distance * 0.5 for buy direction."""
        cfg = {
            "stop_loss": {"source": "ratio", "multiplier": 0.5},
            "target": {"source": "percentage", "value": 20.0},
        }
        sl, tp = resolve_exit_levels(100.0, "buy", cfg, {})
        # TP = 120, distance = 20, SL = 100 - (20 * 0.5) = 90
        assert tp == pytest.approx(120.0)
        assert sl == pytest.approx(90.0)

    def test_indicator_missing_key_returns_none(self):
        """Missing indicator key in row returns None."""
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "percentage", "value": 10.0},
        }
        row = {}  # indicator not in row
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert sl is None

    def test_indicator_tp_sell(self):
        """Indicator TP for sell — returns indicator value below entry."""
        cfg = {
            "stop_loss": {"source": "percentage", "value": 20.0},
            "target": {"source": "indicator", "indicator": "opt_bb_lower"},
        }
        row = {"opt_bb_lower": 85.0}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert tp == pytest.approx(85.0)

    def test_indicator_tp_sell_wrong_side(self):
        """Indicator TP above entry for sell returns None."""
        cfg = {
            "stop_loss": {"source": "percentage", "value": 20.0},
            "target": {"source": "indicator", "indicator": "opt_bb_lower"},
        }
        row = {"opt_bb_lower": 105.0}  # above entry for sell TP = wrong side
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert tp is None

    def test_indicator_buy_sl_wrong_side(self):
        """Indicator SL above entry for buy returns None (SL should be below)."""
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "percentage", "value": 10.0},
        }
        row = {"opt_st_value": 105.0}  # above entry for buy SL = wrong side
        sl, tp = resolve_exit_levels(100.0, "buy", cfg, row)
        assert sl is None

    def test_indicator_buy_sl_correct_side(self):
        """Indicator SL below entry for buy works."""
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "percentage", "value": 10.0},
        }
        row = {"opt_st_value": 90.0}
        sl, tp = resolve_exit_levels(100.0, "buy", cfg, row)
        assert sl == pytest.approx(90.0)
        assert tp == pytest.approx(110.0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_exit_config.py::TestResolveExitLevels -v
```

Expected: FAIL — `resolve_exit_levels` not found.

- [ ] **Step 3: Implement resolve_exit_levels**

Add to `engine/backtest.py` before the `BacktestEngine` class (after imports):

```python
import math


def resolve_exit_levels(avg_entry, direction, exit_config, indicator_row):
    """
    Compute SL and TP price levels for this minute.

    Args:
        avg_entry: weighted average entry price
        direction: "sell" or "buy"
        exit_config: normalized dict from parse_exit_config()
        indicator_row: dict-like row with indicator values (or {} if N/A)

    Returns:
        (sl_level, tp_level) — either can be None if indicator is NaN/wrong-side
    """
    sl_cfg = exit_config["stop_loss"]
    tp_cfg = exit_config["target"]

    # Determine which is anchor (non-ratio) and which is derived (ratio)
    # Resolve anchor first, then derived
    if sl_cfg["source"] == "ratio":
        # TP is anchor, SL is derived
        tp_level = _resolve_one_side(tp_cfg, avg_entry, direction, "target", indicator_row)
        if tp_level is None:
            return None, None
        anchor_distance = abs(avg_entry - tp_level)
        multiplier = sl_cfg.get("multiplier", 1.0)
        derived_distance = anchor_distance * multiplier
        if direction == "sell":
            sl_level = avg_entry + derived_distance
        else:
            sl_level = avg_entry - derived_distance
        return sl_level, tp_level

    elif tp_cfg["source"] == "ratio":
        # SL is anchor, TP is derived
        sl_level = _resolve_one_side(sl_cfg, avg_entry, direction, "stop_loss", indicator_row)
        if sl_level is None:
            return None, None
        anchor_distance = abs(avg_entry - sl_level)
        multiplier = tp_cfg.get("multiplier", 1.0)
        derived_distance = anchor_distance * multiplier
        if direction == "sell":
            tp_level = avg_entry - derived_distance
        else:
            tp_level = avg_entry + derived_distance
        return sl_level, tp_level

    else:
        # Both are independent (percentage or indicator)
        sl_level = _resolve_one_side(sl_cfg, avg_entry, direction, "stop_loss", indicator_row)
        tp_level = _resolve_one_side(tp_cfg, avg_entry, direction, "target", indicator_row)
        return sl_level, tp_level


def _resolve_one_side(side_cfg, avg_entry, direction, side_type, indicator_row):
    """
    Resolve one exit side (SL or TP) to a price level.

    Args:
        side_cfg: {"source": "percentage"|"indicator", ...}
        avg_entry: weighted average entry price
        direction: "sell" or "buy"
        side_type: "stop_loss" or "target"
        indicator_row: dict-like with indicator values

    Returns:
        float price level, or None if unavailable
    """
    source = side_cfg["source"]

    if source == "percentage":
        value = side_cfg.get("value", 20 if side_type == "stop_loss" else 10)
        if side_type == "stop_loss":
            if direction == "sell":
                return avg_entry * (1 + value / 100)
            else:
                return avg_entry * (1 - value / 100)
        else:  # target
            if direction == "sell":
                return avg_entry * (1 - value / 100)
            else:
                return avg_entry * (1 + value / 100)

    elif source == "indicator":
        ind_name = side_cfg.get("indicator", "")
        val = indicator_row.get(ind_name) if ind_name else None
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return None

        # Wrong-side guard
        if side_type == "stop_loss":
            if direction == "sell" and val <= avg_entry:
                return None
            if direction == "buy" and val >= avg_entry:
                return None
        else:  # target
            if direction == "sell" and val >= avg_entry:
                return None
            if direction == "buy" and val <= avg_entry:
                return None

        return val

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_exit_config.py -v
```

Expected: All PASS.

---

### Task 4: Refactor `BacktestEngine.__init__()` and `_check_exit()`

This is the core change. Replace hardcoded `self.stop_loss_pct` / `self.target_pct` with `self.exit_config` and use `resolve_exit_levels()`.

**Files:**
- Modify: `engine/backtest.py:39-96` (`__init__`)
- Modify: `engine/backtest.py:209-314` (`_check_exit`)
- Modify: `engine/backtest.py:354-392` (`_get_track_status`)

- [ ] **Step 1: Update `__init__` to parse exit config**

In `engine/backtest.py`, in `__init__()`:

Update the import on line 23 from:
```python
from engine.trade import Trade, StraddleTrade, parse_entry_config
```
To:
```python
from engine.trade import Trade, StraddleTrade, parse_entry_config, parse_exit_config
```

Then replace lines 64-66:
```python
        # SL / TP from strategy config
        self.stop_loss_pct = strategy.get('stop_loss_pct', 20)
        self.target_pct = strategy.get('target_pct', 10)
```

With:
```python
        # Exit config: percentage, indicator, or ratio
        self.exit_config = parse_exit_config(strategy)

        # Keep flat values for straddle mode (which still uses percentage only)
        self.stop_loss_pct = strategy.get('stop_loss_pct', 20)
        self.target_pct = strategy.get('target_pct', 10)
```

Update the logger line (line 90-96) to show exit config:
```python
        # Build SL/TP display string for logging
        sl_src = self.exit_config["stop_loss"]["source"]
        tp_src = self.exit_config["target"]["source"]
        if sl_src == "percentage":
            sl_display = f"{self.exit_config['stop_loss']['value']}%"
        elif sl_src == "indicator":
            sl_display = f"indicator({self.exit_config['stop_loss']['indicator']})"
        else:
            sl_display = f"ratio({self.exit_config['stop_loss']['multiplier']}x)"
        if tp_src == "percentage":
            tp_display = f"{self.exit_config['target']['value']}%"
        elif tp_src == "indicator":
            tp_display = f"indicator({self.exit_config['target']['indicator']})"
        else:
            tp_display = f"ratio({self.exit_config['target']['multiplier']}x)"

        logger.info(f"Initialized backtest for {instrument} | "
                     f"direction={self.direction} | "
                     f"trade_mode={self.trade_mode} | "
                     f"expiry_mode={self.expiry_mode} | "
                     f"SL={sl_display} | TP={tp_display} | "
                     f"max_trades/day={self.max_trades_per_day or 'unlimited'} | "
                     f"max_sl/day={self.max_sl_per_day or 'unlimited'}")
```

- [ ] **Step 2: Refactor `_check_exit()` to use `resolve_exit_levels()`**

Replace the SL/TP computation block in `_check_exit()` (lines 265-298):

```python
        # Resolve dynamic SL/TP levels
        sl_price, tp_price = resolve_exit_levels(
            avg_entry, self.direction, self.exit_config, candle
        )

        if self.direction == 'sell':
            if sl_price is not None and candle['high'] >= sl_price:
                exit_reason = 'STOP_LOSS'
                exit_price = sl_price
                pnl_pct = -((sl_price - avg_entry) / avg_entry) * 100
                msg = (f"EXIT STOP_LOSS: high={candle['high']:.2f} >= "
                       f"SL={sl_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {sl_price:.2f} | pnl={pnl_pct:+.2f}%")
            elif tp_price is not None and candle['low'] <= tp_price:
                exit_reason = 'TARGET'
                exit_price = tp_price
                pnl_pct = ((avg_entry - tp_price) / avg_entry) * 100
                msg = (f"EXIT TARGET: low={candle['low']:.2f} <= "
                       f"TP={tp_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {tp_price:.2f} | pnl=+{pnl_pct:.2f}%")
        else:
            if sl_price is not None and candle['low'] <= sl_price:
                exit_reason = 'STOP_LOSS'
                exit_price = sl_price
                pnl_pct = -((avg_entry - sl_price) / avg_entry) * 100
                msg = (f"EXIT STOP_LOSS: low={candle['low']:.2f} <= "
                       f"SL={sl_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {sl_price:.2f} | pnl={pnl_pct:+.2f}%")
            elif tp_price is not None and candle['high'] >= tp_price:
                exit_reason = 'TARGET'
                exit_price = tp_price
                pnl_pct = ((tp_price - avg_entry) / avg_entry) * 100
                msg = (f"EXIT TARGET: high={candle['high']:.2f} >= "
                       f"TP={tp_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {tp_price:.2f} | pnl=+{pnl_pct:.2f}%")
```

- [ ] **Step 3: Update `_get_track_status()` to use `resolve_exit_levels()`**

Replace the hardcoded SL/TP calculation in `_get_track_status()` (lines 379-392):

```python
        elif trade.status in ('PARTIAL_POSITION', 'FULL_POSITION'):
            avg = trade.get_avg_entry_price()
            n_filled = len(trade.parts)
            n_total = trade.num_levels
            sl, tp = resolve_exit_levels(
                avg, self.direction, self.exit_config, candle if candle is not None else {}
            )
            sl_str = f"{sl:.2f}" if sl is not None else "N/A"
            tp_str = f"{tp:.2f}" if tp is not None else "N/A"
            return (f"in position {strike} {opt} ({n_filled}/{n_total}) | "
                    f"{price_str} | avg={avg:.2f} SL={sl_str} TP={tp_str}")
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_exit_config.py -v
```

Expected: All PASS.

---

### Task 5: Regression test — verify old strategy output is identical

**Files:**
- None modified

- [ ] **Step 1: Run the same backtest as baseline**

```bash
cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy
python run_backtest.py --strategy rsi_70_sell --instrument NIFTY 2>&1 | tail -20
```

- [ ] **Step 2: Diff against baseline**

```bash
diff backtest_results_NIFTY_baseline.csv backtest_results_NIFTY.csv
```

Expected: No differences in CSV output. Log format may differ slightly (dynamic pnl% calculation vs hardcoded) — this is expected and acceptable. Only the CSV data (trades, prices, P&L) must match exactly.

- [ ] **Step 3: Clean up baseline file**

```bash
rm backtest_results_NIFTY_baseline.csv
```

---

### Task 6: Update UI — `render_risk()` in `strategy_form.py`

**Files:**
- Modify: `ui/strategy_form.py:82-91` (init_state risk section)
- Modify: `ui/strategy_form.py:433-452` (render_risk function)

- [ ] **Step 1: Add new session state defaults in `init_state()`**

Replace the risk section in `init_state()` (lines 82-90):

```python
    # Risk management — exit config
    # SL source: "Percentage", "Indicator", "Ratio"
    if "bt_sl_source" not in st.session_state:
        st.session_state.bt_sl_source = "Percentage"
    if "bt_sl_on" not in st.session_state:
        st.session_state.bt_sl_on = True
    if "bt_sl_pct" not in st.session_state:
        st.session_state.bt_sl_pct = 15.0
    if "bt_sl_indicator" not in st.session_state:
        st.session_state.bt_sl_indicator = ""
    if "bt_sl_multiplier" not in st.session_state:
        st.session_state.bt_sl_multiplier = 0.5

    # TP source: "Percentage", "Indicator", "Ratio"
    if "bt_tp_source" not in st.session_state:
        st.session_state.bt_tp_source = "Percentage"
    if "bt_tp_on" not in st.session_state:
        st.session_state.bt_tp_on = True
    if "bt_tp_pct" not in st.session_state:
        st.session_state.bt_tp_pct = 10.0
    if "bt_tp_indicator" not in st.session_state:
        st.session_state.bt_tp_indicator = ""
    if "bt_tp_multiplier" not in st.session_state:
        st.session_state.bt_tp_multiplier = 2.0
```

- [ ] **Step 2: Rewrite `render_risk()` with source selection**

Replace `render_risk()`:

```python
def render_risk():
    """SL/TP with source selection: Percentage, Indicator, or Ratio."""
    st.markdown("##### Risk Management")

    available = get_available_columns(st.session_state.bt_indicators)
    is_straddle = st.session_state.get("bt_trade_mode") == "Straddle"

    # Source options depend on mode
    if is_straddle:
        sources = ["Percentage"]
        st.caption("Straddle mode: only percentage exits supported.")
    else:
        sources = ["Percentage", "Indicator", "Ratio"]

    c1, c2 = st.columns(2)

    with c1:
        sl_on = st.toggle("Stop Loss", key="bt_sl_on")
        if sl_on:
            sl_source = st.selectbox("SL Source", sources, key="bt_sl_source")
            if sl_source == "Percentage":
                st.number_input(
                    "SL %", min_value=0.1, max_value=100.0,
                    step=0.5, format="%.1f", key="bt_sl_pct",
                )
            elif sl_source == "Indicator":
                if available:
                    default = st.session_state.bt_sl_indicator
                    if default not in available:
                        default = available[0]
                        st.session_state.bt_sl_indicator = default
                    st.session_state.bt_sl_indicator = st.selectbox(
                        "SL Indicator", available,
                        index=available.index(default),
                        key="_bt_sl_ind_sel",
                    )
                else:
                    st.warning("Add indicators first.")
            elif sl_source == "Ratio":
                st.number_input(
                    "SL = TP distance ×",
                    min_value=0.1, max_value=10.0,
                    step=0.1, format="%.1f", key="bt_sl_multiplier",
                )

    with c2:
        tp_on = st.toggle("Take Profit", key="bt_tp_on")
        if tp_on:
            tp_source = st.selectbox("TP Source", sources, key="bt_tp_source")
            if tp_source == "Percentage":
                st.number_input(
                    "TP %", min_value=0.1, max_value=100.0,
                    step=0.5, format="%.1f", key="bt_tp_pct",
                )
            elif tp_source == "Indicator":
                if available:
                    default = st.session_state.bt_tp_indicator
                    if default not in available:
                        default = available[0]
                        st.session_state.bt_tp_indicator = default
                    st.session_state.bt_tp_indicator = st.selectbox(
                        "TP Indicator", available,
                        index=available.index(default),
                        key="_bt_tp_ind_sel",
                    )
                else:
                    st.warning("Add indicators first.")
            elif tp_source == "Ratio":
                st.number_input(
                    "TP = SL distance ×",
                    min_value=0.1, max_value=10.0,
                    step=0.1, format="%.1f", key="bt_tp_multiplier",
                )

    # Validation: both can't be ratio
    sl_source = st.session_state.get("bt_sl_source", "Percentage")
    tp_source = st.session_state.get("bt_tp_source", "Percentage")
    if sl_source == "Ratio" and tp_source == "Ratio":
        st.error("Both SL and TP cannot be Ratio (circular). Change one.")
```

- [ ] **Step 3: Verify the UI renders**

```bash
cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy
python -c "import ui.strategy_form; print('Import OK')"
```

Expected: "Import OK" — no import errors.

---

### Task 7: Update `_build_strategy()` and `_load_strategy_into_form()` in `backtest_runner.py`

**Files:**
- Modify: `ui/backtest_runner.py:503-597` (`_build_strategy`)
- Modify: `ui/backtest_runner.py:464-472` (`_load_strategy_into_form` risk section)
- Modify: `ui/backtest_runner.py:301-310` (`_clear_dynamic_widget_keys`)

- [ ] **Step 1: Update `_build_strategy()` exit config section**

Replace the SL/TP section (lines 551-553):

```python
    # -- Exit config --
    sl_on = st.session_state.get("bt_sl_on", True)
    tp_on = st.session_state.get("bt_tp_on", True)
    sl_source = st.session_state.get("bt_sl_source", "Percentage") if sl_on else None
    tp_source = st.session_state.get("bt_tp_source", "Percentage") if tp_on else None

    def _build_exit_side(source, prefix):
        if source is None:
            # Disabled = effectively 9999% (never triggers)
            return {"source": "percentage", "value": 9999}
        if source == "Percentage":
            return {"source": "percentage", "value": st.session_state.get(f"bt_{prefix}_pct", 15.0)}
        elif source == "Indicator":
            return {"source": "indicator", "indicator": st.session_state.get(f"bt_{prefix}_indicator", "")}
        elif source == "Ratio":
            return {"source": "ratio", "multiplier": st.session_state.get(f"bt_{prefix}_multiplier", 2.0)}
        return {"source": "percentage", "value": 9999}

    exit_cfg = {
        "stop_loss": _build_exit_side(sl_source, "sl"),
        "target": _build_exit_side(tp_source, "tp"),
    }
```

Then in the strategy_dict, replace:
```python
        "stop_loss_pct": sl,
        "target_pct": tp,
```
With:
```python
        "exit": exit_cfg,
```

Also keep backward-compat flat fields for straddle:
```python
        # Flat fields for backward compatibility (straddle mode reads these)
        "stop_loss_pct": exit_cfg["stop_loss"].get("value", 9999) if exit_cfg["stop_loss"]["source"] == "percentage" else 9999,
        "target_pct": exit_cfg["target"].get("value", 9999) if exit_cfg["target"]["source"] == "percentage" else 9999,
```

- [ ] **Step 2: Update `_load_strategy_into_form()` risk section**

Replace lines 464-472:

```python
    # --- Risk / Exit ---
    exit_cfg = strategy.get("exit")
    if exit_cfg:
        sl_cfg = exit_cfg.get("stop_loss", {})
        tp_cfg = exit_cfg.get("target", {})

        sl_src = sl_cfg.get("source", "percentage")
        tp_src = tp_cfg.get("source", "percentage")

        # SL
        sl_disabled = (sl_src == "percentage" and sl_cfg.get("value", 0) >= 9999)
        st.session_state.bt_sl_on = not sl_disabled
        st.session_state.bt_sl_source = sl_src.title()
        if sl_src == "percentage" and not sl_disabled:
            st.session_state.bt_sl_pct = float(sl_cfg.get("value", 15.0))
        elif sl_src == "indicator":
            st.session_state.bt_sl_indicator = sl_cfg.get("indicator", "")
        elif sl_src == "ratio":
            st.session_state.bt_sl_multiplier = float(sl_cfg.get("multiplier", 0.5))

        # TP
        tp_disabled = (tp_src == "percentage" and tp_cfg.get("value", 0) >= 9999)
        st.session_state.bt_tp_on = not tp_disabled
        st.session_state.bt_tp_source = tp_src.title()
        if tp_src == "percentage" and not tp_disabled:
            st.session_state.bt_tp_pct = float(tp_cfg.get("value", 10.0))
        elif tp_src == "indicator":
            st.session_state.bt_tp_indicator = tp_cfg.get("indicator", "")
        elif tp_src == "ratio":
            st.session_state.bt_tp_multiplier = float(tp_cfg.get("multiplier", 2.0))
    else:
        # Old format: flat stop_loss_pct / target_pct
        sl = strategy.get("stop_loss_pct", 20)
        tp = strategy.get("target_pct", 10)
        st.session_state.bt_sl_on = sl < 9999
        if sl < 9999:
            st.session_state.bt_sl_pct = float(sl)
        st.session_state.bt_sl_source = "Percentage"
        st.session_state.bt_tp_on = tp < 9999
        if tp < 9999:
            st.session_state.bt_tp_pct = float(tp)
        st.session_state.bt_tp_source = "Percentage"
```

- [ ] **Step 3: Add exit widget keys to `_clear_dynamic_widget_keys()`**

Add to `stale_prefixes` tuple in `_clear_dynamic_widget_keys()`:

```python
        "_bt_sl_ind_sel", "_bt_tp_ind_sel",  # exit indicator selectboxes
```

- [ ] **Step 4: Verify import + basic form flow**

```bash
cd /Users/hashtodi/Desktop/lemonn/RSI-Options-Trading-Strategy
python -c "from ui.backtest_runner import _build_strategy; print('OK')"
```

---

### Task 8: Update `render_strategy_description()` in `strategy_store.py`

**Files:**
- Modify: `ui/strategy_store.py:204-209`

- [ ] **Step 1: Update risk display for new exit config**

Replace lines 204-209:

```python
        # --- Risk ---
        exit_cfg = strategy.get("exit")
        if exit_cfg:
            sl_cfg = exit_cfg.get("stop_loss", {})
            tp_cfg = exit_cfg.get("target", {})

            def _exit_label(cfg):
                src = cfg.get("source", "percentage")
                if src == "percentage":
                    v = cfg.get("value", 0)
                    return "Off" if v >= 9999 else f"{v}%"
                elif src == "indicator":
                    return f"indicator({cfg.get('indicator', '?')})"
                elif src == "ratio":
                    return f"ratio({cfg.get('multiplier', 1)}x)"
                return "?"

            st.markdown(f"**SL:** {_exit_label(sl_cfg)} | **TP:** {_exit_label(tp_cfg)}")
        else:
            sl = strategy.get("stop_loss_pct", 0)
            tp = strategy.get("target_pct", 0)
            sl_str = "Off" if sl >= 9999 else f"{sl}%"
            tp_str = "Off" if tp >= 9999 else f"{tp}%"
            st.markdown(f"**SL:** {sl_str} | **TP:** {tp_str}")
```

---

### Task 9: Update `engine/reporter.py` and `engine/detailed_logger.py`

These files read flat `stop_loss_pct`/`target_pct` for display. Must handle new exit config format.

**Files:**
- Modify: `engine/reporter.py:201-202, 336-338`
- Modify: `engine/detailed_logger.py:47-48`

- [ ] **Step 1: Add `_exit_display_str()` helper to reporter.py**

Add a helper function near the top of `engine/reporter.py`:

```python
def _exit_display_str(strategy_config):
    """Build SL/TP display strings from strategy config (handles old and new format)."""
    exit_cfg = strategy_config.get("exit")
    if exit_cfg:
        def _label(cfg):
            src = cfg.get("source", "percentage")
            if src == "percentage":
                v = cfg.get("value", 0)
                return "Off" if v >= 9999 else f"{v}%"
            elif src == "indicator":
                return f"indicator({cfg.get('indicator', '?')})"
            elif src == "ratio":
                return f"ratio({cfg.get('multiplier', 1)}x)"
            return "?"
        sl_str = _label(exit_cfg.get("stop_loss", {}))
        tp_str = _label(exit_cfg.get("target", {}))
    else:
        sl = strategy_config.get("stop_loss_pct", 0)
        tp = strategy_config.get("target_pct", 0)
        sl_str = "Off" if sl >= 9999 else f"{sl}%"
        tp_str = "Off" if tp >= 9999 else f"{tp}%"
    return sl_str, tp_str
```

- [ ] **Step 2: Update reporter.py display lines**

Replace line 201-202:
```python
        sl_str, tp_str = _exit_display_str(strategy_config)
        f.write(f"SL: {sl_str} | TP: {tp_str}\n")
```

Replace lines 336-339:
```python
        sl_str, tp_str = _exit_display_str(strategy_config)
        f.write(f"- **Stop Loss**: {sl_str} (exact fill assumed)\n")
        f.write(f"- **Target**: {tp_str} (exact fill assumed)\n")
```

- [ ] **Step 3: Update detailed_logger.py display lines**

Replace lines 47-48:
```python
        sl_str, tp_str = _exit_display_str(cfg)
        self.file.write(f"SL: {sl_str} | TP: {tp_str}\n")
```

Add import at the top of `detailed_logger.py`:
```python
from engine.reporter import _exit_display_str
```

---

### Task 10: Final integration test

**Files:**
- None modified

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/ -v
```

Expected: All pass.

- [ ] **Step 2: Run existing strategy backtest — regression check**

```bash
python run_backtest.py --strategy rsi_70_sell --instrument NIFTY 2>&1 | tail -5
```

Verify trade count and P&L match previous runs.

- [ ] **Step 3: Test with a new indicator exit config**

Create a test strategy JSON manually (or via UI) with:
```json
{
  "exit": {
    "stop_loss": {"source": "indicator", "indicator": "opt_st_3_10_value"},
    "target": {"source": "ratio", "multiplier": 2.0}
  }
}
```

Run it and verify:
- SL triggers when candle high >= SuperTrend value
- TP = 2x the SL distance from entry
- Both move dynamically

- [ ] **Step 4: Test edge cases**

Verify these scenarios in the backtest output:
- NaN indicator during warmup: no premature exit
- Indicator on wrong side: exit skipped that minute
- EOD still closes regardless of indicator state
