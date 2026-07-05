"""
SuperTrend Low-Band Backtest Engine.

Strategy:
  Daily intraday on NIFTY weekly ATM CE/PE. Buy when continuous
  SuperTrend(3,10) on the option is bullish AND its value is within
  ±band_pct of the contract's 9:15-9:19 morning low. TP/SL as % of
  entry; force-exit at 14:45. CE and PE run as fully independent
  state machines with unbounded same-day re-entry.

  Spec: docs/superpowers/specs/2026-05-05-supertrend-low-band-design.md
"""

import logging
import math
import os
from dataclasses import asdict, dataclass, fields
from datetime import date as _date, time as _time
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from config import DATA_PATH, LOT_SIZE, get_nearest_weekly_expiry
from indicators.supertrend import SuperTrend

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.dirname(__file__))

ST_BULLISH = -1.0  # SuperTrend.calculate() returns -1 for bullish, +1 for bearish


@dataclass
class StLowBandTrade:
    """Single completed trade record."""
    date: str                 # "YYYY-MM-DD"
    instrument: str           # "NIFTY"
    expiry_date: str          # "YYYY-MM-DD" of nearest weekly expiry
    option_type: str          # "CE" | "PE"
    strike: int

    morning_low: float        # 9:15-9:19 min(low) for this contract
    band_high: float          # morning_low * (1 + band_pct/100). Entry trigger upper threshold.

    spot_at_entry: float
    entry_time: str           # "HH:MM"
    entry_price: float        # next-bar open of locked strike
    entry_st_value: float     # ST value at trigger bar T close
    entry_trigger_close: float  # option close at trigger bar T

    spot_at_exit: float
    exit_time: str
    exit_price: float         # exact SL/TP level OR EOD close
    exit_reason: str          # "SL" | "TP" | "EOD"

    dte: int                  # trading days from trade_date (excl) to nearest weekly expiry (incl)
    tp_pct: float             # TP % used for this trade (from dte_table)
    sl_pct: float             # SL % used for this trade (from dte_table)
    pnl_points: float         # exit_price - entry_price
    pnl_pct: float            # (exit_price - entry_price) / entry_price * 100
    pnl_inr: float            # pnl_points * lot_size_total
    lot_size: int             # LOT_SIZE * json.lot_size


def trades_to_dataframe(trades: List[StLowBandTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


def evaluate_entry(
    option_close: float,
    st_dir: float,
    morning_low: float,
    band_pct: float,
    bullish_required: bool = True,
) -> bool:
    """True iff entry conditions met at this bar's close.

    Conditions:
      1. (if bullish_required) 5-min st_dir == ST_BULLISH (forward-filled to 1-min)
      2. The option's 1-min close is at or below morning_low × (1 + band_pct/100).
         I.e., the close is either BELOW the morning low or WITHIN band_pct%
         ABOVE it. No lower bound.

    NaN on any input returns False (skip-side semantics).
    """
    if st_dir is None or (isinstance(st_dir, float) and math.isnan(st_dir)):
        return False
    if bullish_required and st_dir != ST_BULLISH:
        return False
    if option_close is None or morning_low is None:
        return False
    if isinstance(option_close, float) and math.isnan(option_close):
        return False
    if isinstance(morning_low, float) and math.isnan(morning_low):
        return False
    upper = morning_low * (1.0 + band_pct / 100.0)
    return option_close <= upper


def compute_trading_dte(trade_date: _date, expiry_date: _date,
                        trading_dates: List[_date]) -> int:
    """Count trading days strictly after trade_date, up to and including expiry_date.

    Convention matches HA-NR7: DTE 0 = expiry day, DTE 1 = previous trading day,
    etc. Uses actual trading-date list (skips weekends + holidays).
    """
    if expiry_date is None:
        return 0
    if trade_date >= expiry_date:
        return 0
    return sum(1 for d in trading_dates if trade_date < d <= expiry_date)


def parse_dte_table(dte_table_cfg: dict) -> Dict[int, Tuple[float, float]]:
    """Parse JSON dte_table into {dte: (tp_pct, sl_pct)}.

    Input format: { "0": { "tp_pct": 20.0, "sl_pct": 12.5 }, ... }
    """
    out: Dict[int, Tuple[float, float]] = {}
    for k, v in dte_table_cfg.items():
        dte = int(k)
        out[dte] = (float(v["tp_pct"]), float(v["sl_pct"]))
    return out


def get_dte_tp_sl(dte: int, table: Dict[int, Tuple[float, float]]) -> Tuple[float, float]:
    """Lookup (tp_pct, sl_pct) for a given DTE; clamp at the maximum DTE in the table.

    DTE values higher than the largest key in the table use the largest key's
    settings. Raises KeyError if the table is empty.
    """
    if not table:
        raise KeyError("dte_table is empty")
    max_dte = max(table.keys())
    return table[min(dte, max_dte)]


def evaluate_exit(
    bar_high: float, bar_low: float, bar_close: float,
    sl: float, tp: float, is_force_exit_bar: bool,
) -> Optional[Tuple[float, str]]:
    """Return (exit_price, exit_reason) if this bar exits, else None.

    Priority: SL > TP > EOD. SL/TP fills assume exact-level fills when
    the wick touches; this is the same convention as Gamma Blast.
    """
    if bar_low <= sl:
        return (float(sl), "SL")
    if bar_high >= tp:
        return (float(tp), "TP")
    if is_force_exit_bar:
        return (float(bar_close), "EOD")
    return None


def compute_first_5min_low_table(
    df: pd.DataFrame, window_start: _time, window_end: _time,
) -> Dict[Tuple, float]:
    """Per-(date, contract) min(low) across bars in [window_start, window_end).

    Window is half-open: window_start inclusive, window_end exclusive. The
    9:15-9:20 candle covers bars timestamped 09:15, 09:16, 09:17, 09:18, 09:19.

    Args:
        df: 1-min option bars with columns date, time_only, strike,
            option_type, expiry_type, expiry_code, low.
        window_start: e.g. time(9, 15)
        window_end:   e.g. time(9, 20)

    Returns:
        Dict keyed by (date, strike, option_type, expiry_type, expiry_code)
        → float (min low). Contracts with no bars in the window are absent.
    """
    in_window = df[(df["time_only"] >= window_start) & (df["time_only"] < window_end)]
    if in_window.empty:
        return {}
    grouped = in_window.groupby(
        ["date", "strike", "option_type", "expiry_type", "expiry_code"],
    )["low"].min()
    return grouped.to_dict()


CONTRACT_COLS = ["strike", "option_type", "expiry_type", "expiry_code"]


def compute_continuous_supertrend_per_contract(
    df: pd.DataFrame, factor: int, atr_period: int,
    timeframe: str = "5min",
) -> pd.DataFrame:
    """Add `st_value` and `st_dir` columns to `df`, computed per contract.

    Each unique (strike, option_type, expiry_type, expiry_code) is treated as
    one contract. ST is computed on `timeframe` OHLC (default "5min")
    continuously across the contract's lifetime (no daily reset), then
    forward-filled to the 1-min rows with a +`timeframe` shift so the
    indicator is only available at the END of each timeframe bar.

    Matches engine/st_ema_backtest.py's resample → compute → ffill convention.
    """
    st_ind = SuperTrend(name="st", factor=factor, atr_period=atr_period)
    tf_minutes = int(pd.Timedelta(timeframe).total_seconds() // 60)

    parts = []
    for _, group in df.groupby(CONTRACT_COLS, sort=False):
        group = group.sort_values("datetime").copy()

        # 1-min OHLC indexed by datetime, market hours only
        ohlc_1m = group.set_index("datetime")[["open", "high", "low", "close"]]
        ohlc_1m = ohlc_1m.between_time("09:15", "15:29")

        if tf_minutes > 1:
            ohlc_tf = ohlc_1m.resample(timeframe).agg({
                "open": "first", "high": "max", "low": "min", "close": "last",
            }).dropna(subset=["close"])
        else:
            ohlc_tf = ohlc_1m

        if ohlc_tf.empty:
            group["st_value"] = float("nan")
            group["st_dir"] = float("nan")
            parts.append(group)
            continue

        st_result = st_ind.calculate(
            ohlc_tf["close"], high=ohlc_tf["high"], low=ohlc_tf["low"],
        )

        # Shift index by +tf_minutes so the timeframe bar's ST is only
        # available at the END of the bar (i.e., at the next bar's open time).
        # E.g., 9:15-9:19 5-min bar's ST value is keyed at 9:20.
        st_tf = pd.DataFrame({
            "st_value": st_result["value"].values,
            "st_dir": st_result["direction"].values,
        }, index=ohlc_tf.index + pd.Timedelta(minutes=tf_minutes))

        # Forward-fill onto the 1-min datetimes
        st_1m = st_tf.reindex(group["datetime"], method="ffill")
        group["st_value"] = st_1m["st_value"].values
        group["st_dir"] = st_1m["st_dir"].values

        parts.append(group)

    if not parts:
        df = df.copy()
        df["st_value"] = float("nan")
        df["st_dir"] = float("nan")
        return df
    return pd.concat(parts, ignore_index=True)


def build_atm_index(df: pd.DataFrame) -> Dict[Tuple, int]:
    """Return {(datetime, option_type): strike} for rows where moneyness == 'ATM'.

    Filters to expiry_type == 'WEEK' and expiry_code == 1 (nearest weekly).
    """
    atm = df[
        (df["moneyness"] == "ATM")
        & (df["expiry_type"] == "WEEK")
        & (df["expiry_code"] == 1)
    ]
    if atm.empty:
        return {}
    out: Dict[Tuple, int] = {}
    for row in atm.itertuples(index=False):
        out[(row.datetime, row.option_type)] = int(row.strike)
    return out


def _format_hhmm(dt) -> str:
    return pd.Timestamp(dt).strftime("%H:%M")


def _block_lock_minute(minute, tf_minutes: int = 5):
    """Given a 1-min bar's datetime, return the minute whose ATM defines the
    strike lock for the 5-min block this 1-min bar belongs to.

    The lock minute is the last 1-min bar of the previous tf_minutes block.
    For tf_minutes=5: bar 13:35 → lock at 13:34; bar 13:39 → lock at 13:34;
    bar 13:40 → lock at 13:39.
    """
    ts = pd.Timestamp(minute)
    floor = ts.floor(f"{tf_minutes}min")
    return floor - pd.Timedelta(minutes=1)


def run_machine_for_day_side(
    df: pd.DataFrame,
    *,
    side: str,
    date: _date,
    expiry_date: _date,
    dte: int,
    morning_low_table: Dict[Tuple, float],
    atm_index: Dict[Tuple, int],
    instrument: str,
    band_pct: float,
    sl_pct: float,
    tp_pct: float,
    scan_start: _time,
    force_exit: _time,
    bullish_required: bool,
    lot_size_total: int,
) -> List[StLowBandTrade]:
    """Run the state machine for one day, one side.

    `df` must already contain st_value / st_dir columns and rows for ALL
    contracts of this side that traded during the day (used for ATM lookup
    + locked-strike row lookup).
    """
    trades: List[StLowBandTrade] = []
    side_df = df[df["option_type"] == side]
    if side_df.empty:
        return trades

    by_strike_minute = side_df.set_index(["strike", "datetime"]).sort_index()

    minutes = sorted(set(side_df["datetime"].tolist()))
    if not minutes:
        return trades

    state: Optional[dict] = None  # None = IDLE; dict with status="OPEN" otherwise

    def row_at(strike, minute):
        try:
            return by_strike_minute.loc[(strike, minute)]
        except KeyError:
            return None

    for idx, minute in enumerate(minutes):
        minute_time = minute.time()
        next_minute = minutes[idx + 1] if idx + 1 < len(minutes) else None

        # ---- OPEN: check exit on locked strike ----
        if state is not None and state["status"] == "OPEN":
            row = row_at(state["strike"], minute)
            if row is not None:
                is_force = minute_time >= force_exit
                result = evaluate_exit(
                    bar_high=float(row["high"]),
                    bar_low=float(row["low"]),
                    bar_close=float(row["close"]),
                    sl=state["sl"], tp=state["tp"],
                    is_force_exit_bar=is_force,
                )
                if result is not None:
                    exit_price, reason = result
                    pnl_points = exit_price - state["entry_price"]
                    pnl_pct = (pnl_points / state["entry_price"]) * 100.0 if state["entry_price"] else 0.0
                    trades.append(StLowBandTrade(
                        date=str(date),
                        instrument=instrument,
                        expiry_date=str(expiry_date),
                        option_type=side,
                        strike=int(state["strike"]),
                        morning_low=state["morning_low"],
                        band_high=state["band_high"],
                        spot_at_entry=state["spot_at_entry"],
                        entry_time=state["entry_time"],
                        entry_price=state["entry_price"],
                        entry_st_value=state["entry_st_value"],
                        entry_trigger_close=state["entry_trigger_close"],
                        spot_at_exit=float(row.get("spot", 0.0)),
                        exit_time=_format_hhmm(minute),
                        exit_price=float(exit_price),
                        exit_reason=reason,
                        dte=int(dte),
                        tp_pct=float(tp_pct),
                        sl_pct=float(sl_pct),
                        pnl_points=float(pnl_points),
                        pnl_pct=float(pnl_pct),
                        pnl_inr=float(pnl_points) * lot_size_total,
                        lot_size=int(lot_size_total),
                    ))
                    state = None  # IDLE — fall through to scan this same minute

        # ---- IDLE: scan for entry ----
        if state is None and scan_start <= minute_time < force_exit:
            # Strike-lock by 5-min block: use the ATM at the close of the
            # previous 5-min bar so the strike doesn't switch mid-block.
            lock_minute = _block_lock_minute(minute, tf_minutes=5)
            atm_strike = atm_index.get((lock_minute, side))
            if atm_strike is None:
                continue
            morning_low_key = (date, atm_strike, side, "WEEK", 1)
            morning_low = morning_low_table.get(morning_low_key, float("nan"))
            if isinstance(morning_low, float) and math.isnan(morning_low):
                continue
            row = row_at(atm_strike, minute)
            if row is None:
                continue
            close_val = float(row.get("close", float("nan")))
            st_val = float(row.get("st_value", float("nan")))
            st_dir = float(row.get("st_dir", float("nan")))

            if not evaluate_entry(
                option_close=close_val, st_dir=st_dir,
                morning_low=morning_low, band_pct=band_pct,
                bullish_required=bullish_required,
            ):
                continue

            if next_minute is None:
                continue
            next_row = row_at(atm_strike, next_minute)
            if next_row is None:
                continue
            entry_price = float(next_row["open"])
            if math.isnan(entry_price):
                continue

            band_high = morning_low * (1 + band_pct / 100.0)
            sl = entry_price * (1 - sl_pct / 100.0)
            tp = entry_price * (1 + tp_pct / 100.0)

            state = {
                "status": "OPEN",
                "strike": int(atm_strike),
                "morning_low": float(morning_low),
                "band_high": float(band_high),
                "sl": float(sl),
                "tp": float(tp),
                "entry_time": _format_hhmm(next_minute),
                "entry_price": entry_price,
                "entry_st_value": st_val,
                "entry_trigger_close": float(row["close"]),
                "spot_at_entry": float(next_row.get("spot", 0.0)),
            }

    # End-of-day safety net: any position still OPEN gets force-closed at the last bar
    if state is not None and state["status"] == "OPEN":
        last_minute = minutes[-1]
        row = row_at(state["strike"], last_minute)
        if row is not None:
            exit_price = float(row["close"])
            pnl_points = exit_price - state["entry_price"]
            pnl_pct = (pnl_points / state["entry_price"]) * 100.0 if state["entry_price"] else 0.0
            trades.append(StLowBandTrade(
                date=str(date),
                instrument=instrument,
                expiry_date=str(expiry_date),
                option_type=side,
                strike=int(state["strike"]),
                morning_low=state["morning_low"],
                band_high=state["band_high"],
                spot_at_entry=state["spot_at_entry"],
                entry_time=state["entry_time"],
                entry_price=state["entry_price"],
                entry_st_value=state["entry_st_value"],
                entry_trigger_close=state["entry_trigger_close"],
                spot_at_exit=float(row.get("spot", 0.0)),
                exit_time=_format_hhmm(last_minute),
                exit_price=exit_price,
                exit_reason="EOD",
                dte=int(dte),
                tp_pct=float(tp_pct),
                sl_pct=float(sl_pct),
                pnl_points=float(pnl_points),
                pnl_pct=float(pnl_pct),
                pnl_inr=float(pnl_points) * lot_size_total,
                lot_size=int(lot_size_total),
            ))

    return trades


from engine.data_loader import load_data  # noqa: E402  (imported here to avoid module-cycle warnings)


def _parse_hhmm(s: str) -> _time:
    hh, mm = s.split(":")
    return _time(int(hh), int(mm))


def _default_loader(start: str, end: str) -> pd.DataFrame:
    """Default loader: read NIFTY weekly options 1-min parquet via load_data."""
    path = os.path.join(BASE_DIR, DATA_PATH["NIFTY"])
    df = load_data(path, start, end, "weekly")
    return df


def run_backtest(
    config: dict,
    loader: Optional[Callable[[str, str], pd.DataFrame]] = None,
) -> List[StLowBandTrade]:
    """Run the SuperTrend Low-Band backtest for the given config.

    `loader(start, end)` returns a DataFrame of 1-min option bars filtered to
    weekly nearest expiry, with columns: datetime, date, time_only, strike,
    option_type, expiry_type, expiry_code, moneyness, open, high, low, close,
    spot, atm_strike. Default loader uses config.DATA_PATH['NIFTY'].
    """
    if loader is None:
        loader = _default_loader

    instrument = config["instrument"]
    if instrument != "NIFTY":
        raise ValueError(f"v1 supports only NIFTY; got {instrument!r}")

    st_cfg = config["supertrend"]
    factor = int(st_cfg["factor"])
    atr_period = int(st_cfg["atr_period"])

    win_cfg = config["first_5min_window"]
    window_start = _parse_hhmm(win_cfg["start"])
    window_end = _parse_hhmm(win_cfg["end"])

    band_pct = float(config["band_pct"])
    dte_table = parse_dte_table(config["dte_table"])

    trading = config["trading"]
    scan_start = _parse_hhmm(trading["scan_start"])
    force_exit = _parse_hhmm(trading["force_exit"])

    lot_multiplier = int(config.get("lot_size", 1))
    lot_size_total = LOT_SIZE[instrument] * lot_multiplier

    start = config["backtest_start"]
    end = config["backtest_end"]

    df = loader(start, end)
    if df.empty:
        return []

    # Pipeline: continuous ST → first-5min low table → ATM index
    df = compute_continuous_supertrend_per_contract(df, factor=factor, atr_period=atr_period)
    morning_low_table = compute_first_5min_low_table(df, window_start, window_end)
    atm_index = build_atm_index(df)

    # Trading-date list for DTE computation (actual market-open days)
    trading_dates = sorted(df["date"].unique())

    all_trades: List[StLowBandTrade] = []
    for trading_date, day_df in df.groupby("date"):
        # Use the NIFTY weekly expiry calendar from config.py
        expiry_date = get_nearest_weekly_expiry(trading_date) or trading_date
        dte = compute_trading_dte(trading_date, expiry_date, trading_dates)
        tp_pct, sl_pct = get_dte_tp_sl(dte, dte_table)

        for side in ("CE", "PE"):
            trades = run_machine_for_day_side(
                day_df, side=side,
                date=trading_date, expiry_date=expiry_date, dte=dte,
                morning_low_table=morning_low_table,
                atm_index=atm_index,
                instrument=instrument,
                band_pct=band_pct, sl_pct=sl_pct, tp_pct=tp_pct,
                scan_start=scan_start, force_exit=force_exit,
                bullish_required=True,
                lot_size_total=lot_size_total,
            )
            all_trades.extend(trades)

    return all_trades


from collections import defaultdict  # noqa: E402


def summarize_trades(trades: List[StLowBandTrade]) -> dict:
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "total_pnl_points": 0.0, "total_pnl_inr": 0.0,
            "by_side": {},
        }
    wins = sum(1 for t in trades if t.pnl_points > 0)
    losses = sum(1 for t in trades if t.pnl_points <= 0)
    total_points = sum(t.pnl_points for t in trades)
    total_inr = sum(t.pnl_inr for t in trades)

    by_side = defaultdict(lambda: {"trades": 0, "pnl_inr": 0.0, "pnl_points": 0.0})
    for t in trades:
        by_side[t.option_type]["trades"] += 1
        by_side[t.option_type]["pnl_inr"] += t.pnl_inr
        by_side[t.option_type]["pnl_points"] += t.pnl_points

    return {
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades),
        "total_pnl_points": total_points,
        "total_pnl_inr": total_inr,
        "by_side": dict(by_side),
    }


def write_trades_csv(trades: List[StLowBandTrade], path: str) -> None:
    """Write trades to CSV. Creates header-only file when empty."""
    if trades:
        df = trades_to_dataframe(trades)
    else:
        df = pd.DataFrame(columns=[f.name for f in fields(StLowBandTrade)])
    df.to_csv(path, index=False)
