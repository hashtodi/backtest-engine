import pandas as pd
import pytest

from indicators.heikin_ashi import compute_heikin_ashi
from engine.ha_nr7_backtest import (
    compute_nr7,
    get_dte_tp_sl,
    adjust_for_ema,
    EngineState,
    HaNr7Trade,
    HaNr7BacktestEngine,
    trades_to_dataframe,
)


class TestHeikinAshi:
    """Test HA candle computation matches TradingView PineScript formula."""

    def _make_spot_df(self, rows):
        """Helper: list of (open, high, low, close) → DataFrame."""
        return pd.DataFrame(rows, columns=["open", "high", "low", "close"])

    def test_first_candle_ha_open(self):
        df = self._make_spot_df([(100, 110, 95, 105)])
        result = compute_heikin_ashi(df)
        assert result["ha_open"].iloc[0] == pytest.approx((100 + 105) / 2)

    def test_first_candle_ha_close(self):
        df = self._make_spot_df([(100, 110, 95, 105)])
        result = compute_heikin_ashi(df)
        assert result["ha_close"].iloc[0] == pytest.approx((100 + 110 + 95 + 105) / 4)

    def test_first_candle_ha_high(self):
        df = self._make_spot_df([(100, 110, 95, 105)])
        result = compute_heikin_ashi(df)
        ha_open = (100 + 105) / 2
        ha_close = (100 + 110 + 95 + 105) / 4
        assert result["ha_high"].iloc[0] == pytest.approx(max(110, ha_open, ha_close))

    def test_first_candle_ha_low(self):
        df = self._make_spot_df([(100, 110, 95, 105)])
        result = compute_heikin_ashi(df)
        ha_open = (100 + 105) / 2
        ha_close = (100 + 110 + 95 + 105) / 4
        assert result["ha_low"].iloc[0] == pytest.approx(min(95, ha_open, ha_close))

    def test_second_candle_ha_open_uses_previous(self):
        df = self._make_spot_df([
            (100, 110, 95, 105),
            (106, 115, 100, 112),
        ])
        result = compute_heikin_ashi(df)
        prev_ha_open = (100 + 105) / 2
        prev_ha_close = (100 + 110 + 95 + 105) / 4
        expected_ha_open = (prev_ha_open + prev_ha_close) / 2
        assert result["ha_open"].iloc[1] == pytest.approx(expected_ha_open)

    def test_original_columns_preserved(self):
        df = self._make_spot_df([(100, 110, 95, 105)])
        result = compute_heikin_ashi(df)
        assert result["open"].iloc[0] == 100
        assert result["high"].iloc[0] == 110
        assert result["low"].iloc[0] == 95
        assert result["close"].iloc[0] == 105

    def test_neutral_candle_detection(self):
        df = self._make_spot_df([
            (100, 105, 95, 100),
            (90, 121, 99, 90),
        ])
        result = compute_heikin_ashi(df)
        ha_body = abs(result["ha_close"].iloc[1] - result["ha_open"].iloc[1])
        regular_range = result["high"].iloc[1] - result["low"].iloc[1]
        assert regular_range == 22
        assert ha_body < 2.5


# ==================================================================
# NR7 Tests
# ==================================================================


class TestNR7:
    """Test NR7 computation matching LuxAlgo PineScript: rng == ta.lowest(rng, 7)."""

    def test_nr7_basic(self):
        """NR7 fires on the candle with the smallest range in 7 candles."""
        # Ranges: 10, 8, 12, 9, 11, 7, 6 -> candle 6 (idx=6) has smallest
        df = pd.DataFrame({
            "high": [110, 108, 112, 109, 111, 107, 106],
            "low":  [100, 100, 100, 100, 100, 100, 100],
        })
        result = compute_nr7(df, lookback=7)
        assert result.iloc[6] == True
        # First 6 candles don't have 7 bars of history -> False
        assert result.iloc[:6].sum() == 0

    def test_nr7_tie_counts(self):
        """Ties count as NR7 (== not <)."""
        # Ranges: 10, 8, 12, 9, 11, 8, 8 -> candles 5 and 6 both tie at 8
        df = pd.DataFrame({
            "high": [110, 108, 112, 109, 111, 108, 108],
            "low":  [100, 100, 100, 100, 100, 100, 100],
        })
        result = compute_nr7(df, lookback=7)
        assert result.iloc[6] == True

    def test_nr7_not_smallest(self):
        """Candle whose range is not the smallest -> NR7 = False."""
        # Ranges: 5, 10, 8, 12, 9, 11, 7 -> candle 6 range=7, but min is 5
        df = pd.DataFrame({
            "high": [105, 110, 108, 112, 109, 111, 107],
            "low":  [100, 100, 100, 100, 100, 100, 100],
        })
        result = compute_nr7(df, lookback=7)
        assert result.iloc[6] == False

    def test_nr7_needs_7_candles(self):
        """NR7 returns False for first 6 candles (insufficient history)."""
        df = pd.DataFrame({
            "high": [110, 108, 112, 109, 111, 107, 106, 115],
            "low":  [100, 100, 100, 100, 100, 100, 100, 100],
        })
        result = compute_nr7(df, lookback=7)
        assert result.iloc[:6].any() == False


# ==================================================================
# DTE TP/SL Tests
# ==================================================================


class TestDteTpSl:
    """Test DTE-based TP/SL lookup."""

    def test_dte_0_expiry_day(self):
        tp, sl = get_dte_tp_sl(0)
        assert tp == 15.0
        assert sl == 15.0

    def test_dte_1(self):
        tp, sl = get_dte_tp_sl(1)
        assert tp == 12.5
        assert sl == 12.5

    def test_dte_2(self):
        tp, sl = get_dte_tp_sl(2)
        assert tp == 10.0
        assert sl == 10.0

    def test_dte_3(self):
        tp, sl = get_dte_tp_sl(3)
        assert tp == 7.5
        assert sl == 7.5

    def test_dte_4_and_above(self):
        tp, sl = get_dte_tp_sl(4)
        assert tp == 5.0
        assert sl == 7.5
        tp2, sl2 = get_dte_tp_sl(10)
        assert tp2 == 5.0
        assert sl2 == 7.5


# ==================================================================
# EMA Adjustment Tests
# ==================================================================


class TestEmaAdjustment:
    """Test EMA-based TP and SL adjustment."""

    def test_above_both_emas_tp_gte_7_5_reduces(self):
        """Entry above both EMAs and TP >= 7.5 -> TP=5%, SL=7.5%."""
        tp, sl, adjusted = adjust_for_ema(
            tp_pct=12.5, sl_pct=12.5, entry_price=200, ema10=195, ema21=190
        )
        assert tp == 5.0
        assert sl == 7.5
        assert adjusted == True

    def test_below_both_emas_tp_lte_7_5_increases(self):
        """Entry below both EMAs and TP <= 7.5 -> TP=10%, SL unchanged."""
        tp, sl, adjusted = adjust_for_ema(
            tp_pct=5.0, sl_pct=7.5, entry_price=150, ema10=155, ema21=160
        )
        assert tp == 10.0
        assert sl == 7.5
        assert adjusted == True

    def test_above_both_tp_below_threshold_no_change(self):
        """Entry above both EMAs but TP < 7.5 -> no change."""
        tp, sl, adjusted = adjust_for_ema(
            tp_pct=5.0, sl_pct=7.5, entry_price=200, ema10=195, ema21=190
        )
        assert tp == 5.0
        assert sl == 7.5
        assert adjusted == False

    def test_below_both_tp_above_threshold_no_change(self):
        """Entry below both EMAs but TP > 7.5 -> no change."""
        tp, sl, adjusted = adjust_for_ema(
            tp_pct=10.0, sl_pct=10.0, entry_price=150, ema10=155, ema21=160
        )
        assert tp == 10.0
        assert sl == 10.0
        assert adjusted == False

    def test_between_emas_no_change(self):
        """Entry between EMAs -> no change regardless of TP."""
        tp, sl, adjusted = adjust_for_ema(
            tp_pct=12.5, sl_pct=12.5, entry_price=155, ema10=150, ema21=160
        )
        assert tp == 12.5
        assert sl == 12.5
        assert adjusted == False

    def test_boundary_tp_7_5_above_both(self):
        """TP exactly 7.5 and above both -> TP=5%, SL=7.5%."""
        tp, sl, adjusted = adjust_for_ema(
            tp_pct=7.5, sl_pct=7.5, entry_price=200, ema10=195, ema21=190
        )
        assert tp == 5.0
        assert sl == 7.5
        assert adjusted == True

    def test_boundary_tp_7_5_below_both(self):
        """TP exactly 7.5 and below both -> TP=10%, SL unchanged if <= 10."""
        tp, sl, adjusted = adjust_for_ema(
            tp_pct=7.5, sl_pct=7.5, entry_price=150, ema10=155, ema21=160
        )
        assert tp == 10.0
        assert sl == 7.5
        assert adjusted == True

    def test_below_both_sl_capped_at_10(self):
        """Below both EMAs: SL capped at 10% (e.g., DTE-0 SL=15% → 10%)."""
        tp, sl, adjusted = adjust_for_ema(
            tp_pct=7.5, sl_pct=15.0, entry_price=150, ema10=155, ema21=160
        )
        assert tp == 10.0
        assert sl == 10.0
        assert adjusted == True

    def test_above_both_sl_fixed_at_7_5(self):
        """Above both EMAs: SL always becomes 7.5% regardless of DTE SL."""
        tp, sl, adjusted = adjust_for_ema(
            tp_pct=15.0, sl_pct=15.0, entry_price=200, ema10=195, ema21=190
        )
        assert tp == 5.0
        assert sl == 7.5
        assert adjusted == True


# ==================================================================
# Trade Dataclass Tests
# ==================================================================


class TestHaNr7Trade:
    """Test HaNr7Trade dataclass."""

    def test_trade_creation(self):
        trade = HaNr7Trade(
            entry_date="2025-01-15",
            alert_candle_time="2025-01-15 10:21",
            entry_times="['2025-01-15 10:24']",
            exit_time="2025-01-15 10:45",
            option_type="CE",
            strike=23400,
            entry_prices="[180.0]",
            avg_entry=180.0,
            num_lots=1,
            exit_price=189.0,
            exit_reason="TP",
            tp_pct=5.0,
            sl_pct=12.5,
            dte=1,
            ema_adjusted=True,
            is_reversal=False,
            pnl_points=9.0,
            pnl_inr=585.0,
        )
        assert trade.option_type == "CE"
        assert trade.strike == 23400
        assert trade.pnl_inr == 585.0

    def test_trades_to_dataframe_empty(self):
        df = trades_to_dataframe([])
        assert df.empty

    def test_trades_to_dataframe(self):
        trade = HaNr7Trade(
            entry_date="2025-01-15",
            alert_candle_time="2025-01-15 10:21",
            entry_times="['2025-01-15 10:24']",
            exit_time="2025-01-15 10:45",
            option_type="CE",
            strike=23400,
            entry_prices="[180.0]",
            avg_entry=180.0,
            num_lots=1,
            exit_price=189.0,
            exit_reason="TP",
            tp_pct=5.0,
            sl_pct=12.5,
            dte=1,
            ema_adjusted=True,
            is_reversal=False,
            pnl_points=9.0,
            pnl_inr=585.0,
        )
        df = trades_to_dataframe([trade])
        assert len(df) == 1
        assert df.iloc[0]["strike"] == 23400
        assert "entry_prices" in df.columns


# ==================================================================
# Engine Init Tests
# ==================================================================


class TestEngineInit:
    """Test HaNr7BacktestEngine initialization defaults."""

    def test_default_params(self):
        engine = HaNr7BacktestEngine("2025-01-01", "2025-01-31")
        assert engine.instrument == "NIFTY"
        assert engine.lot_size == 65
        assert engine.strike_rounding == 100
        assert engine.ha_body_threshold == 2.5
        assert engine.ha_range_threshold == 20.0
        assert engine.nr7_lookback == 7
        assert engine.nr7_scan_window == 5
        assert engine.ema_short_period == 10
        assert engine.ema_long_period == 21
        assert engine.trading_start == "09:30"
        assert engine.last_entry == "14:45"
        assert engine.force_exit == "14:55"

    def test_custom_params(self):
        engine = HaNr7BacktestEngine(
            "2025-01-01",
            "2025-01-31",
            strike_rounding=50,
            ha_body_threshold=3.0,
            nr7_lookback=5,
        )
        assert engine.strike_rounding == 50
        assert engine.ha_body_threshold == 3.0
        assert engine.nr7_lookback == 5

    def test_engine_states(self):
        assert EngineState.IDLE.value == "IDLE"
        assert EngineState.ALERT_ACTIVE.value == "ALERT_ACTIVE"
        assert EngineState.POSITION_OPEN.value == "POSITION_OPEN"
        assert EngineState.DAY_STOPPED.value == "DAY_STOPPED"
