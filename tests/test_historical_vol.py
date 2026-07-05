"""Tests for HV_20d computation."""
import math
import numpy as np
import pandas as pd
from datetime import date
from engine.historical_vol import _hv20_from_daily_close


def _mk_close(n):
    idx = [date(2021, 1, 1) + pd.Timedelta(days=i) for i in range(n)]
    # deterministic gently-trending closes
    vals = [100.0 * (1.01 ** i) for i in range(n)]
    return pd.Series(vals, index=idx)


def test_warmup_is_nan():
    hv = _hv20_from_daily_close(_mk_close(25), lookback=20, annualize=252)
    # first 20 entries have no full 20-return window ending at D-1
    assert hv.iloc[:20].isna().all()
    assert not math.isnan(hv.iloc[21])


def test_no_lookahead_hv_independent_of_same_day_close():
    base = _mk_close(30)
    hv_a = _hv20_from_daily_close(base, 20, 252)
    bumped = base.copy()
    bumped.iloc[25] *= 1.5  # perturb close at D=index 25
    hv_b = _hv20_from_daily_close(bumped, 20, 252)
    # HV at D=25 must NOT change (it only uses returns through D-1)
    assert abs(hv_a.iloc[25] - hv_b.iloc[25]) < 1e-9


def test_hand_computed_value():
    # constant 1% daily growth -> all log returns equal -> stdev 0 -> hv 0
    hv = _hv20_from_daily_close(_mk_close(30), 20, 252)
    assert abs(hv.iloc[25]) < 1e-6
