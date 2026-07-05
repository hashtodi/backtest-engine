"""
Gamma Blast Backtest Engine.

Strategy:
  Expiry-day only. Buy ATM CE or PE on NIFTY/SENSEX after its premium
  is crushed below a configurable alert level, then recovers above an
  entry level. Fixed-absolute SL and TP. Independent CE/PE machines,
  independent per-instrument P&L. See design spec at
  docs/superpowers/specs/2026-04-23-gamma-blast-design.md.
"""

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date as _date, time as _time
from typing import Callable, List, Optional, Tuple

import pandas as pd

from config import (
    DATA_PATH,
    LOT_SIZE,
    NIFTY_WEEKLY_EXPIRY_DATES,
    SENSEX_WEEKLY_EXPIRY_DATES,
)

logger = logging.getLogger(__name__)


@dataclass
class GammaBlastTrade:
    date: str
    instrument: str
    expiry_date: str
    option_type: str           # "CE" or "PE"
    strike: int

    spot_at_arm: float
    arm_time: str              # "HH:MM"
    arm_premium: float

    spot_at_entry: float
    entry_time: str
    entry_price: float
    entry_trigger_close: float

    spot_at_exit: float
    exit_time: str
    exit_price: float
    exit_reason: str           # "SL" | "TP" | "EOD"

    pnl_points: float
    pnl_inr: float
    lot_size: int


def trades_to_dataframe(trades: List[GammaBlastTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


def should_arm(atm_close: float, alert_price: float, bar_time: _time,
               arm_start: _time, arm_deadline: _time) -> bool:
    """True iff this bar's ATM close triggers an arm transition.

    Arm condition is strict `<` alert_price; time window is inclusive on both ends.
    """
    if atm_close >= alert_price:
        return False
    return arm_start <= bar_time <= arm_deadline


def evaluate_entry_trigger(
    bar_open: float, bar_close: float, bar_low: float,
    next_open: float,
    alert_price: float, entry_price: float, sl: float, tp: float,
    already_armed: bool,
) -> Tuple[str, Optional[float]]:
    """Decide what to do at the close of a potential trigger bar.

    Returns one of:
      ("enter", next_open)           — transition to OPEN at next_open
      ("skip",  "gap_above_tp")      — trigger valid but next_open > tp
      ("skip",  "gap_below_sl")      — trigger valid but next_open < sl
      ("no_trigger", None)           — conditions not met
    """
    if bar_close <= entry_price:
        return ("no_trigger", None)

    if already_armed:
        trigger_valid = True
    else:
        # Same-bar whip: need dip (low <= alert_price) AND green candle
        whip_dip = bar_low <= alert_price
        green = bar_close > bar_open
        trigger_valid = whip_dip and green

    if not trigger_valid:
        return ("no_trigger", None)

    if next_open > tp:
        return ("skip", "gap_above_tp")
    if next_open < sl:
        return ("skip", "gap_below_sl")
    return ("enter", next_open)


def evaluate_exit(
    bar_high: float, bar_low: float, bar_close: float,
    sl: float, tp: float, is_force_exit_bar: bool,
) -> Optional[Tuple[float, str]]:
    """Return (exit_price, exit_reason) if this bar exits the position, else None.

    Priority: SL > TP > EOD. SL/TP fills assume exact-level fills (the
    realism approximation is acknowledged in the spec).
    """
    sl_hit = bar_low <= sl
    tp_hit = bar_high >= tp
    if sl_hit:
        return (float(sl), "SL")
    if tp_hit:
        return (float(tp), "TP")
    if is_force_exit_bar:
        return (float(bar_close), "EOD")
    return None


def _format_hhmm(dt) -> str:
    return pd.Timestamp(dt).strftime("%H:%M")


def run_machine_for_day(
    df: pd.DataFrame,
    *,
    instrument: str,
    option_type: str,
    day: _date,
    expiry_date: _date,
    lot_size: int,
    lot_multiplier: int,
    params: dict,
    timing: dict,
) -> List[GammaBlastTrade]:
    """Run the state machine for one day, one option_type.

    Expects df pre-filtered to (expiry_code == 1, option_type == given).
    Minutely bars sorted ascending by datetime. Returns list of completed trades.
    """
    trades: List[GammaBlastTrade] = []
    if df.empty:
        return trades

    atm_by_minute = (
        df[df["moneyness"] == "ATM"]
        .set_index("datetime")
        [["strike", "open", "high", "low", "close", "spot"]]
    )
    by_strike_minute = df.set_index(["strike", "datetime"])

    alert_price = float(params["alert_price"])
    entry_price = float(params["entry_price"])
    sl = float(params["sl"])
    tp = float(params["tp"])
    arm_start = timing["arm_start"]
    arm_deadline = timing["arm_deadline"]
    entry_deadline = timing["entry_deadline"]
    force_exit = timing["force_exit"]

    minutes = sorted(set(df["datetime"].tolist()))

    state = None  # None=IDLE; dict with "status": "ARMED" | "OPEN"

    def atm_row_at(minute):
        try:
            return atm_by_minute.loc[minute]
        except KeyError:
            return None

    def locked_row_at(strike, minute):
        try:
            return by_strike_minute.loc[(strike, minute)]
        except KeyError:
            return None

    for idx, minute in enumerate(minutes):
        minute_time = minute.time()
        next_minute = minutes[idx + 1] if idx + 1 < len(minutes) else None

        # ---- OPEN: check exits on locked strike ----
        if state is not None and state["status"] == "OPEN":
            row = locked_row_at(state["strike"], minute)
            if row is not None:
                is_force = minute_time >= force_exit
                result = evaluate_exit(
                    bar_high=float(row["high"]),
                    bar_low=float(row["low"]),
                    bar_close=float(row["close"]),
                    sl=sl, tp=tp,
                    is_force_exit_bar=is_force,
                )
                if result is not None:
                    exit_price, reason = result
                    pnl_points = exit_price - state["entry_price"]
                    trade = GammaBlastTrade(
                        date=str(day),
                        instrument=instrument,
                        expiry_date=str(expiry_date),
                        option_type=option_type,
                        strike=int(state["strike"]),
                        spot_at_arm=state["spot_at_arm"],
                        arm_time=state["arm_time"],
                        arm_premium=state["arm_premium"],
                        spot_at_entry=state["spot_at_entry"],
                        entry_time=state["entry_time"],
                        entry_price=state["entry_price"],
                        entry_trigger_close=state["entry_trigger_close"],
                        spot_at_exit=float(row["spot"]),
                        exit_time=_format_hhmm(minute),
                        exit_price=float(exit_price),
                        exit_reason=reason,
                        pnl_points=float(pnl_points),
                        pnl_inr=float(pnl_points) * lot_size * lot_multiplier,
                        lot_size=lot_size * lot_multiplier,
                    )
                    trades.append(trade)
                    state = None  # fall through to IDLE for same-minute re-arm

        # ---- ARMED: check entry trigger on locked strike ----
        if state is not None and state["status"] == "ARMED":
            row = locked_row_at(state["strike"], minute)
            if row is not None and next_minute is not None:
                if next_minute.time() <= entry_deadline:
                    next_row = locked_row_at(state["strike"], next_minute)
                    if next_row is not None:
                        result = evaluate_entry_trigger(
                            bar_open=float(row["open"]),
                            bar_close=float(row["close"]),
                            bar_low=float(row["low"]),
                            next_open=float(next_row["open"]),
                            alert_price=alert_price, entry_price=entry_price,
                            sl=sl, tp=tp,
                            already_armed=True,
                        )
                        action, payload = result
                        if action == "enter":
                            state = {
                                "status": "OPEN",
                                "strike": state["strike"],
                                "spot_at_arm": state["spot_at_arm"],
                                "arm_time": state["arm_time"],
                                "arm_premium": state["arm_premium"],
                                "spot_at_entry": float(next_row["spot"]),
                                "entry_time": _format_hhmm(next_minute),
                                "entry_price": float(payload),
                                "entry_trigger_close": float(row["close"]),
                            }
                            continue
                        elif action == "skip":
                            state = None
            if state is not None and state["status"] == "ARMED" and minute_time > entry_deadline:
                state = None

        # ---- IDLE: whip first, then plain arm ----
        if state is None:
            atm = atm_row_at(minute)
            in_arm_window = arm_start <= minute_time <= arm_deadline
            if atm is not None and in_arm_window:
                a_open = float(atm["open"])
                a_close = float(atm["close"])
                a_low = float(atm["low"])

                whip_candidate = (a_low <= alert_price) and (a_close > entry_price)
                green = a_close > a_open
                red_or_doji = a_close <= a_open

                if whip_candidate and green:
                    if next_minute is not None and next_minute.time() <= entry_deadline:
                        next_row = locked_row_at(int(atm["strike"]), next_minute)
                        if next_row is not None:
                            result = evaluate_entry_trigger(
                                bar_open=a_open, bar_close=a_close, bar_low=a_low,
                                next_open=float(next_row["open"]),
                                alert_price=alert_price, entry_price=entry_price,
                                sl=sl, tp=tp,
                                already_armed=False,
                            )
                            action, payload = result
                            if action == "enter":
                                state = {
                                    "status": "OPEN",
                                    "strike": int(atm["strike"]),
                                    "spot_at_arm": float(atm["spot"]),
                                    "arm_time": _format_hhmm(minute),
                                    "arm_premium": a_close,
                                    "spot_at_entry": float(next_row["spot"]),
                                    "entry_time": _format_hhmm(next_minute),
                                    "entry_price": float(payload),
                                    "entry_trigger_close": a_close,
                                }
                                continue
                elif whip_candidate and red_or_doji:
                    pass
                elif should_arm(
                    atm_close=a_close, alert_price=alert_price,
                    bar_time=minute_time, arm_start=arm_start, arm_deadline=arm_deadline,
                ):
                    state = {
                        "status": "ARMED",
                        "strike": int(atm["strike"]),
                        "spot_at_arm": float(atm["spot"]),
                        "arm_time": _format_hhmm(minute),
                        "arm_premium": a_close,
                    }

    # End-of-day safety: force close any dangling OPEN position
    if state is not None and state["status"] == "OPEN":
        last_minute = minutes[-1]
        last_row = locked_row_at(state["strike"], last_minute)
        if last_row is not None:
            exit_price = float(last_row["close"])
            pnl_points = exit_price - state["entry_price"]
            trade = GammaBlastTrade(
                date=str(day),
                instrument=instrument,
                expiry_date=str(expiry_date),
                option_type=option_type,
                strike=int(state["strike"]),
                spot_at_arm=state["spot_at_arm"],
                arm_time=state["arm_time"],
                arm_premium=state["arm_premium"],
                spot_at_entry=state["spot_at_entry"],
                entry_time=state["entry_time"],
                entry_price=state["entry_price"],
                entry_trigger_close=state["entry_trigger_close"],
                spot_at_exit=float(last_row["spot"]),
                exit_time=_format_hhmm(last_minute),
                exit_price=exit_price,
                exit_reason="EOD",
                pnl_points=float(pnl_points),
                pnl_inr=float(pnl_points) * lot_size * lot_multiplier,
                lot_size=lot_size * lot_multiplier,
            )
            trades.append(trade)

    return trades


def _parse_hhmm(s: str) -> _time:
    hh, mm = s.split(":")
    return _time(int(hh), int(mm))


def _expiry_list_for(instrument: str) -> List[_date]:
    key = instrument.upper()
    if key == "NIFTY":
        return list(NIFTY_WEEKLY_EXPIRY_DATES)
    if key == "SENSEX":
        return list(SENSEX_WEEKLY_EXPIRY_DATES)
    raise ValueError(f"Unsupported instrument: {instrument}")


def _params_are_valid(p: dict) -> bool:
    required = ("alert_price", "entry_price", "sl", "tp")
    return all(p.get(k) is not None for k in required)


def _default_loader(instrument: str, day: _date) -> pd.DataFrame:
    """Load one day's option rows for an instrument from the parquet file.

    The parquet stores `datetime` as ISO strings and `ts` as unix epoch
    seconds. We filter on `ts` (int) for efficiency and parse `datetime`
    to tz-naive pandas Timestamps (IST-local) for the state machine.
    """
    path = DATA_PATH[instrument.upper()]
    start_ts = int(pd.Timestamp(day).tz_localize("Asia/Kolkata").timestamp())
    end_ts = start_ts + 86400
    df = pd.read_parquet(
        path,
        filters=[
            ("ts", ">=", start_ts),
            ("ts", "<", end_ts),
            ("expiry_code", "==", 1),
            ("expiry_type", "==", "WEEK"),
        ],
    )
    if not df.empty:
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return df


def run_backtest(
    config: dict,
    loader: Callable[[str, _date], pd.DataFrame] = None,
) -> List[GammaBlastTrade]:
    """Run the Gamma Blast backtest across expiry days."""
    if loader is None:
        loader = _default_loader

    timing = {
        "arm_start": _parse_hhmm(config["timing"]["arm_start"]),
        "arm_deadline": _parse_hhmm(config["timing"]["arm_deadline"]),
        "entry_deadline": _parse_hhmm(config["timing"]["entry_deadline"]),
        "force_exit": _parse_hhmm(config["timing"]["force_exit"]),
    }

    start = pd.Timestamp(config["backtest_start"]).date()
    end = pd.Timestamp(config["backtest_end"]).date()
    lot_multiplier = int(config.get("lot_size", 1))

    all_trades: List[GammaBlastTrade] = []
    for instrument in config["instruments"]:
        params = config["params"].get(instrument, {})
        if not _params_are_valid(params):
            logger.info("Skipping %s: params incomplete", instrument)
            continue
        expiries = [d for d in _expiry_list_for(instrument) if start <= d <= end]
        lot_size = LOT_SIZE[instrument.upper()]

        for day in expiries:
            day_df = loader(instrument, day)
            if day_df.empty:
                logger.debug("No data for %s %s", instrument, day)
                continue

            for option_type in ("CE", "PE"):
                ot_df = day_df[
                    (day_df["option_type"] == option_type) &
                    (day_df["expiry_code"] == 1)
                ].copy()
                if ot_df.empty:
                    continue
                ot_df = ot_df.sort_values("datetime").reset_index(drop=True)

                trades = run_machine_for_day(
                    ot_df,
                    instrument=instrument,
                    option_type=option_type,
                    day=day,
                    expiry_date=day,
                    lot_size=lot_size,
                    lot_multiplier=lot_multiplier,
                    params=params,
                    timing=timing,
                )
                all_trades.extend(trades)

    return all_trades


def summarize_trades(trades: List[GammaBlastTrade]) -> dict:
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "total_pnl_points": 0.0, "total_pnl_inr": 0.0,
            "by_instrument": {},
        }

    wins = sum(1 for t in trades if t.pnl_points > 0)
    losses = sum(1 for t in trades if t.pnl_points <= 0)
    total_points = sum(t.pnl_points for t in trades)
    total_inr = sum(t.pnl_inr for t in trades)

    by_inst = defaultdict(lambda: {"trades": 0, "pnl_inr": 0.0, "pnl_points": 0.0})
    for t in trades:
        by_inst[t.instrument]["trades"] += 1
        by_inst[t.instrument]["pnl_inr"] += t.pnl_inr
        by_inst[t.instrument]["pnl_points"] += t.pnl_points

    return {
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades),
        "total_pnl_points": total_points,
        "total_pnl_inr": total_inr,
        "by_instrument": dict(by_inst),
    }


def write_trades_csv(trades: List[GammaBlastTrade], path: str) -> None:
    """Write trades to a CSV at `path`. Creates header-only file if no trades."""
    if trades:
        df = trades_to_dataframe(trades)
    else:
        df = pd.DataFrame(columns=[f.name for f in fields(GammaBlastTrade)])
    df.to_csv(path, index=False)
