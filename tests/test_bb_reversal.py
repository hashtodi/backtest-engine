"""Tests for BBReversalTrade dataclass and SignalState machine."""
import pandas as pd
import pytest

from engine.bb_reversal_backtest import (
    BBReversalTrade,
    SignalState,
    check_signal_state,
    trades_to_dataframe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_trade(**overrides) -> BBReversalTrade:
    """Return a BBReversalTrade with sensible defaults, allowing field overrides."""
    defaults = dict(
        date="2026-04-07",
        spot_strike=22500.0,
        pe_strike=22500.0,
        expiry_date="2026-04-10",
        signal_step="break_red_low",
        signal_time="10:00",
        entry_time="10:05",
        entry_price=120.0,
        spot_at_entry=22480.0,
        qty=75,
        tp_level=135.0,
        sl_level=105.0,
        exit_time="10:22",
        exit_price=135.0,
        exit_reason="TP",
        pnl_points=15.0,
        pnl_pct=12.5,
        pnl_inr=1125.0,
    )
    defaults.update(overrides)
    return BBReversalTrade(**defaults)


def advance(state: SignalState, *, close, open_, high, low, bb_upper) -> bool:
    """Convenience wrapper for check_signal_state."""
    return check_signal_state(state, close, open_, high, low, bb_upper)


# ---------------------------------------------------------------------------
# Task 1: Trade dataclass and trades_to_dataframe
# ---------------------------------------------------------------------------

class TestBBReversalTradeDataclass:
    """Test that the dataclass is well-formed and holds the correct fields."""

    def test_all_fields_present(self):
        t = make_trade()
        assert t.date == "2026-04-07"
        assert t.spot_strike == 22500.0
        assert t.pe_strike == 22500.0
        assert t.expiry_date == "2026-04-10"
        assert t.signal_step == "break_red_low"
        assert t.entry_time == "10:05"
        assert t.entry_price == 120.0
        assert t.spot_at_entry == 22480.0
        assert t.qty == 75
        assert t.tp_level == 135.0
        assert t.sl_level == 105.0
        assert t.exit_time == "10:22"
        assert t.exit_price == 135.0
        assert t.exit_reason == "TP"
        assert t.pnl_points == 15.0
        assert t.pnl_pct == 12.5
        assert t.pnl_inr == 1125.0

    def test_tp_level_is_entry_plus_15(self):
        t = make_trade(entry_price=200.0, tp_level=215.0, sl_level=185.0)
        assert t.tp_level == t.entry_price + 15
        assert t.sl_level == t.entry_price - 15

    def test_pnl_inr_equals_points_times_qty(self):
        t = make_trade(pnl_points=15.0, qty=75, pnl_inr=1125.0)
        assert t.pnl_inr == t.pnl_points * t.qty

    def test_exit_reasons_accepted(self):
        for reason in ("TP", "SL", "EOD"):
            t = make_trade(exit_reason=reason)
            assert t.exit_reason == reason


class TestTradesToDataframe:
    """Test trades_to_dataframe converts lists correctly."""

    def test_empty_list_returns_empty_dataframe(self):
        df = trades_to_dataframe([])
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_single_trade_has_correct_columns(self):
        t = make_trade()
        df = trades_to_dataframe([t])
        assert len(df) == 1
        expected_cols = {
            "date", "spot_strike", "pe_strike", "expiry_date", "signal_step",
            "entry_time", "entry_price", "spot_at_entry", "qty",
            "tp_level", "sl_level", "exit_time", "exit_price", "exit_reason",
            "pnl_points", "pnl_pct", "pnl_inr",
        }
        assert expected_cols.issubset(set(df.columns))

    def test_single_trade_values_match(self):
        t = make_trade(entry_price=100.0, exit_price=115.0, pnl_points=15.0)
        df = trades_to_dataframe([t])
        assert df.iloc[0]["entry_price"] == 100.0
        assert df.iloc[0]["exit_price"] == 115.0
        assert df.iloc[0]["pnl_points"] == 15.0

    def test_multiple_trades(self):
        trades = [make_trade(date=f"2026-04-0{i}") for i in range(1, 4)]
        df = trades_to_dataframe(trades)
        assert len(df) == 3


# ---------------------------------------------------------------------------
# Task 2: SignalState machine
# ---------------------------------------------------------------------------

class TestSignalStateIdle:
    """IDLE phase behaviour."""

    def test_idle_stays_idle_when_below_bb(self):
        state = SignalState()
        fired = advance(state, close=22000, open_=22050, high=22100, low=21950, bb_upper=22100)
        assert not fired
        assert state.phase == "IDLE"

    def test_idle_transitions_to_watching_on_close_above_bb(self):
        state = SignalState()
        fired = advance(state, close=22200, open_=22050, high=22250, low=22000, bb_upper=22100)
        assert not fired
        assert state.phase == "WATCHING"

    def test_idle_does_not_fire_signal(self):
        state = SignalState()
        fired = advance(state, close=20000, open_=22000, high=22100, low=19900, bb_upper=22100)
        assert not fired

    def test_idle_exactly_at_bb_upper_stays_idle(self):
        """Close == bb_upper is NOT above; must be strictly greater."""
        state = SignalState()
        fired = advance(state, close=22100, open_=22000, high=22150, low=22000, bb_upper=22100)
        assert not fired
        assert state.phase == "IDLE"


class TestSignalStateWatching:
    """WATCHING phase behaviour."""

    def _watching_state(self) -> SignalState:
        state = SignalState()
        advance(state, close=22200, open_=22050, high=22250, low=22000, bb_upper=22100)
        assert state.phase == "WATCHING"
        return state

    def test_green_candle_stays_watching(self):
        state = self._watching_state()
        fired = advance(state, close=22300, open_=22200, high=22350, low=22180, bb_upper=22100)
        assert not fired
        assert state.phase == "WATCHING"

    def test_green_candle_below_bb_stays_watching(self):
        """Dropping below BB upper while WATCHING does not cancel setup."""
        state = self._watching_state()
        # Green candle but close < bb_upper
        fired = advance(state, close=22050, open_=21900, high=22100, low=21850, bb_upper=22100)
        assert not fired
        assert state.phase == "WATCHING"

    def test_red_candle_transitions_to_red_found(self):
        state = self._watching_state()
        fired = advance(state, close=22100, open_=22250, high=22260, low=22080, bb_upper=22100)
        assert not fired
        assert state.phase == "RED_FOUND"

    def test_red_candle_records_low(self):
        state = self._watching_state()
        advance(state, close=22100, open_=22250, high=22260, low=22080, bb_upper=22100)
        assert state.red_low == 22080

    def test_doji_candle_stays_watching(self):
        """close == open is treated as non-red → stays WATCHING."""
        state = self._watching_state()
        fired = advance(state, close=22200, open_=22200, high=22220, low=22180, bb_upper=22100)
        assert not fired
        assert state.phase == "WATCHING"


class TestSignalStateRedFound:
    """RED_FOUND phase behaviour."""

    def _red_found_state(self, red_low: float = 22080) -> SignalState:
        state = SignalState()
        # IDLE → WATCHING
        advance(state, close=22200, open_=22050, high=22250, low=22000, bb_upper=22100)
        # WATCHING → RED_FOUND with red_low = red_low
        advance(state, close=22100, open_=22250, high=22260, low=red_low, bb_upper=22100)
        assert state.phase == "RED_FOUND"
        assert state.red_low == red_low
        return state

    def test_low_below_red_low_fires_signal(self):
        """Signal fires when candle low breaches redLow (intrabar touch)."""
        state = self._red_found_state(red_low=22080)
        fired = advance(state, close=22070, open_=22100, high=22110, low=22060, bb_upper=22100)
        assert fired

    def test_low_breaches_but_close_above_still_fires(self):
        """Signal fires even if close is above redLow — low breach is enough."""
        state = self._red_found_state(red_low=22080)
        fired = advance(state, close=22090, open_=22100, high=22110, low=22070, bb_upper=22100)
        assert fired

    def test_low_equal_to_red_low_no_signal(self):
        """low == red_low does NOT fire; needs strictly less than."""
        state = self._red_found_state(red_low=22080)
        fired = advance(state, close=22090, open_=22100, high=22110, low=22080, bb_upper=22100)
        assert not fired
        assert state.phase == "RED_FOUND"

    def test_low_above_red_low_no_signal(self):
        state = self._red_found_state(red_low=22080)
        fired = advance(state, close=22150, open_=22100, high=22160, low=22090, bb_upper=22100)
        assert not fired

    def test_signal_resets_state(self):
        """After signal fires the state should be reset to IDLE."""
        state = self._red_found_state(red_low=22080)
        advance(state, close=22070, open_=22100, high=22110, low=22060, bb_upper=22100)
        assert state.phase == "IDLE"
        assert state.red_low is None

    def test_green_breakout_above_bb_resets_to_watching(self):
        """Green candle with close > bb_upper in RED_FOUND → back to WATCHING."""
        state = self._red_found_state(red_low=22080)
        fired = advance(state, close=22200, open_=22100, high=22210, low=22090, bb_upper=22100)
        assert not fired
        assert state.phase == "WATCHING"
        assert state.red_low is None

    def test_green_candle_not_above_bb_stays_red_found(self):
        """Green candle that doesn't close above BB → stays RED_FOUND."""
        state = self._red_found_state(red_low=22080)
        fired = advance(state, close=22090, open_=22085, high=22110, low=22080, bb_upper=22100)
        assert not fired
        assert state.phase == "RED_FOUND"

    def test_second_red_candle_does_not_update_red_low(self):
        """Only the FIRST red candle's low is stored; subsequent reds are ignored."""
        state = self._red_found_state(red_low=22080)
        original_low = state.red_low
        # Another red candle whose low stays ABOVE red_low
        fired = advance(state, close=22085, open_=22100, high=22105, low=22082, bb_upper=22100)
        assert not fired
        assert state.phase == "RED_FOUND"
        assert state.red_low == original_low  # unchanged

    def test_red_candle_low_above_red_low_stays_red_found(self):
        """Red candle whose low doesn't breach redLow stays RED_FOUND."""
        state = self._red_found_state(red_low=22080)
        fired = advance(state, close=22095, open_=22120, high=22130, low=22085, bb_upper=22100)
        assert not fired
        assert state.phase == "RED_FOUND"


class TestSignalStateReset:
    """reset() method behaviour."""

    def test_reset_from_idle(self):
        state = SignalState()
        state.reset()
        assert state.phase == "IDLE"
        assert state.red_low is None

    def test_reset_from_watching(self):
        state = SignalState()
        advance(state, close=22200, open_=22050, high=22250, low=22000, bb_upper=22100)
        state.reset()
        assert state.phase == "IDLE"
        assert state.red_low is None

    def test_reset_from_red_found(self):
        state = SignalState()
        advance(state, close=22200, open_=22050, high=22250, low=22000, bb_upper=22100)
        advance(state, close=22100, open_=22250, high=22260, low=22080, bb_upper=22100)
        assert state.red_low is not None
        state.reset()
        assert state.phase == "IDLE"
        assert state.red_low is None

    def test_double_reset_is_idempotent(self):
        state = SignalState()
        state.reset()
        state.reset()
        assert state.phase == "IDLE"
        assert state.red_low is None


class TestSignalStateMultiCandle:
    """Integration-style sequences across multiple candles."""

    def test_full_sequence_idle_watching_red_found_signal(self):
        """Full happy path: IDLE → WATCHING → RED_FOUND → signal fires."""
        state = SignalState()
        bb = 22100

        # Candle 1: below BB, stays IDLE
        assert not advance(state, close=21900, open_=21800, high=21950, low=21780, bb_upper=bb)
        assert state.phase == "IDLE"

        # Candle 2: close > BB → WATCHING
        assert not advance(state, close=22200, open_=22050, high=22250, low=22000, bb_upper=bb)
        assert state.phase == "WATCHING"

        # Candle 3: green candle → stays WATCHING
        assert not advance(state, close=22250, open_=22200, high=22300, low=22180, bb_upper=bb)
        assert state.phase == "WATCHING"

        # Candle 4: red candle → RED_FOUND, red_low = 22090
        assert not advance(state, close=22110, open_=22250, high=22260, low=22090, bb_upper=bb)
        assert state.phase == "RED_FOUND"
        assert state.red_low == 22090

        # Candle 5: second red candle, low stays above red_low — red_low unchanged
        assert not advance(state, close=22095, open_=22120, high=22130, low=22091, bb_upper=bb)
        assert state.red_low == 22090

        # Candle 6: low < red_low → SIGNAL fires
        fired = advance(state, close=22080, open_=22100, high=22105, low=22070, bb_upper=bb)
        assert fired

    def test_green_breakout_resets_and_can_fire_again(self):
        """After a green breakout resets to WATCHING, a new setup can complete."""
        state = SignalState()
        bb = 22100

        # Setup 1: reach RED_FOUND
        advance(state, close=22200, open_=22050, high=22250, low=22000, bb_upper=bb)
        advance(state, close=22110, open_=22250, high=22260, low=22090, bb_upper=bb)
        assert state.phase == "RED_FOUND"

        # Green breakout → back to WATCHING
        advance(state, close=22250, open_=22150, high=22260, low=22140, bb_upper=bb)
        assert state.phase == "WATCHING"

        # New red candle → RED_FOUND again with fresh low
        advance(state, close=22120, open_=22250, high=22260, low=22100, bb_upper=bb)
        assert state.phase == "RED_FOUND"
        assert state.red_low == 22100

        # Signal fires
        fired = advance(state, close=22095, open_=22120, high=22130, low=22085, bb_upper=bb)
        assert fired


# ---------------------------------------------------------------------------
# Task 3: Engine initialization
# ---------------------------------------------------------------------------

class TestBBReversalEngineInit:
    """Test that the engine initializes with correct default parameters."""

    def test_engine_init(self):
        """Engine initializes with default parameters."""
        from engine.bb_reversal_backtest import BBReversalBacktestEngine
        engine = BBReversalBacktestEngine(start_date="2025-06-01", end_date="2025-06-30")
        assert engine.tp_points == 15
        assert engine.sl_points == 15
        assert engine.entry_start == "09:18"
        assert engine.entry_end == "15:19"
        assert engine.force_exit_time == "15:20"
        assert engine.bb_period == 20
        assert engine.bb_std == 2.0
        assert engine.instrument == "NIFTY"
        assert engine.lot_size == 75
        assert engine._spot_1m is None
        assert engine._options_1m is None

    def test_engine_custom_params(self):
        """Engine accepts custom parameters."""
        from engine.bb_reversal_backtest import BBReversalBacktestEngine
        engine = BBReversalBacktestEngine(
            start_date="2025-06-01",
            end_date="2025-06-30",
            tp_points=20.0,
            sl_points=10.0,
            bb_period=30,
            bb_std=2.5,
            entry_start="09:30",
            entry_end="15:00",
            force_exit_time="15:15",
        )
        assert engine.tp_points == 20.0
        assert engine.sl_points == 10.0
        assert engine.bb_period == 30
        assert engine.bb_std == 2.5
        assert engine.entry_start == "09:30"
        assert engine.entry_end == "15:00"
        assert engine.force_exit_time == "15:15"
