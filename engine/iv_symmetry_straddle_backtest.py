"""
IV Symmetry Naked Short Straddle Backtest Engine.

Strategy (from Sanket's IV_skew_straddle_v5 notebook, NS variant):
  Every minute, measure how symmetric the IV smile is around ATM.
  For each distance n (1..10 strikes), pair symmetry = min(iv_a, iv_b) / max(iv_a, iv_b)
  comparing the +n vs -n strike of the same option type. A pair is valid
  only when both IVs exist and are > 0.

  Signal (all required, evaluated on bar close):
    - mean CE pair symmetry >= sym_min (default 0.80)
    - mean PE pair symmetry >= sym_min
    - at least min_pairs (default 2) valid pairs on each side
    - bar time within entry window (09:45 - 15:10) and flat

  On signal at bar T close -> SELL the ATM straddle at bar T+1 OPEN.
  (Strict no-lookahead: every fill happens at a price printed after the
  decision bar. The notebook filled at the signal bar's own close.)

  Exits (monitored on the ENTRY strike's straddle close, never the
  rolling ATM -- the contract actually sold is what's tracked):
    - SL:    loss >= sl_pct (default 8%) of entry premium  -> exit T+1 open
    - TP:    gain >= tp_pct (default 30%) of entry premium -> exit T+1 open
    - FORCE: bar time >= 15:10 -> exit at that bar's open
    - EOD:   last bar of day safety net -> exit at last close

  Re-entry allowed after exit (max 1 open position). Gross P&L, no costs.
"""

import argparse
import logging
import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from config import DATA_PATH, LOT_SIZE, get_nearest_weekly_expiry

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

# Signal uses +/-10 offsets; prices are loaded wider so the fixed entry
# strike can still be tracked after spot drifts away from it.
SIGNAL_MAX_OFFSET = 10
PRICE_MAX_OFFSET = 20

# expiry_type/expiry_code are filter-only (predicate pushdown works on
# columns that aren't selected), keeping the loaded frame lean.
LOAD_COLUMNS = [
    "datetime", "option_type",
    "strike", "atm_strike", "strike_offset", "spot", "open", "close", "iv",
]


# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------

@dataclass
class IVSymmetryTrade:
    date: str                  # "YYYY-MM-DD"
    strike: float              # fixed at entry; exits track this contract
    expiry_date: str

    signal_time: str           # bar whose close fired the signal
    entry_time: str            # next bar; filled at its open
    entry_price: float         # straddle open (CE open + PE open)
    ce_entry_price: float
    pe_entry_price: float
    qty: int

    sl_level: float            # entry * (1 + sl_pct/100)
    tp_level: float            # entry * (1 - tp_pct/100)

    ce_sym: float              # symmetry scores at signal bar
    pe_sym: float
    ce_pairs: int
    pe_pairs: int
    spot_at_entry: float

    exit_time: str
    exit_price: float
    exit_reason: str           # "SL" / "TP" / "FORCE" / "EOD"

    pnl_points: float          # entry - exit (short straddle)
    pnl_pct: float             # of entry premium
    pnl_inr: float             # pnl_points * lot size
    hold_minutes: int
    is_expiry_day: bool
    day_name: str


def trades_to_dataframe(trades: List[IVSymmetryTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


# ---------------------------------------------------------------------------
# Signal computation (vectorized, uses only same-bar data)
# ---------------------------------------------------------------------------

def compute_signal_frame(
    df: pd.DataFrame,
    sym_min: float = 0.80,
    min_pairs: int = 2,
    max_offset: int = SIGNAL_MAX_OFFSET,
) -> pd.DataFrame:
    """Compute IV symmetry per minute from long-format options data.

    Returns DataFrame indexed by datetime with columns:
      ce_sym, pe_sym, ce_pairs, pe_pairs, signal, atm_strike, spot, time_str

    `signal` covers the symmetry conditions only; the engine applies the
    time window and position state.
    """
    piv = df.pivot_table(
        index="datetime", columns=["option_type", "strike_offset"],
        values="iv", aggfunc="first",
    )
    # iv <= 0 means the solver had no solution (deep ITM) -> not usable
    piv = piv.where(piv > 0)

    out = pd.DataFrame(index=piv.index)

    for side in ("CE", "PE"):
        ratios = []
        for n in range(1, max_offset + 1):
            a = piv[(side, n)] if (side, n) in piv.columns else None
            b = piv[(side, -n)] if (side, -n) in piv.columns else None
            if a is None or b is None:
                continue
            # NaN if either side missing -> pair invalid
            ratios.append(np.minimum(a, b) / np.maximum(a, b))
        key = side.lower()
        if ratios:
            rdf = pd.concat(ratios, axis=1)
            out[f"{key}_sym"] = rdf.mean(axis=1, skipna=True)
            out[f"{key}_pairs"] = rdf.notna().sum(axis=1)
        else:
            out[f"{key}_sym"] = np.nan
            out[f"{key}_pairs"] = 0

    out["signal"] = (
        (out["ce_sym"] >= sym_min)
        & (out["pe_sym"] >= sym_min)
        & (out["ce_pairs"] >= min_pairs)
        & (out["pe_pairs"] >= min_pairs)
    ).fillna(False)

    meta = df.groupby("datetime")[["atm_strike", "spot"]].first()
    out = out.join(meta)
    out["time_str"] = out.index.map(lambda dt: dt.strftime("%H:%M"))
    return out.sort_index()


def build_strike_prices(df: pd.DataFrame) -> Dict[float, pd.DataFrame]:
    """Per-strike straddle prices: { strike: DataFrame indexed by datetime
    with straddle_open, straddle_close, ce_open, pe_open, ce_close, pe_close }.

    Only strikes with both CE and PE bars at common timestamps are kept.
    """
    result: Dict[float, pd.DataFrame] = {}
    for strike, grp in df.groupby("strike"):
        ce = grp[grp["option_type"] == "CE"].set_index("datetime").sort_index()
        pe = grp[grp["option_type"] == "PE"].set_index("datetime").sort_index()
        if ce.empty or pe.empty:
            continue
        common = ce.index.intersection(pe.index)
        if common.empty:
            continue
        ce = ce.loc[common]
        pe = pe.loc[common]
        sdf = pd.DataFrame(index=common)
        sdf["ce_open"] = ce["open"]
        sdf["pe_open"] = pe["open"]
        sdf["ce_close"] = ce["close"]
        sdf["pe_close"] = pe["close"]
        sdf["straddle_open"] = ce["open"] + pe["open"]
        sdf["straddle_close"] = ce["close"] + pe["close"]
        result[float(strike)] = sdf
    return result


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class IVSymmetryStraddleEngine:
    """Naked short straddle on IV symmetry signal, lookahead-safe fills."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        sl_pct: float = 8.0,
        tp_pct: float = 30.0,
        sym_min: float = 0.80,
        min_pairs: int = 2,
        entry_start: str = "09:45",
        force_exit_time: str = "15:10",
    ):
        self.start_date = start_date
        self.end_date = end_date
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct
        self.sym_min = sym_min
        self.min_pairs = min_pairs
        self.entry_start = entry_start
        self.force_exit_time = force_exit_time
        self.instrument = "NIFTY"
        self.lot_size = LOT_SIZE.get("NIFTY", 65)

        self._options_1m: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_window(self, start: str, end_excl: str) -> pd.DataFrame:
        """Load weekly nearest-expiry options within +/-PRICE_MAX_OFFSET strikes.

        Uses pyarrow predicate pushdown so the 100M+ row parquet is never
        fully materialized. The datetime column is an ISO string, so string
        comparison gives correct date filtering.
        """
        path = os.path.join(BASE_DIR, DATA_PATH[self.instrument])
        logger.info(f"Loading {path} [{start} .. {end_excl})...")
        df = pd.read_parquet(
            path,
            columns=LOAD_COLUMNS,
            filters=[
                ("expiry_type", "==", "WEEK"),
                ("expiry_code", "==", 1),
                ("strike_offset", ">=", -PRICE_MAX_OFFSET),
                ("strike_offset", "<=", PRICE_MAX_OFFSET),
                ("datetime", ">=", start),
                ("datetime", "<", end_excl),
            ],
        )
        df["datetime"] = pd.to_datetime(df["datetime"], format="ISO8601")
        df["date"] = df["datetime"].dt.date
        logger.info(f"Options 1m: {len(df):,} rows, {df['date'].nunique()} days")
        return df

    def _year_chunks(self):
        """Split [start_date, end_date] into calendar-year windows.

        Keeps peak memory bounded on multi-year runs; results are identical
        because no engine state crosses a day boundary.
        """
        start = pd.Timestamp(self.start_date)
        end = pd.Timestamp(self.end_date)
        chunks = []
        while start <= end:
            year_end = min(pd.Timestamp(f"{start.year}-12-31"), end)
            chunks.append((
                start.strftime("%Y-%m-%d"),
                (year_end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            ))
            start = year_end + pd.Timedelta(days=1)
        return chunks

    # ------------------------------------------------------------------
    # Core backtest loop
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[IVSymmetryTrade]:
        trades: List[IVSymmetryTrade] = []
        chunks = self._year_chunks()
        day_idx = 0
        # rough day total for progress reporting (~245 trading days/year)
        est_total = max(
            1, int((pd.Timestamp(self.end_date) - pd.Timestamp(self.start_date)).days / 365.25 * 245) + 1
        )
        for chunk_start, chunk_end_excl in chunks:
            df = self._load_window(chunk_start, chunk_end_excl)
            for trading_date, day_df in df.groupby("date", sort=True):
                if progress_callback:
                    progress_callback(day_idx, est_total, str(trading_date))
                trades.extend(self.run_day(day_df, trading_date))
                day_idx += 1
            del df
        logger.info(f"Backtest complete: {len(trades)} trades over {day_idx} days")
        return trades

    def run_day(self, day_df: pd.DataFrame, trading_date=None) -> List[IVSymmetryTrade]:
        """Run one trading day on long-format data. Testable in isolation."""
        if day_df.empty:
            return []
        if trading_date is None:
            trading_date = day_df["datetime"].iloc[0].date()

        expiry_date = self._safe_expiry(trading_date)
        is_expiry_day = expiry_date is not None and str(expiry_date) == str(trading_date)
        day_name = pd.Timestamp(trading_date).strftime("%a")

        sf = compute_signal_frame(
            day_df, self.sym_min, self.min_pairs, SIGNAL_MAX_OFFSET
        )
        strike_prices = build_strike_prices(day_df)
        if sf.empty or not strike_prices:
            return []

        trades: List[IVSymmetryTrade] = []

        # Position state
        position: Optional[dict] = None   # dict with entry details
        pending_entry: Optional[dict] = None
        pending_exit: Optional[str] = None  # exit reason awaiting next-bar fill

        for t_dt, row in sf.iterrows():
            t_str = row["time_str"]

            # ---- 1. Fill pending exit at this bar's open ----
            if position is not None and pending_exit is not None:
                fill = self._straddle_open_at(strike_prices, position["strike"], t_dt)
                if fill is None:
                    fill = position["last_close"]  # strike stopped printing
                trades.append(self._close(position, t_str, fill, pending_exit,
                                          trading_date, expiry_date, is_expiry_day, day_name))
                position = None
                pending_exit = None

            # ---- 2. Force exit at this bar's open ----
            if position is not None and t_str >= self.force_exit_time:
                fill = self._straddle_open_at(strike_prices, position["strike"], t_dt)
                if fill is None:
                    fill = position["last_close"]
                trades.append(self._close(position, t_str, fill, "FORCE",
                                          trading_date, expiry_date, is_expiry_day, day_name))
                position = None

            # ---- 3. Fill pending entry at this bar's open ----
            if pending_entry is not None and position is None:
                if t_str >= self.force_exit_time:
                    pending_entry = None  # too late to enter
                else:
                    strike = pending_entry["strike"]
                    sdf = strike_prices.get(strike)
                    if sdf is not None and t_dt in sdf.index:
                        bar = sdf.loc[t_dt]
                        entry_price = round(bar["straddle_open"], 2)
                        position = {
                            **pending_entry,
                            "entry_time": t_str,
                            "entry_dt": t_dt,
                            "entry_price": entry_price,
                            "ce_entry": bar["ce_open"],
                            "pe_entry": bar["pe_open"],
                            "sl_level": round(entry_price * (1 + self.sl_pct / 100), 2),
                            "tp_level": round(entry_price * (1 - self.tp_pct / 100), 2),
                            "last_close": entry_price,
                        }
                        logger.debug(
                            f"{trading_date} {t_str} SELL STRADDLE strike={strike} "
                            f"@{entry_price} SL={position['sl_level']} TP={position['tp_level']}"
                        )
                    pending_entry = None  # strike not printing -> cancel

            # ---- 4. Exit condition check on this bar's close (fixed strike) ----
            if position is not None and pending_exit is None:
                sdf = strike_prices.get(position["strike"])
                cur = None
                if sdf is not None and t_dt in sdf.index:
                    cur = sdf.loc[t_dt, "straddle_close"]
                    position["last_close"] = cur
                else:
                    cur = position["last_close"]  # carry last known price
                if cur is not None:
                    move_pct = (cur - position["entry_price"]) / position["entry_price"] * 100
                    if move_pct >= self.sl_pct:
                        pending_exit = "SL"
                    elif -move_pct >= self.tp_pct:
                        pending_exit = "TP"

            # ---- 5. Entry signal on this bar's close ----
            if (position is None
                    and pending_entry is None
                    and bool(row["signal"])
                    and self.entry_start <= t_str < self.force_exit_time):
                pending_entry = {
                    "strike": float(row["atm_strike"]),
                    "signal_time": t_str,
                    "ce_sym": round(float(row["ce_sym"]), 4),
                    "pe_sym": round(float(row["pe_sym"]), 4),
                    "ce_pairs": int(row["ce_pairs"]),
                    "pe_pairs": int(row["pe_pairs"]),
                    "spot": float(row["spot"]),
                }

        # ---- EOD safety net: still open after last bar -> exit at last close ----
        if position is not None:
            last_t = sf.iloc[-1]["time_str"]
            trades.append(self._close(position, last_t, position["last_close"], "EOD",
                                      trading_date, expiry_date, is_expiry_day, day_name))

        return trades

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _safe_expiry(self, trading_date):
        try:
            return get_nearest_weekly_expiry(trading_date)
        except Exception:
            return None

    @staticmethod
    def _straddle_open_at(strike_prices, strike, t_dt) -> Optional[float]:
        sdf = strike_prices.get(strike)
        if sdf is not None and t_dt in sdf.index:
            return round(sdf.loc[t_dt, "straddle_open"], 2)
        return None

    def _close(self, position, exit_time, exit_price, reason,
               trading_date, expiry_date, is_expiry_day, day_name) -> IVSymmetryTrade:
        exit_price = round(float(exit_price), 2)
        entry_price = position["entry_price"]
        pnl_points = round(entry_price - exit_price, 2)
        h, m = position["entry_time"].split(":")
        eh, em = exit_time.split(":")
        hold = (int(eh) * 60 + int(em)) - (int(h) * 60 + int(m))
        return IVSymmetryTrade(
            date=str(trading_date),
            strike=position["strike"],
            expiry_date=str(expiry_date) if expiry_date else "",
            signal_time=position["signal_time"],
            entry_time=position["entry_time"],
            entry_price=entry_price,
            ce_entry_price=round(float(position["ce_entry"]), 2),
            pe_entry_price=round(float(position["pe_entry"]), 2),
            qty=self.lot_size,
            sl_level=position["sl_level"],
            tp_level=position["tp_level"],
            ce_sym=position["ce_sym"],
            pe_sym=position["pe_sym"],
            ce_pairs=position["ce_pairs"],
            pe_pairs=position["pe_pairs"],
            spot_at_entry=position["spot"],
            exit_time=exit_time,
            exit_price=exit_price,
            exit_reason=reason,
            pnl_points=pnl_points,
            pnl_pct=round(pnl_points / entry_price * 100, 3) if entry_price else 0.0,
            pnl_inr=round(pnl_points * self.lot_size, 2),
            hold_minutes=hold,
            is_expiry_day=is_expiry_day,
            day_name=day_name,
        )


# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------

def summarize(trades_df: pd.DataFrame) -> str:
    """Plain-text summary report comparable to the notebook's metrics()."""
    if trades_df.empty:
        return "No trades."
    n = len(trades_df)
    wins = (trades_df["pnl_inr"] > 0).sum()
    cum = trades_df["pnl_inr"].cumsum()
    dd = (cum - cum.cummax()).min()
    losses = (trades_df["pnl_inr"] <= 0).astype(int)
    max_consec_loss = (
        losses.groupby((losses != losses.shift()).cumsum()).cumsum().max()
        if (losses > 0).any() else 0
    )
    reasons = trades_df["exit_reason"].value_counts()

    lines = [
        f"Trades:              {n}",
        f"Win rate:            {wins / n * 100:.1f}%",
        f"Total P&L:           Rs {trades_df['pnl_inr'].sum():,.0f}",
        f"Avg P&L/trade:       Rs {trades_df['pnl_inr'].mean():,.0f}",
        f"Avg win:             Rs {trades_df.loc[trades_df['pnl_inr'] > 0, 'pnl_inr'].mean():,.0f}",
        f"Avg loss:            Rs {trades_df.loc[trades_df['pnl_inr'] <= 0, 'pnl_inr'].mean():,.0f}",
        f"Best / Worst:        Rs {trades_df['pnl_inr'].max():,.0f} / Rs {trades_df['pnl_inr'].min():,.0f}",
        f"Max drawdown:        Rs {dd:,.0f}",
        f"Max consec losses:   {max_consec_loss}",
        f"Avg hold:            {trades_df['hold_minutes'].mean():.0f} min",
        "Exit reasons:        "
        + ", ".join(f"{r}: {c} ({c / n * 100:.1f}%)" for r, c in reasons.items()),
    ]

    for flag, label in [(True, "Expiry days"), (False, "Non-expiry days")]:
        sub = trades_df[trades_df["is_expiry_day"] == flag]
        if not sub.empty:
            wr = (sub["pnl_inr"] > 0).mean() * 100
            lines.append(
                f"{label + ':':<21}{len(sub)} trades, win {wr:.1f}%, "
                f"avg Rs {sub['pnl_inr'].mean():,.0f}, total Rs {sub['pnl_inr'].sum():,.0f}"
            )

    lines.append("\nDay-of-week:")
    for day, sub in trades_df.groupby("day_name"):
        wr = (sub["pnl_inr"] > 0).mean() * 100
        lines.append(
            f"  {day}: {len(sub):>5} trades, win {wr:5.1f}%, "
            f"avg Rs {sub['pnl_inr'].mean():>8,.0f}, total Rs {sub['pnl_inr'].sum():>12,.0f}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="IV symmetry naked short straddle backtest")
    parser.add_argument("--start", default="2020-08-03")
    parser.add_argument("--end", default="2026-05-22")
    parser.add_argument("--sl", type=float, default=8.0, help="SL %% of entry premium")
    parser.add_argument("--tp", type=float, default=30.0, help="TP %% of entry premium")
    parser.add_argument("--sym-min", type=float, default=0.80)
    parser.add_argument("--min-pairs", type=int, default=2)
    parser.add_argument("--entry-start", default="09:45")
    parser.add_argument("--force-exit", default="15:10")
    parser.add_argument("--out", default=None, help="trades CSV path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    engine = IVSymmetryStraddleEngine(
        start_date=args.start, end_date=args.end,
        sl_pct=args.sl, tp_pct=args.tp,
        sym_min=args.sym_min, min_pairs=args.min_pairs,
        entry_start=args.entry_start, force_exit_time=args.force_exit,
    )

    def progress(i, total, d):
        if i % 50 == 0:
            print(f"  {i}/{total} {d}")

    trades = engine.run(progress_callback=progress)
    df = trades_to_dataframe(trades)

    out = args.out or os.path.join(
        BASE_DIR, f"iv_symmetry_straddle_{args.start}_{args.end}.csv"
    )
    df.to_csv(out, index=False)
    print(f"\nTrades written to {out}\n")
    print(summarize(df))


if __name__ == "__main__":
    main()
