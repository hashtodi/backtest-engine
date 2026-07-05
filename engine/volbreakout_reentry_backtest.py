"""
Volume-Breakout Re-entry Backtest Engine (cash equity, long only).

Level:
  - Aggregate 1-min data to daily OHLCV.
  - volMA = SMA of the prior `vol_ma_period` (75) daily volumes (excludes the candle).
  - Breakout candle = a daily candle STRICTLY BEFORE the earning date whose day-volume
    > `vol_mult` (5x) * volMA. `level` = the daily HIGH of the LATEST such candle.

Entry (first) -- the fill becomes the BASE for all subsequent levels:
  - From the next trading day after earning, within `max_hold_days` (80 calendar) days.
  - If that next day's open < breakout-high -> enter at the open; base = open.
  - Else (open >= breakout-high) wait for the first bar that CLOSES <= breakout-high ->
    enter at the breakout-high; base = breakout-high.
  - If neither happens within the window -> NO_ENTRY.

Exit (every entry) -- ALL triggers on the 1-min CLOSE (never high/low); fill at the level:
  - SL = base*(1-sl_pct)          [default 0.95*base]
  - TP = base*(1+tp_pct)          [default 1.08*base]
  - Profit-lock ladder (vs base): close>=1.05 -> SL=1.01; >=1.06 -> 1.02; >=1.07 -> 1.03.
    Locks only ratchet up; ratcheting uses this bar's close AFTER the stop/TP checks.
  - Stop fires when close <= current stop (fill at the stop); TP when close >= TP (fill at
    TP). Time cap: force-exit at the last bar's close on/before earning+max_hold_days
    (TIME); if data ends first, OPEN.

Re-entry (at most `max_reentries` = 1, only after a STOP-OUT, exit reason SL) -- same base:
  - After the stop-out, a bar must CLOSE below the SL level (< 0.95*base), THEN a later bar
    must CLOSE above the base -> re-enter at the base. Same SL/TP/lock rules, same base.
  - Not after TP / TIME / OPEN.

Sizing: single leg, full `capital_per_stock` per entry; qty = floor(capital / base).
"""

import os
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd

STOCKS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'stocks')


@dataclass
class VolBreakoutTrade:
    symbol: str
    earning_date: str
    leg: int                 # 1 = first entry, 2 = re-entry
    breakout_date: str       # daily candle whose high is the level
    level: float
    entry_time: str
    entry_price: float       # actual fill
    qty: int
    sl_init: float           # initial -5% stop level
    tp_price: float          # +8% level
    locked_sl: float         # final stop level in force at exit (>= sl_init if a lock armed)
    exit_time: str
    exit_price: float
    exit_reason: str         # 'SL' | 'TP' | 'TIME' | 'OPEN' | 'NO_ENTRY' | 'NO_LEVEL' | 'NO_DATA'
    pnl: float
    pnl_pct: float
    days_held: int


class VolBreakoutReentryEngine:

    def __init__(
        self,
        result_dates: dict,                 # {symbol: 'YYYY-MM-DD' earning date}
        capital_per_stock: float = 100000.0,
        vol_ma_period: int = 75,
        vol_mult: float = 5.0,
        sl_pct: float = 5.0,
        tp_pct: float = 8.0,
        lock_steps=((5, 1), (6, 2), (7, 3)),  # (profit% vs level, locked profit% vs level)
        max_hold_days: int = 80,
        max_reentries: int = 1,
    ):
        self.result_dates = result_dates
        self.capital_per_stock = capital_per_stock
        self.vol_ma_period = vol_ma_period
        self.vol_mult = vol_mult
        self.sl_pct = sl_pct / 100.0
        self.tp_pct = tp_pct / 100.0
        self.lock_steps = sorted(lock_steps)        # ascending by trigger
        self.max_hold_days = max_hold_days
        self.max_reentries = max_reentries

    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> list:
        syms = list(self.result_dates.keys())
        trades = []
        for i, s in enumerate(syms):
            if progress_callback:
                progress_callback(i, len(syms), s)
            trades.extend(self._run_stock(s, self.result_dates[s]))
        return trades

    # ------------------------------------------------------------------

    def _load_stock(self, symbol: str) -> Optional[pd.DataFrame]:
        path = os.path.join(STOCKS_DIR, symbol, f'{symbol}_1m.parquet')
        if not os.path.exists(path):
            return None
        df = pd.read_parquet(path)
        dt = pd.to_datetime(df['datetime'])
        dt = dt.dt.tz_convert('Asia/Kolkata') if dt.dt.tz is not None else dt.dt.tz_localize('Asia/Kolkata')
        df = df.copy()
        df['date'] = dt.dt.date
        df['dt_str'] = dt.dt.strftime('%Y-%m-%d %H:%M')
        sort_col = 'ts' if 'ts' in df.columns else 'datetime'
        return df.sort_values(sort_col).reset_index(drop=True)

    def _daily_level(self, df: pd.DataFrame, earning_date):
        """Return (level, breakout_date) or (None, None)."""
        daily = (df.groupby('date')
                   .agg(vol=('volume', 'sum'), high=('high', 'max'))
                   .reset_index()
                   .sort_values('date')
                   .reset_index(drop=True))
        # SMA of the prior `vol_ma_period` volumes (shift excludes the candle itself)
        daily['vma'] = daily['vol'].shift(1).rolling(self.vol_ma_period).mean()
        cand = daily[(daily['date'] < earning_date)
                     & daily['vma'].notna()
                     & (daily['vol'] > self.vol_mult * daily['vma'])]
        if cand.empty:
            return None, None
        last = cand.iloc[-1]
        return float(last['high']), last['date']

    def _run_stock(self, symbol: str, earning_date: str) -> list:
        df = self._load_stock(symbol)
        if df is None or df.empty:
            return [self._stub(symbol, earning_date, 'NO_DATA')]

        R = pd.Timestamp(earning_date).date()
        level, bdate = self._daily_level(df, R)
        if level is None:
            return [self._stub(symbol, earning_date, 'NO_LEVEL')]

        cutoff = (pd.Timestamp(R) + pd.Timedelta(days=self.max_hold_days)).date()
        obs_full = df[df['date'] > R].reset_index(drop=True)
        capped = bool((obs_full['date'] > cutoff).any())
        obs = obs_full[obs_full['date'] <= cutoff].reset_index(drop=True)
        if obs.empty:
            return [self._stub(symbol, earning_date, 'NO_DATA', level=level, bdate=bdate)]

        o = obs['open'].to_numpy()
        c = obs['close'].to_numpy()
        tstr = obs['dt_str'].to_numpy()

        trades = []

        # ---- Entry-1: the fill is the BASE for all levels ----
        if o[0] < level:
            e_idx, base = 0, float(o[0])                       # next-day open below breakout-high
        else:
            touch = next((i for i in range(len(c)) if c[i] <= level), None)   # close-based touch
            if touch is None:
                return [self._stub(symbol, earning_date, 'NO_ENTRY', level=level, bdate=bdate)]
            e_idx, base = touch, float(level)

        res = self._simulate(c, base, e_idx, tstr, capped)
        trades.append(self._make_trade(symbol, R, 1, bdate, level, base, e_idx, tstr, res))

        # ---- Re-entry (max 1, only after a stop-out); same base ----
        reentries = 0
        while res['reason'] == 'SL' and reentries < self.max_reentries:
            r_idx = self._find_reentry(c, base, res['idx'])
            if r_idx is None:
                break
            res = self._simulate(c, base, r_idx, tstr, capped)
            trades.append(self._make_trade(symbol, R, 2 + reentries, bdate, level, base, r_idx, tstr, res))
            reentries += 1

        return trades

    def _simulate(self, c, base, entry_idx, tstr, capped) -> dict:
        """Close-based walk after entry_idx; return {idx, price, time, reason, locked_sl, tp}.
        Stop/TP/lock all evaluated on the 1-min CLOSE; exits fill at the exact level."""
        sl = base * (1 - self.sl_pct)
        tp = base * (1 + self.tp_pct)
        ladder = [(base * (1 + t / 100.0), base * (1 + lk / 100.0)) for t, lk in self.lock_steps]
        cur_sl = sl
        n = len(c)
        for j in range(entry_idx + 1, n):
            cl = float(c[j])
            # 1) stop (current stop carried from prior bars)
            if cl <= cur_sl:
                return dict(idx=j, price=cur_sl, time=str(tstr[j]), reason='SL', locked_sl=cur_sl, tp=tp)
            # 2) TP
            if cl >= tp:
                return dict(idx=j, price=tp, time=str(tstr[j]), reason='TP', locked_sl=cur_sl, tp=tp)
            # 3) ratchet the lock on this bar's close (after the stop/TP checks)
            for trig, lock in ladder:
                if cl >= trig and lock > cur_sl:
                    cur_sl = lock
        last = n - 1
        return dict(idx=last, price=float(c[last]), time=str(tstr[last]),
                    reason=('TIME' if capped else 'OPEN'), locked_sl=cur_sl, tp=tp)

    def _find_reentry(self, c, base, from_idx):
        """After a stop-out: a close < 0.95*base (arm), THEN a close > base -> re-enter @ base."""
        sl_lvl = base * (1 - self.sl_pct)
        armed = False
        for j in range(from_idx + 1, len(c)):
            if not armed:
                if float(c[j]) < sl_lvl:
                    armed = True
                continue
            if float(c[j]) > base:
                return j
        return None

    def _make_trade(self, symbol, R, leg, bdate, level, base, e_idx, tstr, res) -> VolBreakoutTrade:
        qty = int(self.capital_per_stock // base)
        exit_price = res['price']
        pnl = qty * (exit_price - base)
        invested = qty * base
        days = (pd.Timestamp(res['time'][:10]).date() - pd.Timestamp(str(tstr[e_idx])[:10]).date()).days
        return VolBreakoutTrade(
            symbol=symbol, earning_date=str(R), leg=leg,
            breakout_date=str(bdate), level=round(level, 2),
            entry_time=str(tstr[e_idx]), entry_price=round(base, 2), qty=qty,
            sl_init=round(base * (1 - self.sl_pct), 2), tp_price=round(res['tp'], 2),
            locked_sl=round(res['locked_sl'], 2),
            exit_time=res['time'], exit_price=round(exit_price, 2), exit_reason=res['reason'],
            pnl=round(pnl, 2), pnl_pct=round(pnl / invested * 100, 3) if invested else 0.0,
            days_held=days,
        )

    @staticmethod
    def _stub(symbol, earning_date, reason, level=0.0, bdate='') -> VolBreakoutTrade:
        return VolBreakoutTrade(
            symbol=symbol, earning_date=str(pd.Timestamp(earning_date).date()), leg=0,
            breakout_date=str(bdate), level=round(float(level), 2),
            entry_time='', entry_price=0.0, qty=0, sl_init=0.0, tp_price=0.0, locked_sl=0.0,
            exit_time='', exit_price=0.0, exit_reason=reason, pnl=0.0, pnl_pct=0.0, days_held=0,
        )


def trades_to_dataframe(trades: list) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
