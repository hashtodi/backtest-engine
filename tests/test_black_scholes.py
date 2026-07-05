"""Tests for Black-Scholes delta."""
import math
from engine.black_scholes import bs_delta


def test_ce_delta_in_range():
    d = bs_delta("CE", 100, 100, 0.20, 1.0, r=0.0, q=0.0)
    assert 0.0 < d < 1.0
    # ATM call with zero rate/carry is slightly above 0.5
    assert abs(d - 0.5398) < 1e-3


def test_pe_delta_negative():
    d = bs_delta("PE", 100, 100, 0.20, 1.0, r=0.0, q=0.0)
    assert -1.0 < d < 0.0


def test_deep_itm_ce_delta_near_one():
    assert bs_delta("CE", 200, 100, 0.20, 0.05) > 0.99


def test_degenerate_inputs_return_nan():
    assert math.isnan(bs_delta("CE", 100, 100, 0.0, 1.0))   # sigma 0
    assert math.isnan(bs_delta("CE", 100, 100, 0.20, 0.0))  # T 0


def test_fixture_true_tuesday_expiry():
    # 2026-05-11 09:45, spot 23866, CE 24300 iv 22.45%, true expiry Tue 2026-05-12 15:30
    T = 1785 / 525600.0
    d = bs_delta("CE", 23866, 24300, 0.2245, T, r=0.065, q=0.0)
    assert abs(d - 0.088) < 0.005


def test_fixture_wrong_thursday_expiry_reproduces_doc():
    # Same option, wrong Thursday T -> reproduces the doc's buggy 0.206
    T = 4665 / 525600.0
    d = bs_delta("CE", 23866, 24300, 0.2245, T, r=0.065, q=0.0)
    assert abs(d - 0.206) < 0.01
