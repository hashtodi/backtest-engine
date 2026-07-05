"""Entry-candle exit behavior for single-leg trades.

On the candle where a single-leg position is filled (intrabar, at an indicator
level or staggered limit), the engine evaluates the stop-loss conservatively
but never the target:
  - both SL & TP touched -> STOP_LOSS
  - only SL touched       -> STOP_LOSS
  - only TP touched       -> nothing fires (held)

On any later candle both are live, and SL still wins ties.
See BacktestEngine._check_exit.
"""
from datetime import time as dt_time

import pandas as pd
import pytest

from engine.backtest import BacktestEngine
from engine.trade import Trade

T_ENTRY = pd.Timestamp("2026-01-31 11:00:00")
T_LATER = pd.Timestamp("2026-01-31 11:05:00")

# entry_price = 100  ->  buy: SL=95, TP=107.5   |   sell: SL=105, TP=92.5
EXIT_CFG = {
    "stop_loss": {"source": "percentage", "value": 5.0},
    "target": {"source": "percentage", "value": 7.5},
}


def _make_engine(direction="buy"):
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.direction = direction
    engine.exit_config = EXIT_CFG
    engine.max_loss_pct_per_day = None
    engine.exit_time = dt_time(15, 20)
    engine.trades = []
    # Stub the contract-candle lookup: return whatever is passed as minute_data,
    # so tests can hand a candle dict straight in.
    engine._get_contract_candle = lambda trade, minute_data: minute_data
    return engine


def _make_filled_trade(direction="buy", entry_time=T_ENTRY, entry_price=100.0):
    trade = Trade(
        signal_time=entry_time,
        base_price=entry_price,
        option_type="CE",
        strike=20000,
        expiry_type="weekly",
        expiry_code=0,
        instrument="NIFTY",
        direction=direction,
        entry_levels_config=[{"pct_from_base": 0.0, "capital_pct": 100.0}],
        lot_size=75,
    )
    trade.add_entry(trade.entry_levels[0], entry_time, entry_price)
    return trade


def _candle(low, high):
    return {"low": low, "high": high, "close": (low + high) / 2}


# ---------------- ENTRY CANDLE (t == last_entry_time) ----------------

def test_entry_candle_both_touched_fires_sl():
    """Buy: candle dips below SL(95) and spikes above TP(107.5) -> SL wins."""
    engine = _make_engine("buy")
    trade = _make_filled_trade(entry_time=T_ENTRY)
    closed, _ = engine._check_exit(trade, _candle(94.0, 108.0), None, T_ENTRY, False, 0.0)
    assert closed is True
    assert trade.exit_reason == "STOP_LOSS"
    assert trade.exit_price == pytest.approx(95.0)


def test_entry_candle_sl_only_fires_sl():
    engine = _make_engine("buy")
    trade = _make_filled_trade(entry_time=T_ENTRY)
    closed, _ = engine._check_exit(trade, _candle(94.0, 105.0), None, T_ENTRY, False, 0.0)
    assert closed is True
    assert trade.exit_reason == "STOP_LOSS"


def test_entry_candle_tp_only_does_not_fire():
    """Target is suppressed on the entry candle -> trade held."""
    engine = _make_engine("buy")
    trade = _make_filled_trade(entry_time=T_ENTRY)
    closed, _ = engine._check_exit(trade, _candle(97.0, 108.0), None, T_ENTRY, False, 0.0)
    assert closed is False
    assert trade.exit_reason is None


def test_entry_candle_sell_both_touched_fires_sl():
    """Sell side: high>=SL(105) and low<=TP(92.5) -> SL wins."""
    engine = _make_engine("sell")
    trade = _make_filled_trade("sell", entry_time=T_ENTRY)
    closed, _ = engine._check_exit(trade, _candle(92.0, 106.0), None, T_ENTRY, False, 0.0)
    assert closed is True
    assert trade.exit_reason == "STOP_LOSS"


# ---------------- LATER CANDLE (t != last_entry_time) ----------------

def test_later_candle_both_touched_fires_sl():
    engine = _make_engine("buy")
    trade = _make_filled_trade(entry_time=T_ENTRY)
    closed, _ = engine._check_exit(trade, _candle(94.0, 108.0), None, T_LATER, False, 0.0)
    assert closed is True
    assert trade.exit_reason == "STOP_LOSS"


def test_later_candle_tp_only_fires_target():
    engine = _make_engine("buy")
    trade = _make_filled_trade(entry_time=T_ENTRY)
    closed, _ = engine._check_exit(trade, _candle(99.0, 108.0), None, T_LATER, False, 0.0)
    assert closed is True
    assert trade.exit_reason == "TARGET"
    assert trade.exit_price == pytest.approx(107.5)
