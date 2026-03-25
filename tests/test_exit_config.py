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


from engine.backtest import resolve_exit_levels
import math


class TestResolveExitLevels:
    """Test resolve_exit_levels() computes SL/TP prices correctly."""

    def test_percentage_sell(self):
        cfg = {
            "stop_loss": {"source": "percentage", "value": 20.0},
            "target": {"source": "percentage", "value": 10.0},
        }
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, {})
        assert sl == pytest.approx(120.0)
        assert tp == pytest.approx(90.0)

    def test_percentage_buy(self):
        cfg = {
            "stop_loss": {"source": "percentage", "value": 20.0},
            "target": {"source": "percentage", "value": 10.0},
        }
        sl, tp = resolve_exit_levels(100.0, "buy", cfg, {})
        assert sl == pytest.approx(80.0)
        assert tp == pytest.approx(110.0)

    def test_indicator_sell_sl(self):
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "percentage", "value": 10.0},
        }
        row = {"opt_st_value": 115.0}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert sl == pytest.approx(115.0)
        assert tp == pytest.approx(90.0)

    def test_indicator_nan_returns_none(self):
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "percentage", "value": 10.0},
        }
        row = {"opt_st_value": float("nan")}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert sl is None
        assert tp == pytest.approx(90.0)

    def test_indicator_wrong_side_returns_none(self):
        """For sell, SL must be > avg_entry. 95 < 100, so wrong side."""
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "percentage", "value": 10.0},
        }
        row = {"opt_st_value": 95.0}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert sl is None

    def test_ratio_tp_from_percentage_sl(self):
        """TP = 2x SL distance. SL=110, dist=10, TP=100-20=80."""
        cfg = {
            "stop_loss": {"source": "percentage", "value": 10.0},
            "target": {"source": "ratio", "multiplier": 2.0},
        }
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, {})
        assert sl == pytest.approx(110.0)
        assert tp == pytest.approx(80.0)

    def test_ratio_tp_from_indicator_sl(self):
        """TP = 2x SL distance. SL indicator=110, dist=10, TP=80."""
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "ratio", "multiplier": 2.0},
        }
        row = {"opt_st_value": 110.0}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
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
        """SL = TP_distance * 0.5 for buy. TP=120, dist=20, SL=100-10=90."""
        cfg = {
            "stop_loss": {"source": "ratio", "multiplier": 0.5},
            "target": {"source": "percentage", "value": 20.0},
        }
        sl, tp = resolve_exit_levels(100.0, "buy", cfg, {})
        assert tp == pytest.approx(120.0)
        assert sl == pytest.approx(90.0)

    def test_indicator_missing_key_returns_none(self):
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "percentage", "value": 10.0},
        }
        row = {}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert sl is None

    def test_indicator_tp_sell(self):
        """Indicator TP for sell — value below entry."""
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
        row = {"opt_bb_lower": 105.0}
        sl, tp = resolve_exit_levels(100.0, "sell", cfg, row)
        assert tp is None

    def test_indicator_buy_sl_wrong_side(self):
        """Indicator SL above entry for buy returns None."""
        cfg = {
            "stop_loss": {"source": "indicator", "indicator": "opt_st_value"},
            "target": {"source": "percentage", "value": 10.0},
        }
        row = {"opt_st_value": 105.0}
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
