"""HV_20d from NIFTY daily closes.

sigma = stdev(last 20 daily log-returns) ; hv = sigma * sqrt(252) * 100.
No look-ahead: HV for day D uses the 20 returns ending at D-1.
"""
import numpy as np
import pandas as pd


def _hv20_from_daily_close(daily_close: pd.Series, lookback: int = 20,
                           annualize: int = 252) -> pd.Series:
    log_ret = np.log(daily_close / daily_close.shift(1))
    sigma = log_ret.rolling(lookback).std(ddof=1).shift(1)  # shift -> exclude day D's return
    return sigma * np.sqrt(annualize) * 100.0


def compute_hv20(spot_path: str, lookback: int = 20,
                 annualize: int = 252) -> dict:
    df = pd.read_parquet(spot_path, columns=["datetime", "close"])
    dt = pd.to_datetime(df["datetime"].str.slice(0, 19))  # naive IST
    df = df.assign(_date=dt.dt.date)
    daily_close = df.groupby("_date")["close"].last().sort_index()
    hv = _hv20_from_daily_close(daily_close, lookback, annualize)
    return hv.to_dict()
