"""
SENSEX dual short-premium backtest.

Part 1: 09:45 short strangle (sell 0.25d CE=ATM+600 and 0.25d PE=ATM-600).
Part 2: post-11:45 range breakout on spot; sell the 09:45-recorded ATM PUT on a
        high break / ATM CALL on a low break.

Shared per-leg management: SL=1.25x cost, target=0.10x cost, one re-entry at cost.
Strikes are LOCKED at selection time; SL/target/re-entry monitor only that
locked contract's own OHLC. No slippage. See
docs/superpowers/specs/2026-07-01-sensex-dual-short-backtest-design.md.
"""
import logging
import statistics
from collections import defaultdict
from datetime import time as _time
from typing import Optional

import pandas as pd

from engine.data_loader import load_data
from engine.expiry_calendar import days_to_weekly_expiry, trading_days_to_weekly_expiry
from config import DATA_PATH, SPOT_DATA_PATH, LOT_SIZE, STRIKE_ROUNDING

logger = logging.getLogger(__name__)

INSTRUMENT = "SENSEX"
LOT = LOT_SIZE[INSTRUMENT]

# Delta -> strike_offset mapping (SENSEX step 100).
OFF_P1_CE = 6    # 0.25d CE = ATM + 600
OFF_P1_PE = -6   # 0.25d PE = ATM - 600
OFF_ATM = 0      # 0.5d  = ATM

# Time constants (IST wall-clock).
RANGE_START = _time(9, 45)
RANGE_END = _time(11, 45)
MONITOR_START = _time(11, 45)   # breakout monitoring is strictly AFTER 11:45
SQUARE_OFF = _time(15, 28)
ENTRY_TIME = _time(9, 45)

# Management levels (off the ORIGINAL entry cost).
SL_MULT = 1.25
TGT_MULT = 0.10


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------
def pick_row(slice_df: pd.DataFrame, option_type: str, offset: int) -> Optional[pd.Series]:
    """Return the single row matching (option_type, strike_offset), else None."""
    rows = slice_df[(slice_df["option_type"] == option_type)
                    & (slice_df["strike_offset"] == offset)]
    return rows.iloc[0] if len(rows) else None


def select_locked_strikes(slice_0945: pd.DataFrame, off_025: int = OFF_P1_CE) -> dict:
    """Pick the four legs' contracts from the 09:45 slice by strike_offset.

    off_025 = |offset| (in strikes) for the 0.25d strangle legs
    (CE = +off_025, PE = -off_025). Part-2 legs are always ATM (offset 0).
    """
    return {
        "p1_ce": pick_row(slice_0945, "CE", off_025),
        "p1_pe": pick_row(slice_0945, "PE", -off_025),
        "p2_ce": pick_row(slice_0945, "CE", OFF_ATM),
        "p2_pe": pick_row(slice_0945, "PE", OFF_ATM),
    }


# ---------------------------------------------------------------------------
# Spot range + breakout detection
# ---------------------------------------------------------------------------
def compute_range(spot_day: pd.DataFrame) -> tuple:
    """(range_high, range_low) over 09:45-11:45 inclusive; (nan, nan) if empty."""
    w = spot_day[(spot_day["time_only"] >= RANGE_START)
                 & (spot_day["time_only"] <= RANGE_END)]
    if w.empty:
        return float("nan"), float("nan")
    return float(w["high"].max()), float(w["low"].min())


def detect_breakouts(spot_day: pd.DataFrame, range_high: float, range_low: float) -> tuple:
    """First high-break (-> PUT) and first low-break (-> CALL) datetimes after 11:45."""
    mon = spot_day[(spot_day["time_only"] > MONITOR_START)
                   & (spot_day["time_only"] <= SQUARE_OFF)].sort_values("time_only")
    put_dt = None
    call_dt = None
    for _, b in mon.iterrows():
        if put_dt is None and b["high"] > range_high:
            put_dt = b["datetime"]
        if call_dt is None and b["low"] < range_low:
            call_dt = b["datetime"]
        if put_dt is not None and call_dt is not None:
            break
    return put_dt, call_dt


# ---------------------------------------------------------------------------
# Per-leg SL / target / re-entry state machine
# ---------------------------------------------------------------------------
def _trip(kind, entry_idx, entry_price, exit_idx, exit_price, reason):
    return {"entry_kind": kind, "entry_idx": entry_idx, "entry_price": float(entry_price),
            "exit_idx": exit_idx, "exit_price": float(exit_price), "exit_reason": reason,
            "pnl_points": float(entry_price) - float(exit_price)}


def _run_one(highs, lows, closes, entry_idx, scan_start, entry_price,
             sl_level, tgt_level, eod_idx):
    """Scan [scan_start, eod_idx] for SL (high>=sl) then TGT (low<=tgt); SL-first.
    Returns a round-trip dict (exit reason SL/TARGET/EOD)."""
    for i in range(scan_start, eod_idx + 1):
        if highs[i] >= sl_level:                       # SL-first tie-break
            return _trip("INITIAL", entry_idx, entry_price, i, sl_level, "SL")
        if lows[i] <= tgt_level:
            return _trip("INITIAL", entry_idx, entry_price, i, tgt_level, "TARGET")
    return _trip("INITIAL", entry_idx, entry_price, eod_idx, closes[eod_idx], "EOD")


def simulate_leg(highs, lows, closes, entry_idx, entry_cost, eod_idx,
                 sl_mult=SL_MULT, tgt_mult=TGT_MULT):
    """Short-leg SL/target/re-entry state machine.

    highs/lows/closes: the locked contract's 1-min OHLC for the day (index 0 = first bar).
    entry_idx: index of the entry bar (leg filled at entry_cost; monitoring starts entry_idx+1).
    eod_idx: last index to consider (15:28 / last-available bar).
    sl_mult/tgt_mult: SL and target levels as multiples of the original entry cost.
    Returns 1 or 2 round-trip dicts (INITIAL, optional REENTRY).
    """
    sl_level = sl_mult * entry_cost
    tgt_level = tgt_mult * entry_cost

    trips = []
    initial = _run_one(highs, lows, closes, entry_idx, entry_idx + 1,
                       entry_cost, sl_level, tgt_level, eod_idx)
    trips.append(initial)

    # Re-entry only after an SL exit, and only if there is room before EOD.
    if initial["exit_reason"] == "SL" and initial["exit_idx"] < eod_idx:
        re_idx = None
        for i in range(initial["exit_idx"] + 1, eod_idx + 1):
            if lows[i] <= entry_cost:                  # premium back down to cost
                re_idx = i
                break
        if re_idx is not None:
            re = _run_one(highs, lows, closes, re_idx, re_idx + 1,
                          entry_cost, sl_level, tgt_level, eod_idx)
            re["entry_kind"] = "REENTRY"
            trips.append(re)
    return trips


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def session_dates(options_path: str) -> list:
    """All REGULAR-hours session dates (sorted, unique) in an options parquet,
    independent of any backtest window. A weekday present here is a normal trading
    session; one absent (with data around it) is a holiday. Diwali *Muhurat*
    (evening-only) days are excluded — they have no regular-hours bars — so they
    don't count as sessions for trading-day DTE. Used for trading-day DTE counting."""
    dt = pd.to_datetime(pd.read_parquet(options_path, columns=["datetime"])["datetime"])
    # A normal session opens in the morning; require a bar at/before 10:00. This
    # excludes Diwali Muhurat days (afternoon-only, ~13:45+) which have no morning
    # bars and which the strategy can't trade anyway (no 09:45 entry bar).
    morning = dt[dt.dt.time <= _time(10, 0)]
    return sorted(set(morning.dt.date))


def load_spot(spot_path: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Load spot 1-min parquet, filter date range, add date/time_only (IST wall-clock)."""
    df = pd.read_parquet(spot_path)
    df["datetime"] = pd.to_datetime(df["datetime"])            # tz-aware +05:30
    start = pd.to_datetime(start_date).tz_localize("Asia/Kolkata")
    end = pd.to_datetime(end_date).tz_localize("Asia/Kolkata") + pd.Timedelta(days=1)
    df = df[(df["datetime"] >= start) & (df["datetime"] < end)].copy()
    df = df.sort_values("datetime").reset_index(drop=True)
    df["date"] = df["datetime"].dt.date                        # local (IST) date
    df["time_only"] = df["datetime"].dt.time                   # local (IST) wall time
    return df


# ---------------------------------------------------------------------------
# Per-day processing
# ---------------------------------------------------------------------------
def contract_day_bars(day_opts: pd.DataFrame, strike: float, option_type: str) -> pd.DataFrame:
    """That locked contract's rows for the day, sorted by datetime."""
    c = day_opts[(day_opts["strike"] == strike)
                 & (day_opts["option_type"] == option_type)]
    return c.sort_values("datetime").reset_index(drop=True)


def _bump(skips: dict, reason: str) -> None:
    skips[reason] = skips.get(reason, 0) + 1


def run_leg_from_frame(bars: pd.DataFrame, entry_dt, part, side, strike,
                       sl_mult=SL_MULT, tgt_mult=TGT_MULT, lot=LOT):
    """Expand a locked-contract frame into trade dicts. Returns (trades, skip_reason|None)."""
    if bars.empty:
        return [], "NO_CONTRACT_BARS"
    idx = bars.index[bars["datetime"] == entry_dt]
    if len(idx) == 0:
        return [], "ENTRY_BAR_MISSING"
    entry_pos = int(idx[0])

    eod_mask = bars["time_only"] <= SQUARE_OFF
    if not eod_mask.any():
        return [], "NO_EOD_BAR"
    eod_pos = int(bars.index[eod_mask][-1])
    if eod_pos < entry_pos:            # entered at/after square-off -> no room
        eod_pos = entry_pos

    highs = bars["high"].to_numpy()
    lows = bars["low"].to_numpy()
    closes = bars["close"].to_numpy()
    entry_cost = float(bars["close"].iloc[entry_pos])

    trips = simulate_leg(highs, lows, closes, entry_pos, entry_cost, eod_pos, sl_mult, tgt_mult)

    eod_is_square_off = bars["time_only"].iloc[eod_pos] == SQUARE_OFF
    trade_date = bars["date"].iloc[entry_pos]
    sl_level = sl_mult * entry_cost
    tgt_level = tgt_mult * entry_cost
    leg_id = f"{trade_date}_{part}_{side}_{strike:.0f}"
    trades = []
    for tr in trips:
        reason = tr["exit_reason"]
        if reason == "EOD" and not eod_is_square_off:
            reason = "EOD_LAST_AVAILABLE"
        trades.append({
            "date": trade_date, "part": part, "side": side, "strike": strike,
            "leg_id": leg_id,
            "entry_kind": tr["entry_kind"],
            "entry_time": bars["datetime"].iloc[tr["entry_idx"]],
            "entry_cost": tr["entry_price"],
            "sl_level": sl_level, "target_level": tgt_level,
            "exit_time": bars["datetime"].iloc[tr["exit_idx"]],
            "exit_price": tr["exit_price"], "exit_reason": reason,
            "pnl_points": tr["pnl_points"], "pnl_inr": tr["pnl_points"] * lot,
        })
    return trades, None


def process_day(day_opts: pd.DataFrame, spot_day: pd.DataFrame, trading_date, dte,
                off_025=OFF_P1_CE, sl_mult=SL_MULT, tgt_mult=TGT_MULT, lot=LOT):
    """Run both parts for one trading day. Returns (trades, skips)."""
    trades, skips = [], {}

    slice_0945 = day_opts[day_opts["time_only"] == ENTRY_TIME]
    if slice_0945.empty:
        _bump(skips, "NO_0945_BAR")
        return trades, skips

    picks = select_locked_strikes(slice_0945, off_025)

    # --- Part 1: enter now at 09:45 close ---
    entry_dt_0945 = slice_0945["datetime"].iloc[0]
    for side, key, miss in (("CE", "p1_ce", "P1_CE_UNAVAILABLE_0945"),
                            ("PE", "p1_pe", "P1_PE_UNAVAILABLE_0945")):
        row = picks[key]
        if row is None:
            _bump(skips, miss)
            continue
        bars = contract_day_bars(day_opts, float(row["strike"]), side)
        leg_trades, skip = run_leg_from_frame(bars, entry_dt_0945, "P1", side,
                                              float(row["strike"]), sl_mult, tgt_mult, lot)
        if skip:
            _bump(skips, miss)
        trades.extend(leg_trades)

    # --- Part 2: record ATM strikes, then breakout after 11:45 ---
    if picks["p2_ce"] is None or picks["p2_pe"] is None:
        _bump(skips, "P2_ATM_NOT_RECORDABLE_0945")
        return trades, skips

    range_high, range_low = compute_range(spot_day)
    if pd.isna(range_high) or pd.isna(range_low):
        _bump(skips, "NO_RANGE_WINDOW")
        return trades, skips

    put_dt, call_dt = detect_breakouts(spot_day, range_high, range_low)

    # High break -> sell recorded ATM PUT.
    if put_dt is not None:
        strike = float(picks["p2_pe"]["strike"])
        bars = contract_day_bars(day_opts, strike, "PE")
        leg_trades, skip = run_leg_from_frame(bars, put_dt, "P2", "PE", strike,
                                              sl_mult, tgt_mult, lot)
        if skip:
            _bump(skips, "P2_PUT_UNAVAILABLE_BREAKOUT")
        trades.extend(leg_trades)

    # Low break -> sell recorded ATM CALL.
    if call_dt is not None:
        strike = float(picks["p2_ce"]["strike"])
        bars = contract_day_bars(day_opts, strike, "CE")
        leg_trades, skip = run_leg_from_frame(bars, call_dt, "P2", "CE", strike,
                                              sl_mult, tgt_mult, lot)
        if skip:
            _bump(skips, "P2_CALL_UNAVAILABLE_BREAKOUT")
        trades.extend(leg_trades)

    return trades, skips


# ---------------------------------------------------------------------------
# Window-bucket classification (what happened to the locked strike)
# ---------------------------------------------------------------------------
def classify_day_buckets(day_opts: pd.DataFrame, day_trades: list, step: int) -> list:
    """Tag each trade dict with a leg-level 'bucket':

      clean     - the locked strike stayed within the stored +/-window all hold
      recovered - it drifted beyond the window at some minute (we lost its price)
                  then price reversed and it came back, exiting NORMALLY
      blind     - it was still open when the strike left for good
                  (final exit reason EOD_LAST_AVAILABLE, squared off at last-seen price)

    `step` is the instrument strike step (SENSEX 100 / NIFTY 50), so the offset is
    computed in the data's own units. Mutates and returns day_trades.
    """
    if not day_trades:
        return day_trades
    atm = day_opts.groupby("time_only")["atm_strike"].first()
    hw = day_opts.assign(_ao=day_opts["strike_offset"].abs()).groupby("time_only")["_ao"].max()
    minutes = sorted(day_opts["time_only"].unique())

    by_leg = defaultdict(list)
    for t in day_trades:
        by_leg[t["leg_id"]].append(t)

    for rows in by_leg.values():
        rows_sorted = sorted(rows, key=lambda r: r["exit_time"])
        strike = float(rows_sorted[0]["strike"])
        entry_t = min(r["entry_time"] for r in rows).time()
        exit_t = max(r["exit_time"] for r in rows).time()
        blind = rows_sorted[-1]["exit_reason"] == "EOD_LAST_AVAILABLE"
        wandered = False
        for m in minutes:
            if entry_t <= m <= exit_t and m in atm.index:
                if abs(round((strike - float(atm.loc[m])) / step)) > int(hw.loc[m]):
                    wandered = True
                    break
        bucket = "blind" if blind else ("recovered" if wandered else "clean")
        for r in rows:
            r["bucket"] = bucket
    return day_trades


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class SensexDualShortBacktest:
    def __init__(self, start_date, end_date, instrument="SENSEX",
                 options_path=None, spot_path=None,
                 sl_mult=SL_MULT, tgt_mult=TGT_MULT, off_025=OFF_P1_CE,
                 dte_max=3, lots=1, dte_mode="trading"):
        self.instrument = instrument.upper()
        self.start_date = start_date
        self.end_date = end_date
        self.options_path = options_path or DATA_PATH[self.instrument]
        self.spot_path = spot_path or SPOT_DATA_PATH[self.instrument]
        self.sl_mult = sl_mult
        self.tgt_mult = tgt_mult
        self.off_025 = off_025          # |offset| for 0.25d legs (6 => SENSEX ±600 / NIFTY ±300)
        self.dte_max = dte_max
        self.dte_mode = dte_mode        # "trading" (sessions) or "calendar" (raw days)
        self.lots = lots
        self.lot = LOT_SIZE[self.instrument] * lots   # lot_size * number of lots
        self.trades = []
        self.skips = {}
        self.non_trading_days = 0

    def run(self):
        opts = load_data(self.options_path, self.start_date, self.end_date, "weekly")
        spot = load_spot(self.spot_path, self.start_date, self.end_date)

        opts_by_day = dict(tuple(opts.groupby("date")))
        spot_by_day = dict(tuple(spot.groupby("date")))
        # Full session calendar from the whole parquet (not the windowed load),
        # so trading-day DTE can see sessions all the way to each expiry.
        sessions = session_dates(self.options_path) if self.dte_mode == "trading" else None

        for d in sorted(set(opts_by_day) & set(spot_by_day)):
            if self.dte_mode == "trading":
                dte = trading_days_to_weekly_expiry(self.instrument, d, sessions)
            else:
                dte = days_to_weekly_expiry(self.instrument, d)
            if dte is None or dte < 0 or dte > self.dte_max:
                self.non_trading_days += 1
                continue
            day_trades, day_skips = process_day(
                opts_by_day[d], spot_by_day[d], d, dte,
                off_025=self.off_025, sl_mult=self.sl_mult,
                tgt_mult=self.tgt_mult, lot=self.lot)
            classify_day_buckets(opts_by_day[d], day_trades, STRIKE_ROUNDING[self.instrument])
            for t in day_trades:
                t["dte"] = dte
            self.trades.extend(day_trades)
            for reason, n in day_skips.items():
                self.skips[reason] = self.skips.get(reason, 0) + n

        logger.info("Backtest complete: %d trades, %d non-trading days, skips=%s",
                    len(self.trades), self.non_trading_days, self.skips)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def summarize(trades, skips, non_trading_days):
    total = sum(t["pnl_inr"] for t in trades)
    p1 = sum(t["pnl_inr"] for t in trades if t["part"] == "P1")
    p2 = sum(t["pnl_inr"] for t in trades if t["part"] == "P2")

    leg_net = {}
    for t in trades:
        leg_net[t["leg_id"]] = leg_net.get(t["leg_id"], 0.0) + t["pnl_inr"]
    nets = list(leg_net.values())
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]

    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    # Data-coverage split: "blind" round-trips are those squared off at the last
    # available price because the locked strike left the +/-10 data window before
    # 15:28 (exit_reason EOD_LAST_AVAILABLE). Their P&L is an approximation, so
    # separate it from the fully-observed P&L.
    blind = [t for t in trades if t["exit_reason"] == "EOD_LAST_AVAILABLE"]
    blind_pnl = sum(t["pnl_inr"] for t in blind)
    blind_legs = len({t["leg_id"] for t in blind})

    return {
        "total_pnl_inr": total,
        "p1_pnl_inr": p1,
        "p2_pnl_inr": p2,
        "n_legs": len(nets),
        "n_round_trips": len(trades),
        "n_reentries": sum(1 for t in trades if t["entry_kind"] == "REENTRY"),
        "win_rate": (len(wins) / len(nets)) if nets else 0.0,
        "avg_win_inr": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss_inr": (sum(losses) / len(losses)) if losses else 0.0,
        "exit_reason_counts": reasons,
        "skips": dict(skips),
        "non_trading_days": non_trading_days,
        "observed_pnl_inr": total - blind_pnl,
        "blind_pnl_inr": blind_pnl,
        "blind_pnl_pct": (blind_pnl / total) if total else 0.0,
        "n_blind_round_trips": len(blind),
        "n_blind_legs": blind_legs,
    }


def bucket_summary(trades: list) -> dict:
    """Per-bucket (clean / recovered / blind) leg-level stats: win rate, P&L
    distribution, and holding time. Requires trades tagged with 'bucket'
    (see classify_day_buckets). Returns {bucket: {stats...}}."""
    legs = {}
    for t in trades:
        lid = t["leg_id"]
        e = legs.get(lid)
        if e is None:
            legs[lid] = {"bucket": t.get("bucket", "clean"), "pnl": t["pnl_inr"],
                         "entry": t["entry_time"], "exit": t["exit_time"]}
        else:
            e["pnl"] += t["pnl_inr"]
            if t["entry_time"] < e["entry"]:
                e["entry"] = t["entry_time"]
            if t["exit_time"] > e["exit"]:
                e["exit"] = t["exit_time"]

    groups = defaultdict(list)
    for lg in legs.values():
        groups[lg["bucket"]].append(lg)

    out = {}
    for b, ls in groups.items():
        nets = [x["pnl"] for x in ls]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n <= 0]
        holds = [(x["exit"] - x["entry"]).total_seconds() / 60.0 for x in ls]
        out[b] = {
            "n_legs": len(ls),
            "win_rate": (len(wins) / len(ls)) if ls else 0.0,
            "total_pnl_inr": sum(nets),
            "mean_pnl_inr": statistics.mean(nets) if nets else 0.0,
            "median_pnl_inr": statistics.median(nets) if nets else 0.0,
            "avg_win_inr": statistics.mean(wins) if wins else 0.0,
            "avg_loss_inr": statistics.mean(losses) if losses else 0.0,
            "hold_median_min": statistics.median(holds) if holds else 0.0,
            "hold_mean_min": statistics.mean(holds) if holds else 0.0,
        }
    return out
