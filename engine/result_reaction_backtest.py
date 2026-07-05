"""
Post-Result Reaction Averaging Backtest Engine (cash equity, long only).

Strategy (per stock, one quarterly result date):
  Reference:
    - result_session = trading session of the result date (if the result date is a
      non-trading day, the previous trading day).
    - marked_high   = high of result_session.
    - observation   = every trading day STRICTLY AFTER the result date.

  Entry-1 (50%): first 1-min bar (from the day after results, no time cap) where price
    dips below marked_high. Fill = min(bar.open, marked_high). In practice this is the
    next-day open; in a gap-up it can be many days later when price first trades back
    below the result-day high.

  Entry-2 (50%): level = entry1 * (1 + second_entry_pct/100), a SIGNED offset off entry-1.
    Positive (default +10%) => above entry-1: pyramid up, fills on a rise
    (high >= level, fill = max(open, level)). Negative => below: average down, fills on a
    drop (low <= level, fill = min(open, level)). The order stays live until the position
    closes.

  Average & exits:
    - avg = entry1 (one leg) or the quantity-weighted average
      (qty1*entry1 + qty2*entry2)/(qty1 + qty2) (both legs).
    - Fixed SL = avg * (1 - sl_pct/100)   [sl_pct=None => no fixed stop].
    - Trailing SL = peak * (1 - trail_pct/100), peak = highest price since entry-1
      [trail_pct=None => no trailing]. Exit reason 'TSL'. The effective stop on a bar is
      the tighter (higher) of the fixed and trailing levels; peak uses prior-bar highs.
    - TP  = avg * (1 + tp_pct/100) before the 2nd entry; after the 2nd entry it uses
      tp_pct_after_second if set (0 => exit at the average / breakeven).
    - Stops/TP re-anchor the instant entry-2 fills; exits are deferred to the following bar.
    - second_entry_pct=None => single entry (no 2nd leg).
    - Per bar: process entry-2 first, then SL/TP on the (possibly updated) average.
      If a single bar would hit both SL and TP, SL is taken first (conservative).
    - Hold across days. Optional max_hold_days cap (calendar days from the result date):
      if no SL/TP by result_date + max_hold_days, force-exit at the last bar on/before the
      cutoff (exit_reason TIME). With no cap (or data ends first), a still-open position
      exits OPEN, marked-to-market at the last close.

  Sizing: fixed rupee notional per stock, split 50/50. qty = floor(leg_capital / price).
"""

import os
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd

STOCKS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'stocks')


@dataclass
class ResultReactionTrade:
    symbol: str
    result_date: str
    result_session: str        # session whose high is the marked high
    marked_high: float
    observation_start: str     # first trading day after the result date
    entry1_time: str
    entry1_price: float
    qty1: int
    entry2_time: str           # '' if entry-2 never filled
    entry2_price: float        # 0.0 if entry-2 never filled
    qty2: int
    avg_price: float
    sl_price: float
    tp_price: float
    exit_time: str
    exit_price: float
    exit_reason: str           # 'SL' | 'TSL' | 'TP' | 'TIME' | 'OPEN' | 'NO_ENTRY' | 'NO_DATA'
    pnl: float                 # in rupees
    pnl_pct: float             # pnl / rupees invested
    days_held: int


class ResultReactionEngine:
    """Backtests the post-result reaction averaging strategy across stocks."""

    def __init__(
        self,
        result_dates: dict,             # {symbol: 'YYYY-MM-DD'}
        capital_per_stock: float = 100000.0,
        second_entry_pct: Optional[float] = 10.0,  # SIGNED % offset of entry-2 from entry-1:
                                          #   +10 => entry1*1.10 (pyramid up, fills on a rise)
                                          #   -20 => entry1*0.80 (average down, fills on a drop)
                                          #   None => single entry (no 2nd leg)
        sl_pct: Optional[float] = 10.0,   # fixed stop; None => no fixed stop
        trail_pct: Optional[float] = None,  # % trailing stop from the post-entry peak;
                                            # None => no trailing stop (exit reason 'TSL')
        tp_pct: float = 15.0,             # TP before the 2nd entry fills
        tp_pct_after_second: Optional[float] = None,  # TP after 2nd entry; None => keep tp_pct.
                                                      # 0 => exit at the average (breakeven)
        max_hold_days: Optional[int] = None,   # calendar days from the result date; None = no cap
    ):
        self.result_dates = result_dates
        self.capital_per_stock = capital_per_stock
        self.second_entry_pct = (second_entry_pct / 100.0) if second_entry_pct is not None else None
        self.sl_pct = (sl_pct / 100.0) if sl_pct is not None else None
        self.trail_pct = (trail_pct / 100.0) if trail_pct is not None else None
        self.tp_pct = tp_pct / 100.0
        self.tp_pct_after_second = (tp_pct_after_second / 100.0) if tp_pct_after_second is not None else None
        self.max_hold_days = max_hold_days

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> list:
        """Run the backtest across all configured (symbol, result_date) pairs."""
        symbols = list(self.result_dates.keys())
        trades = []
        for i, symbol in enumerate(symbols):
            if progress_callback:
                progress_callback(i, len(symbols), symbol)
            t = self._run_stock(symbol, self.result_dates[symbol])
            if t:
                trades.append(t)
        return trades

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_stock(self, symbol: str) -> Optional[pd.DataFrame]:
        path = os.path.join(STOCKS_DIR, symbol, f'{symbol}_1m.parquet')
        if not os.path.exists(path):
            return None

        df = pd.read_parquet(path)
        # Keep IST wall-clock (a tz-aware -> .values conversion would shift to UTC).
        dt = pd.to_datetime(df['datetime'])
        if dt.dt.tz is None:
            dt = dt.dt.tz_localize('Asia/Kolkata')
        else:
            dt = dt.dt.tz_convert('Asia/Kolkata')

        df = df.copy()
        df['date'] = dt.dt.date
        df['dt_str'] = dt.dt.strftime('%Y-%m-%d %H:%M')
        sort_col = 'ts' if 'ts' in df.columns else 'datetime'
        df = df.sort_values(sort_col).reset_index(drop=True)
        return df

    def _run_stock(self, symbol: str, result_date: str) -> Optional[ResultReactionTrade]:
        df = self._load_stock(symbol)
        if df is None or df.empty:
            return self._stub(symbol, result_date, 'NO_DATA')

        R = pd.Timestamp(result_date).date()
        trading_days = sorted(df['date'].unique())

        # result_session = latest trading day <= R (handles non-trading result dates)
        sessions_on_or_before = [d for d in trading_days if d <= R]
        if not sessions_on_or_before:
            return self._stub(symbol, result_date, 'NO_DATA')
        result_session = sessions_on_or_before[-1]
        marked_high = float(df.loc[df['date'] == result_session, 'high'].max())

        # observation = bars strictly after the result date, optionally capped at
        # result_date + max_hold_days (calendar days). `capped` => data exists beyond the
        # cutoff, so a no-SL/TP exit at the end of `obs` is a TIME exit, not an OPEN one.
        obs_full = df[df['date'] > R].reset_index(drop=True)
        if self.max_hold_days is not None:
            cutoff = (pd.Timestamp(R) + pd.Timedelta(days=self.max_hold_days)).date()
            capped = bool((obs_full['date'] > cutoff).any())
            obs = obs_full[obs_full['date'] <= cutoff].reset_index(drop=True)
        else:
            obs = obs_full
            capped = False
        if obs.empty:
            return self._stub(symbol, result_date, 'NO_DATA',
                              result_session=str(result_session), marked_high=marked_high)
        observation_start = str(obs['date'].iloc[0])

        # ---- Entry-1: first bar where price dips below the marked high ----
        e1_idx = None
        for i, row in enumerate(obs.itertuples(index=False)):
            if row.low < marked_high:
                e1_idx = i
                entry1_price = min(float(row.open), marked_high)
                entry1_time = row.dt_str
                break
        if e1_idx is None:
            return self._stub(symbol, result_date, 'NO_ENTRY',
                              result_session=str(result_session), marked_high=marked_high,
                              observation_start=observation_start)

        leg_capital = self.capital_per_stock / 2.0
        qty1 = int(leg_capital // entry1_price)

        # ---- Walk bars after entry-1: (optional) entry-2, then stops / TP ----
        # entry-2 is an optional SIGNED offset from entry-1: above (pyramid up) fills on a
        # rise (high >= level); below (average down) fills on a drop (low <= level).
        do_e2 = self.second_entry_pct is not None
        level2 = entry1_price * (1 + self.second_entry_pct) if do_e2 else None
        e2_up = do_e2 and self.second_entry_pct > 0
        e2_filled = False
        entry2_price = 0.0
        entry2_time = ''
        qty2 = 0

        avg = entry1_price
        sl = avg * (1 - self.sl_pct) if self.sl_pct is not None else None
        tp = avg * (1 + self.tp_pct)
        peak = entry1_price   # highest price seen since entry-1 (drives the trailing stop)

        exit_price = None
        exit_time = None
        exit_reason = None

        post = obs.iloc[e1_idx + 1:]
        for row in post.itertuples(index=False):
            o, h, l = float(row.open), float(row.high), float(row.low)

            # 1) entry-2 fill (only once), then re-anchor avg/SL/TP. Exits are deferred to
            #    the NEXT bar (continue) so we never resolve the re-anchored TP/SL on the
            #    same minute the 2nd leg fills (intrabar ordering is unknown).
            if do_e2 and not e2_filled and ((e2_up and h >= level2) or (not e2_up and l <= level2)):
                entry2_price = max(o, level2) if e2_up else min(o, level2)  # gap -> fill at open
                entry2_time = row.dt_str
                qty2 = int(leg_capital // entry2_price)
                e2_filled = True
                # quantity-weighted (cost-basis) average across the two legs
                avg = (qty1 * entry1_price + qty2 * entry2_price) / (qty1 + qty2)
                sl = avg * (1 - self.sl_pct) if self.sl_pct is not None else None
                # after the 2nd entry, TP may switch (e.g. 0% => exit at the average/breakeven)
                tp_eff = self.tp_pct_after_second if self.tp_pct_after_second is not None else self.tp_pct
                tp = avg * (1 + tp_eff)
                peak = max(peak, h)
                continue

            # 2) exits — stops use the peak through the PREVIOUS bar (no intrabar look-ahead).
            #    Effective stop = the tighter (higher) of fixed SL and the trailing level.
            stop_level = None
            stop_reason = None
            if sl is not None:
                stop_level, stop_reason = sl, 'SL'
            if self.trail_pct is not None:
                trail_level = peak * (1 - self.trail_pct)
                if stop_level is None or trail_level > stop_level:
                    stop_level, stop_reason = trail_level, 'TSL'

            if stop_level is not None and l <= stop_level:
                exit_price = min(o, stop_level)   # gap-down -> fill at open
                exit_time = row.dt_str
                exit_reason = stop_reason
                break
            if h >= tp:
                exit_price = max(o, tp)   # gap-up -> fill at open
                exit_time = row.dt_str
                exit_reason = 'TP'
                break

            peak = max(peak, h)   # update AFTER exit checks so this bar's high can't protect its low

        # ---- No SL/TP -> forced exit at end of the (capped) window ----
        if exit_reason is None:
            last = obs.iloc[-1]
            exit_price = float(last['close'])
            exit_time = last['dt_str']
            # capped => hit the max-hold cutoff; otherwise data simply ran out
            exit_reason = 'TIME' if capped else 'OPEN'

        # ---- P&L ----
        pnl = qty1 * (exit_price - entry1_price)
        invested = qty1 * entry1_price
        if e2_filled:
            pnl += qty2 * (exit_price - entry2_price)
            invested += qty2 * entry2_price
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0.0

        days_held = (pd.Timestamp(exit_time).date() - pd.Timestamp(entry1_time).date()).days

        return ResultReactionTrade(
            symbol=symbol,
            result_date=str(R),
            result_session=str(result_session),
            marked_high=round(marked_high, 2),
            observation_start=observation_start,
            entry1_time=entry1_time,
            entry1_price=round(entry1_price, 2),
            qty1=qty1,
            entry2_time=entry2_time,
            entry2_price=round(entry2_price, 2),
            qty2=qty2,
            avg_price=round(avg, 2),
            sl_price=round(sl, 2) if sl is not None else 0.0,
            tp_price=round(tp, 2),
            exit_time=exit_time,
            exit_price=round(exit_price, 2),
            exit_reason=exit_reason,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 3),
            days_held=days_held,
        )

    @staticmethod
    def _stub(symbol, result_date, reason, result_session='', marked_high=0.0,
              observation_start='') -> ResultReactionTrade:
        """A non-trade row (NO_DATA / NO_ENTRY) so every symbol is visible in output."""
        return ResultReactionTrade(
            symbol=symbol, result_date=str(pd.Timestamp(result_date).date()),
            result_session=result_session, marked_high=round(float(marked_high), 2),
            observation_start=observation_start,
            entry1_time='', entry1_price=0.0, qty1=0,
            entry2_time='', entry2_price=0.0, qty2=0,
            avg_price=0.0, sl_price=0.0, tp_price=0.0,
            exit_time='', exit_price=0.0, exit_reason=reason,
            pnl=0.0, pnl_pct=0.0, days_held=0,
        )


def trades_to_dataframe(trades: list) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
