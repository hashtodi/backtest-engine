"""Look-ahead-safe indicator entry (use_prev_bar_entry) — the BOOM-ST fix.

With the flag ON, indicator-level entry:
  - never fills on the signal bar (the signal is only confirmed at its close), and
  - rests the limit at the entry indicator's PREVIOUS-bar value (_prev),
    since the current bar's value isn't known until the bar closes.
With the flag OFF, the original same-bar behavior (current value) is preserved.
"""
import pandas as pd

from engine.backtest import BacktestEngine
from engine.trade import Trade

T_SIGNAL = pd.Timestamp("2026-01-31 11:25:00")
T_NEXT = pd.Timestamp("2026-01-31 11:26:00")


def _engine(use_prev_bar_entry):
    e = BacktestEngine.__new__(BacktestEngine)
    e.use_prev_bar_entry = use_prev_bar_entry
    e.entry_indicator = "opt_st"
    e._get_contract_candle = lambda trade, minute_data: minute_data  # stub
    return e


def _waiting_trade():
    return Trade(
        signal_time=T_SIGNAL,
        base_price=100.0,
        option_type="CE",
        strike=20000,
        expiry_type="weekly",
        expiry_code=0,
        instrument="NIFTY",
        direction="buy",
        entry_levels_config=[{"pct_from_base": 0.0, "capital_pct": 100.0}],
        lot_size=75,
    )


def _candle(low, high, st_curr, st_prev):
    return {"low": low, "high": high, "close": (low + high) / 2,
            "opt_st": st_curr, "opt_st_prev": st_prev}


# ---------------- flag ON ----------------

def test_no_fill_on_signal_bar_even_if_touched():
    """Signal bar: low(80) is below prev ST(90), but we still do NOT enter."""
    e, tr = _engine(True), _waiting_trade()
    e._check_indicator_entry(tr, _candle(80, 99, st_curr=100, st_prev=90), T_SIGNAL)
    assert tr.status == "WAITING_ENTRY"
    assert len(tr.parts) == 0


def test_next_bar_no_touch_no_fill():
    """Next bar: low(95) stayed above prev ST(90) -> no fill."""
    e, tr = _engine(True), _waiting_trade()
    e._check_indicator_entry(tr, _candle(95, 105, st_curr=100, st_prev=90), T_NEXT)
    assert tr.status == "WAITING_ENTRY"
    assert len(tr.parts) == 0


def test_next_bar_fill_uses_prev_bar_value():
    """Next bar: low(88) reaches prev ST(90) -> fill AT 90 (prev), not 100 (curr)."""
    e, tr = _engine(True), _waiting_trade()
    e._check_indicator_entry(tr, _candle(88, 99, st_curr=100, st_prev=90), T_NEXT)
    assert len(tr.parts) == 1
    assert tr.get_avg_entry_price() == 90.0
    assert tr.last_entry_time == T_NEXT


# ---------------- flag OFF (regression) ----------------

def test_flag_off_fills_same_bar_with_current_value():
    """Original behavior: signal bar, current ST(100) within range -> fill at 100."""
    e, tr = _engine(False), _waiting_trade()
    e._check_indicator_entry(tr, _candle(88, 105, st_curr=100, st_prev=90), T_SIGNAL)
    assert len(tr.parts) == 1
    assert tr.get_avg_entry_price() == 100.0
