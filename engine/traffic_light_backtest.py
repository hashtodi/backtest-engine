"""
Traffic Light Backtest Engine.

Strategy summary:
  - Pair detection on NIFTY spot 1-min: an opposite-color two-candle pair
    (green-red or red-green) defines pair_high = max(highs) and
    pair_low = min(lows) as breakout levels.
  - Filter at pair formation (fixed for life of pair):
      CE blocked iff RSI(14) > 70 on both pair bars AND close <= EMA(15)
      PE blocked iff RSI(14) < 30 on both pair bars AND close >= EMA(15)
      If both sides blocked, skip the pair entirely.
  - Lock first pair, never refresh: while a pair is armed or a trade is
    open, new opposite-color pairs are ignored.
  - Breakout: close-strict above pair_high (CE) or below pair_low (PE).
    Entry fills at NEXT 1-min option open at ATM strike (walks OTM
    +-1..+-4 if premium * lot_size >= premium_budget).
  - SL on spot = pair_low - buffer (CE) / pair_high + buffer (PE).
    TP on spot = pair_high + range * rr (CE) / pair_low - range * rr (PE).
  - Spot wick (high/low) triggers SL/TP; exit fills at NEXT 1-min option open.
  - Force exit at force_exit time: exit at that bar's option close, EOD.
  - Entry deadline: last trigger bar at entry_deadline time (inclusive);
    pair expires after that.
  - One trade at a time. After resolution, rolling scan resumes from the
    very next bar close.
"""

import logging
import math
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date as _date, datetime as _dt, time as _time
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from config import (
    DATA_PATH,
    LOT_SIZE,
    NIFTY_WEEKLY_EXPIRY_DATES,
    SPOT_DATA_PATH,
    STRIKE_ROUNDING,
    get_nearest_weekly_expiry,
)
from indicators.rsi import RSI
from indicators.ema import EMA

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class TrafficLightTrade:
    date: str
    instrument: str
    expiry_date: str
    option_type: str            # "CE" or "PE"
    strike: int
    strike_offset: int          # 0 = ATM, +N CE-OTM, -N PE-OTM

    pair_high: float            # spot levels at pair formation
    pair_low: float
    pair_bar1_time: str         # "HH:MM"
    pair_bar2_time: str
    range_size: float

    sl_spot: float              # spot exit levels
    tp_spot: float

    entry_time: str
    spot_at_entry: float
    entry_price: float          # option open at fill bar

    exit_time: str
    spot_at_exit: float
    exit_price: float
    exit_reason: str            # "SL" | "TP" | "EOD"

    pnl_points: float
    pnl_inr: float
    lot_size: int


def trades_to_dataframe(trades: List[TrafficLightTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=[f.name for f in fields(TrafficLightTrade)])
    return pd.DataFrame([asdict(t) for t in trades])


def write_trades_csv(trades: List[TrafficLightTrade], path: str) -> None:
    df = trades_to_dataframe(trades)
    df.to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Pure helpers (state-free, easy to test)
# ---------------------------------------------------------------------------

def candle_color(open_: float, close_: float) -> str:
    """G = green (close > open), R = red (close < open), D = doji (close == open)."""
    if close_ > open_:
        return "G"
    if close_ < open_:
        return "R"
    return "D"


def is_opposite_pair(
    prev_open: float, prev_close: float,
    this_open: float, this_close: float,
) -> bool:
    """True iff (prev, this) is G-R or R-G. Doji bars never form a pair."""
    p = candle_color(prev_open, prev_close)
    t = candle_color(this_open, this_close)
    return (p == "G" and t == "R") or (p == "R" and t == "G")


def evaluate_pair_filters(
    rsi_prev: float, rsi_now: float,
    ema_now: float, close_now: float,
    rsi_overbought: float, rsi_oversold: float,
) -> Tuple[bool, bool]:
    """Decide which sides are blocked at pair formation.

    CE blocked iff RSI > overbought on BOTH pair bars AND close <= EMA at pair-bar-2.
    PE blocked iff RSI < oversold on BOTH pair bars AND close >= EMA at pair-bar-2.
    NaN inputs -> both blocked (filter undecidable).

    Returns (ce_blocked, pe_blocked).
    """
    if pd.isna(rsi_prev) or pd.isna(rsi_now) or pd.isna(ema_now):
        return (True, True)
    ce_overbought = rsi_prev > rsi_overbought and rsi_now > rsi_overbought
    pe_oversold = rsi_prev < rsi_oversold and rsi_now < rsi_oversold
    ce_blocked = bool(ce_overbought and (close_now <= ema_now))
    pe_blocked = bool(pe_oversold and (close_now >= ema_now))
    return (ce_blocked, pe_blocked)


def evaluate_breakout(
    spot_close: float,
    pair_high: float, pair_low: float,
    ce_armed: bool, pe_armed: bool,
) -> Optional[str]:
    """Close-strict breakout check. Returns 'CE', 'PE', or None.

    Close can't be both > high and < low, so no tie possible.
    """
    if ce_armed and spot_close > pair_high:
        return "CE"
    if pe_armed and spot_close < pair_low:
        return "PE"
    return None


def evaluate_open_exit(
    side: str,
    spot_high: float, spot_low: float,
    sl_spot: float, tp_spot: float,
) -> Optional[str]:
    """Wick-based SL/TP detection on the spot bar for an OPEN position.

    CE (long calls): SL hit when spot_low <= sl_spot; TP when spot_high >= tp_spot.
    PE (long puts):  SL hit when spot_high >= sl_spot; TP when spot_low <= tp_spot.
    SL wins ties.
    """
    if side == "CE":
        sl_hit = spot_low <= sl_spot
        tp_hit = spot_high >= tp_spot
    else:
        sl_hit = spot_high >= sl_spot
        tp_hit = spot_low <= tp_spot
    if sl_hit:
        return "SL"
    if tp_hit:
        return "TP"
    return None


def round_to_atm(spot: float, rounding: int) -> int:
    return int(round(spot / rounding) * rounding)


def select_strike(
    spot_at_trigger: float,
    side: str,
    rounding: int,
    options_at_minute: Dict[int, float],   # actual_strike -> option close
    max_offset: int,
    lot_size: int,
    premium_budget_inr: float,
    min_offset: int = 0,
) -> Optional[Tuple[int, int, float]]:
    """Walk OTM offsets [min_offset .. max_offset] until premium*lot_size < budget.

    For CE, OTM direction is +offset (higher strikes).
    For PE, OTM direction is -offset (lower strikes).
    min_offset=0 starts at ATM (default). Set >0 to skip ATM and start deeper OTM.

    Strikes are looked up by ACTUAL STRIKE VALUE (atm_strike + signed_offset *
    rounding) rather than the data's strike_offset column — this keeps the
    engine's ATM computation as the source of truth and avoids any half-strike
    rounding disagreement between Python's banker's rounding and the data
    feed's rounding rule.

    Returns (strike, signed_offset, opt_close) or None if budget not satisfied
    within the range.
    """
    if min_offset < 0 or max_offset < min_offset:
        return None
    atm_strike = round_to_atm(spot_at_trigger, rounding)
    direction = 1 if side == "CE" else -1
    for offset_walk in range(min_offset, max_offset + 1):
        signed_offset = direction * offset_walk
        candidate_strike = atm_strike + signed_offset * rounding
        opt_close = options_at_minute.get(candidate_strike)
        if opt_close is None or pd.isna(opt_close) or opt_close <= 0:
            continue
        if opt_close * lot_size < premium_budget_inr:
            return (candidate_strike, signed_offset, float(opt_close))
    return None


# ---------------------------------------------------------------------------
# Per-day state machine
# ---------------------------------------------------------------------------

IDLE = "IDLE"
PAIR_ARMED = "PAIR_ARMED"
ENTRY_PENDING = "ENTRY_PENDING"
OPEN = "OPEN"
EXIT_PENDING = "EXIT_PENDING"


def _format_hhmm(dt) -> str:
    return pd.Timestamp(dt).strftime("%H:%M")


def _options_at_minute_for_side(
    options_day: pd.DataFrame,
    minute: pd.Timestamp,
    side: str,
    price_col: str = "close",
) -> Dict[int, float]:
    """Build {actual_strike: price} for one minute, one option_type.

    Indexed by actual strike value (not data's strike_offset) so the engine's
    own ATM calculation is the source of truth for the strike walk.

    price_col selects which OHLC column to read ('open' for entry-bar fills,
    'close' for trigger-bar reference snapshots, etc.).
    """
    sub = options_day[
        (options_day["datetime"] == minute)
        & (options_day["option_type"] == side)
    ]
    out: Dict[int, float] = {}
    for strike, price in zip(sub["strike"].tolist(), sub[price_col].tolist()):
        out[int(strike)] = float(price)
    return out


def _option_row_at(
    options_day: pd.DataFrame,
    minute: pd.Timestamp,
    side: str,
    strike: int,
) -> Optional[pd.Series]:
    sub = options_day[
        (options_day["datetime"] == minute)
        & (options_day["option_type"] == side)
        & (options_day["strike"] == strike)
    ]
    if sub.empty:
        return None
    return sub.iloc[0]


def run_machine_for_day(
    spot_day: pd.DataFrame,
    options_day: pd.DataFrame,
    *,
    day: _date,
    expiry_date: _date,
    instrument: str,
    lot_size: int,
    lot_multiplier: int,
    params: dict,
    timing: dict,
    strike_rounding: int,
) -> List[TrafficLightTrade]:
    """Run the Traffic Light state machine for a single trading day.

    Args:
        spot_day:    spot 1-min bars (datetime, open, high, low, close,
                     rsi, ema) sorted ascending, in-day only.
        options_day: options 1-min rows for this day, weekly expiry_code=1,
                     filtered to current expiry.
        day:         the trading date.
        expiry_date: weekly expiry date (=day on expiry, else next weekly).
        instrument:  e.g. "NIFTY".
        lot_size:    underlying lot multiplier (e.g. NIFTY=65).
        lot_multiplier: JSON "lot_size" — number of lots (default 1).
        params:      strategy params (rsi/ema periods, rr, buffer, budget, etc.).
        timing:      parsed _time objects (scan_start, entry_deadline, force_exit).
    """
    trades: List[TrafficLightTrade] = []
    if spot_day.empty:
        return trades

    rr_ratio = float(params["rr_ratio"])
    sl_buffer = float(params["sl_buffer"])
    rsi_overbought = float(params["rsi_overbought"])
    rsi_oversold = float(params["rsi_oversold"])
    max_otm_offset = int(params["max_otm_offset"])
    min_otm_offset = int(params.get("min_otm_offset", 0))
    premium_budget = float(params["premium_budget_inr"])

    scan_start: _time = timing["scan_start"]
    entry_deadline: _time = timing["entry_deadline"]
    force_exit: _time = timing["force_exit"]

    state: dict = {"status": IDLE}
    bars = spot_day.reset_index(drop=True)
    prev_idx: Optional[int] = None
    # Strict no-overlap gate: the most recent bar at which a trade resolved
    # (exit fill, budget-skip at entry, or entry data-gap cancel). The next
    # pair_bar1 must be STRICTLY AFTER this minute, so bars during the trade's
    # life can never participate in the next pair.
    last_resolution_minute = None

    def record_trade(s: dict, *, exit_price: float, exit_reason: str,
                     exit_time, spot_at_exit: float) -> None:
        pnl_points = float(exit_price) - float(s["entry_price"])
        trades.append(TrafficLightTrade(
            date=str(day),
            instrument=instrument,
            expiry_date=str(expiry_date),
            option_type=s["side"],
            strike=int(s["strike"]),
            strike_offset=int(s["signed_offset"]),
            pair_high=float(s["pair_high"]),
            pair_low=float(s["pair_low"]),
            pair_bar1_time=str(s["pair_bar1_time"]),
            pair_bar2_time=str(s["pair_bar2_time"]),
            range_size=float(s["pair_high"] - s["pair_low"]),
            sl_spot=float(s["sl_spot"]),
            tp_spot=float(s["tp_spot"]),
            entry_time=str(s["entry_time"]),
            spot_at_entry=float(s["spot_at_entry"]),
            entry_price=float(s["entry_price"]),
            exit_time=_format_hhmm(exit_time),
            spot_at_exit=float(spot_at_exit),
            exit_price=float(exit_price),
            exit_reason=exit_reason,
            pnl_points=pnl_points,
            pnl_inr=pnl_points * lot_size * lot_multiplier,
            lot_size=lot_size * lot_multiplier,
        ))

    for idx in range(len(bars)):
        row = bars.iloc[idx]
        minute = row["datetime"]
        minute_time = minute.time()
        is_force_exit_bar = (minute_time >= force_exit)
        before_scan_start = (minute_time < scan_start)

        # --- Step 1: fill any pending exit from prior bar (option open) ---
        if state["status"] == EXIT_PENDING:
            opt_row = _option_row_at(options_day, minute, state["side"], state["strike"])
            if opt_row is not None and not pd.isna(opt_row["open"]):
                exit_price = float(opt_row["open"])
                record_trade(
                    state,
                    exit_price=exit_price,
                    exit_reason=state["exit_reason"],
                    exit_time=minute,
                    spot_at_exit=float(row["open"]),
                )
                state = {"status": IDLE}
                last_resolution_minute = minute
            else:
                # No option row at fill bar — fall back to trigger close as exit,
                # using the exit_trigger_time and prior-bar spot. This is a data-gap
                # safety net; expected to be rare for liquid strikes.
                logger.warning(
                    "Missing option fill at %s for strike=%s side=%s; using trigger close",
                    minute, state["strike"], state["side"],
                )
                record_trade(
                    state,
                    exit_price=float(state["trigger_option_close"]),
                    exit_reason=state["exit_reason"],
                    exit_time=state["exit_trigger_time"],
                    spot_at_exit=float(state["spot_at_exit_trigger"]),
                )
                state = {"status": IDLE}
                last_resolution_minute = minute

        # --- Step 2: fill any pending entry from prior bar ---
        # All entry-bar consistent (Option C):
        #   - ATM from spot at entry-bar open
        #   - Premium budget check uses entry-bar option OPEN (= the fill price)
        #   - Fill at entry-bar option open
        # trigger_option_close recorded separately for analysis only.
        if state["status"] == ENTRY_PENDING:
            spot_at_entry = float(row["open"])
            options_at_entry = _options_at_minute_for_side(
                options_day, minute, state["side"], price_col="open",
            )
            selection = select_strike(
                spot_at_trigger=spot_at_entry,
                side=state["side"],
                rounding=strike_rounding,
                options_at_minute=options_at_entry,
                max_offset=max_otm_offset,
                min_offset=min_otm_offset,
                lot_size=lot_size,
                premium_budget_inr=premium_budget,
            )
            if selection is None:
                logger.info(
                    "Premium budget skip (at entry) %s %s at %s pair=[%s,%s]",
                    instrument, state["side"], minute,
                    state["pair_low"], state["pair_high"],
                )
                state = {"status": IDLE}
                last_resolution_minute = minute
            else:
                strike, signed_offset, entry_open_price = selection
                state = {
                    "status": OPEN,
                    "side": state["side"],
                    "strike": strike,
                    "signed_offset": signed_offset,
                    "pair_high": state["pair_high"],
                    "pair_low": state["pair_low"],
                    "pair_bar1_time": state["pair_bar1_time"],
                    "pair_bar2_time": state["pair_bar2_time"],
                    "sl_spot": state["sl_spot"],
                    "tp_spot": state["tp_spot"],
                    "entry_time": _format_hhmm(minute),
                    "spot_at_entry": spot_at_entry,
                    "entry_price": entry_open_price,
                }

        # --- Step 3: OPEN — force exit at force_exit bar, else check SL/TP wick ---
        if state["status"] == OPEN:
            if is_force_exit_bar:
                opt_row = _option_row_at(options_day, minute, state["side"], state["strike"])
                if opt_row is not None and not pd.isna(opt_row["close"]):
                    exit_price = float(opt_row["close"])
                else:
                    exit_price = float(state["entry_price"])  # fallback: flat
                record_trade(
                    state,
                    exit_price=exit_price,
                    exit_reason="EOD",
                    exit_time=minute,
                    spot_at_exit=float(row["close"]),
                )
                state = {"status": IDLE}
            else:
                exit_reason = evaluate_open_exit(
                    state["side"],
                    float(row["high"]), float(row["low"]),
                    state["sl_spot"], state["tp_spot"],
                )
                if exit_reason is not None:
                    state = {
                        **state,
                        "status": EXIT_PENDING,
                        "exit_reason": exit_reason,
                        "exit_trigger_time": minute,
                        "spot_at_exit_trigger": float(row["close"]),
                    }

        # --- Step 4: PAIR_ARMED — check breakout on this bar's close ---
        if state["status"] == PAIR_ARMED:
            if minute_time > entry_deadline:
                state = {"status": IDLE}
            else:
                breakout_side = evaluate_breakout(
                    float(row["close"]),
                    state["pair_high"], state["pair_low"],
                    state["ce_armed"], state["pe_armed"],
                )
                if breakout_side is not None:
                    # Strike NOT selected here — deferred to entry-bar (Step 2).
                    # Only SL/TP levels (pair-based) are locked now.
                    range_size = float(state["pair_high"] - state["pair_low"])
                    if breakout_side == "CE":
                        sl_spot = float(state["pair_low"]) - sl_buffer
                        tp_spot = float(state["pair_high"]) + range_size * rr_ratio
                    else:
                        sl_spot = float(state["pair_high"]) + sl_buffer
                        tp_spot = float(state["pair_low"]) - range_size * rr_ratio
                    state = {
                        "status": ENTRY_PENDING,
                        "side": breakout_side,
                        "pair_high": state["pair_high"],
                        "pair_low": state["pair_low"],
                        "pair_bar1_time": state["pair_bar1_time"],
                        "pair_bar2_time": state["pair_bar2_time"],
                        "sl_spot": sl_spot,
                        "tp_spot": tp_spot,
                    }

        # --- Step 5: IDLE — try to form a new pair with (prev_idx, idx) ---
        if state["status"] == IDLE and prev_idx is not None and not before_scan_start:
            prev_row = bars.iloc[prev_idx]
            prev_time = prev_row["datetime"].time()
            # Strict no-overlap: pair_bar1 (= prev_row) must be strictly AFTER
            # the most recent resolution bar. Skips the fill bar AND the bar
            # immediately after, so the earliest new pair starts on bars
            # cleanly outside the prior trade's life.
            past_resolution = (
                last_resolution_minute is None
                or prev_row["datetime"] > last_resolution_minute
            )
            # Require BOTH pair bars to be in the scan window (>= scan_start).
            # Also: do not arm if past the entry deadline (would expire immediately).
            if past_resolution and prev_time >= scan_start and minute_time <= entry_deadline and not is_force_exit_bar:
                if is_opposite_pair(
                    float(prev_row["open"]), float(prev_row["close"]),
                    float(row["open"]), float(row["close"]),
                ):
                    ce_blocked, pe_blocked = evaluate_pair_filters(
                        rsi_prev=float(prev_row["rsi"]) if not pd.isna(prev_row["rsi"]) else float("nan"),
                        rsi_now=float(row["rsi"]) if not pd.isna(row["rsi"]) else float("nan"),
                        ema_now=float(row["ema"]) if not pd.isna(row["ema"]) else float("nan"),
                        close_now=float(row["close"]),
                        rsi_overbought=rsi_overbought,
                        rsi_oversold=rsi_oversold,
                    )
                    if not (ce_blocked and pe_blocked):
                        pair_high = max(float(prev_row["high"]), float(row["high"]))
                        pair_low = min(float(prev_row["low"]), float(row["low"]))
                        state = {
                            "status": PAIR_ARMED,
                            "ce_armed": not ce_blocked,
                            "pe_armed": not pe_blocked,
                            "pair_high": pair_high,
                            "pair_low": pair_low,
                            "pair_bar1_time": _format_hhmm(prev_row["datetime"]),
                            "pair_bar2_time": _format_hhmm(minute),
                        }

        prev_idx = idx

    # --- End-of-day safety: any dangling position is force-closed ---
    if state["status"] in (OPEN, EXIT_PENDING):
        last_row = bars.iloc[-1]
        last_minute = last_row["datetime"]
        opt_row = _option_row_at(options_day, last_minute, state["side"], state["strike"])
        if opt_row is not None and not pd.isna(opt_row["close"]):
            exit_price = float(opt_row["close"])
        else:
            exit_price = float(state["entry_price"])  # fallback: flat
        record_trade(
            state,
            exit_price=exit_price,
            exit_reason="EOD",
            exit_time=last_minute,
            spot_at_exit=float(last_row["close"]),
        )

    return trades


# ---------------------------------------------------------------------------
# Multi-day driver: data loading + indicator pre-compute
# ---------------------------------------------------------------------------

def _parse_hhmm(s: str) -> _time:
    hh, mm = s.split(":")
    return _time(int(hh), int(mm))


def _load_spot_with_indicators(
    instrument: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    rsi_period: int,
    ema_period: int,
    spot_loader: Optional[Callable[[str], pd.DataFrame]] = None,
) -> pd.DataFrame:
    """Load spot 1-min data and attach rsi / ema columns.

    Loads from start - 5 calendar days (warmup) to end + 1 day. RSI/EMA are
    computed continuously across the entire window so indicators are
    well-warmed by the time scanning hits backtest_start.
    """
    if spot_loader is None:
        path = SPOT_DATA_PATH[instrument.upper()]
        df = pd.read_parquet(path)
    else:
        df = spot_loader(instrument)
    if df.empty:
        return df

    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)

    warmup_start = start - pd.Timedelta(days=5)
    end_exclusive = end + pd.Timedelta(days=1)
    df = df[(df["datetime"] >= warmup_start) & (df["datetime"] < end_exclusive)]
    df = df.sort_values("datetime").reset_index(drop=True)
    if df.empty:
        return df

    rsi = RSI(name="rsi", period=rsi_period).calculate(df["close"])
    ema = EMA(name="ema", period=ema_period).calculate(df["close"])
    df["rsi"] = rsi.values
    df["ema"] = ema.values

    df["date"] = df["datetime"].dt.date
    return df


def _default_options_loader(instrument: str, day: _date) -> pd.DataFrame:
    """Load one day of options data (weekly expiry_code=1) for an instrument."""
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
    spot_loader: Optional[Callable[[str], pd.DataFrame]] = None,
    options_loader: Optional[Callable[[str, _date], pd.DataFrame]] = None,
) -> List[TrafficLightTrade]:
    """Run Traffic Light across the configured date range.

    `spot_loader(instrument) -> DataFrame` and
    `options_loader(instrument, day) -> DataFrame` are injection seams for
    tests (they receive the raw, unprocessed frames).
    """
    if options_loader is None:
        options_loader = _default_options_loader

    instrument = config.get("instrument", "NIFTY").upper()
    params = config["params"]
    timing = {
        "scan_start": _parse_hhmm(config["timing"]["scan_start"]),
        "entry_deadline": _parse_hhmm(config["timing"]["entry_deadline"]),
        "force_exit": _parse_hhmm(config["timing"]["force_exit"]),
    }
    lot_multiplier = int(config.get("lot_size", 1))
    strike_rounding = STRIKE_ROUNDING.get(instrument, 50)
    lot_size = LOT_SIZE[instrument]

    start = pd.Timestamp(config["backtest_start"])
    end = pd.Timestamp(config["backtest_end"])

    spot_full = _load_spot_with_indicators(
        instrument, start, end,
        rsi_period=int(params["rsi_period"]),
        ema_period=int(params["ema_period"]),
        spot_loader=spot_loader,
    )
    if spot_full.empty:
        logger.warning("No spot data loaded for %s in window", instrument)
        return []

    start_date = start.date()
    end_date = end.date()
    in_window_dates = sorted({
        d for d in spot_full["date"].unique()
        if start_date <= d <= end_date
    })

    all_trades: List[TrafficLightTrade] = []
    for day in in_window_dates:
        spot_day = spot_full[spot_full["date"] == day].reset_index(drop=True)
        if spot_day.empty:
            continue

        options_day = options_loader(instrument, day)
        if options_day.empty:
            logger.info("No options data for %s on %s — skipping day", instrument, day)
            continue

        expiry_date = get_nearest_weekly_expiry(day) or day

        day_trades = run_machine_for_day(
            spot_day=spot_day,
            options_day=options_day,
            day=day,
            expiry_date=expiry_date,
            instrument=instrument,
            lot_size=lot_size,
            lot_multiplier=lot_multiplier,
            params=params,
            timing=timing,
            strike_rounding=strike_rounding,
        )
        all_trades.extend(day_trades)

    return all_trades


def summarize_trades(trades: List[TrafficLightTrade]) -> dict:
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "total_pnl_points": 0.0, "total_pnl_inr": 0.0,
            "by_side": {}, "by_reason": {},
        }

    wins = sum(1 for t in trades if t.pnl_points > 0)
    losses = sum(1 for t in trades if t.pnl_points <= 0)
    total_points = sum(t.pnl_points for t in trades)
    total_inr = sum(t.pnl_inr for t in trades)

    by_side: Dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl_inr": 0.0, "pnl_points": 0.0})
    by_reason: Dict[str, int] = defaultdict(int)
    for t in trades:
        by_side[t.option_type]["trades"] += 1
        by_side[t.option_type]["pnl_inr"] += t.pnl_inr
        by_side[t.option_type]["pnl_points"] += t.pnl_points
        by_reason[t.exit_reason] += 1

    return {
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades),
        "total_pnl_points": total_points,
        "total_pnl_inr": total_inr,
        "by_side": dict(by_side),
        "by_reason": dict(by_reason),
    }
