"""
Inter-Sector Pairs Trading Backtest Engine.

Strategy:
  Scan (end of previous trading day):
    1. Daily RSI(14) + RSI_MA(14) filter:
       - Long bag:  55 < RSI < 70  AND  RSI > RSI_MA
       - Short bag: 30 < RSI < 45  AND  RSI < RSI_MA
    2. If either bag is empty → skip next trading day

  Trade day at 9:30 AM:
    3. Compute 5-min RS(50) using last complete 5-min bar (9:25) on trade day
       plus multi-day lookback as needed:
       RS = (close / close[50]) / (nifty / nifty[50]) - 1
       - Long candidate:  highest RS in long bag
       - Short candidate: lowest  RS in short bag
       - Ties broken by PREF_ORDER

  Entry (9:30 AM open, qty = 1 each):
    - Buy  long_stock  at open of 9:30 candle
    - Short short_stock at open of 9:30 candle

  Exit (checked on each 1-min candle close from 9:30 onwards):
    combined_pnl_pct = long_leg_pnl_pct + short_leg_pnl_pct
    - TP:  combined_pnl_pct >= tp_pct   (default 1.0%, pinned exactly)
    - SL:  combined_pnl_pct <= -sl_pct  (default 0.75%, pinned exactly)
    - EOD: force exit at last candle
"""

import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STOCKS_DIR = os.path.join(BASE_DIR, 'data', 'stocks')
NIFTY_PATH = os.path.join(BASE_DIR, 'data', 'spot', 'nifty', 'NIFTY_1m.parquet')

DEFAULT_UNIVERSE: Dict[str, List[str]] = {
    "4W":        ["M&M", "MARUTI"],
    "Cement":    ["AMBUJACEM", "ULTRACEMCO"],
    "FMCG":      ["GODREJCP", "HINDUNILVR", "NESTLEIND", "TATACONSUM"],
    "PSU":       ["BANKBARODA", "CANBK", "SBIN", "UNIONBANK", "PNB"],
    "Pvt":       ["HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK"],
    "Pharma":    ["CIPLA", "DIVISLAB", "DRREDDY", "SUNPHARMA", "TORNTPOWER", "ZYDUSLIFE"],
    "IT":        ["HCLTECH", "INFY", "TCS", "LTIM", "TECHM", "WIPRO"],
    "Steel":     ["JSWSTEEL", "JINDALSTEL", "TATASTEEL"],
    "OMC":       ["BPCL", "IOC"],
    "Insurance": ["HDFCLIFE", "SBILIFE"],
}

# Preference order for tie-breaking when two stocks have identical RS.
# Lower index = higher preference.
PREF_ORDER: List[str] = [
    "HDFCBANK", "TCS", "ICICIBANK", "SBIN", "INFY",
    "HINDUNILVR", "AXISBANK", "KOTAKBANK", "SUNPHARMA", "ULTRACEMCO",
    "MARUTI", "BPCL", "NESTLEIND", "HCLTECH", "TATASTEEL",
    "JSWSTEEL", "IOC", "CIPLA", "DIVISLAB", "DRREDDY",
    "TATACONSUM", "SBILIFE", "HDFCLIFE", "GODREJCP", "AMBUJACEM",
    "M&M", "JINDALSTEL", "BANKBARODA", "PNB", "CANBK",
    "UNIONBANK", "TATAMOTORS",
]
_PREF_RANK: Dict[str, int] = {sym: i for i, sym in enumerate(PREF_ORDER)}
_PREF_FALLBACK = len(PREF_ORDER)  # rank for any symbol not in the list


@dataclass
class PairTrade:
    trade_date: str
    scan_date: str

    long_stock: str
    long_rsi: float
    long_rsi_ma: float
    long_rs: float
    long_entry_price: float
    long_exit_price: float
    long_pnl_pct: float
    long_pnl_inr: float

    short_stock: str
    short_rsi: float
    short_rsi_ma: float
    short_rs: float
    short_entry_price: float
    short_exit_price: float
    short_pnl_pct: float
    short_pnl_inr: float

    combined_pnl_pct: float
    combined_pnl_inr: float
    exit_time: str
    exit_reason: str  # 'TP' | 'SL' | 'EOD'


class PairsBacktestEngine:

    def __init__(
        self,
        start_date: str,
        end_date: str,
        universe: Optional[Dict[str, List[str]]] = None,
        rsi_period: int = 14,
        rsi_ma_period: int = 14,
        rs_period: int = 50,
        tp_pct: float = 1.0,
        sl_pct: float = 0.75,
    ):
        self.start_date = pd.Timestamp(start_date).date()
        self.end_date = pd.Timestamp(end_date).date()
        self.universe = universe or DEFAULT_UNIVERSE
        self.all_stocks = [s for stocks in self.universe.values() for s in stocks]
        self.rsi_period = rsi_period
        self.rsi_ma_period = rsi_ma_period
        self.rs_period = rs_period
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

        # Populated during run()
        self._stock_1m: Dict[str, pd.DataFrame] = {}
        self._nifty_1m: Optional[pd.DataFrame] = None
        self._daily_rsi: Dict[str, pd.Series] = {}
        self._daily_rsi_ma: Dict[str, pd.Series] = {}
        self._5min_close: Dict[str, pd.Series] = {}
        self._nifty_5min: Optional[pd.Series] = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(self, progress_callback=None) -> List[PairTrade]:
        self._load_all_data(progress_callback)
        trading_days = self._get_trading_days()
        trades = []

        for idx in range(1, len(trading_days)):
            trade_day = trading_days[idx]
            scan_day  = trading_days[idx - 1]

            if trade_day < self.start_date or trade_day > self.end_date:
                continue

            bags = self._scan_day(scan_day)
            if bags is None:
                continue

            trade = self._execute_trade(trade_day, scan_day, bags)
            if trade:
                trades.append(trade)

        return trades

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_all_data(self, progress_callback=None):
        total = len(self.all_stocks) + 1
        for i, symbol in enumerate(self.all_stocks):
            if progress_callback:
                progress_callback(i, total, symbol)
            df = self._load_1m(os.path.join(STOCKS_DIR, symbol, f'{symbol}_1m.parquet'))
            if df is None:
                continue
            self._stock_1m[symbol] = df
            daily_close = df.groupby('date')['close'].last()
            self._daily_rsi[symbol], self._daily_rsi_ma[symbol] = self._wilder_rsi_daily(daily_close)
            self._5min_close[symbol] = self._resample_5min(df)

        if progress_callback:
            progress_callback(len(self.all_stocks), total, 'NIFTY50')
        nifty_df = self._load_1m(NIFTY_PATH)
        if nifty_df is not None:
            self._nifty_1m = nifty_df
            self._nifty_5min = self._resample_5min(nifty_df)

    @staticmethod
    def _load_1m(path: str) -> Optional[pd.DataFrame]:
        if not os.path.exists(path):
            return None
        df = pd.read_parquet(path)
        dt = pd.to_datetime(df['datetime'])
        if dt.dt.tz is None:
            dt = dt.dt.tz_localize('Asia/Kolkata')
        else:
            dt = dt.dt.tz_convert('Asia/Kolkata')
        df = df.copy()
        df['datetime'] = dt
        df['date']     = dt.dt.date
        df['time']     = dt.dt.strftime('%H:%M')
        return df.sort_values('datetime').reset_index(drop=True)

    @staticmethod
    def _resample_5min(df: pd.DataFrame) -> pd.Series:
        """1-min close → 5-min close, market hours only."""
        s = df.set_index('datetime')['close'].between_time('09:15', '15:29')
        return s.resample('5min').last().dropna()

    def _wilder_rsi_daily(self, daily_close: pd.Series) -> Tuple[pd.Series, pd.Series]:
        """Wilder's RSI on date-indexed daily close. Exact replica of indicators/rsi.py."""
        p = self.rsi_period
        closes = daily_close.values
        n = len(closes)
        rsi_arr = np.full(n, np.nan)

        if n >= p + 1:
            delta  = np.diff(closes, prepend=np.nan)
            gains  = np.maximum(delta, 0.0)
            losses = np.maximum(-delta, 0.0)

            avg_gain = float(np.mean(gains[1:p + 1]))
            avg_loss = float(np.mean(losses[1:p + 1]))
            rsi_arr[p] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

            for i in range(p + 1, n):
                avg_gain = (avg_gain * (p - 1) + gains[i]) / p
                avg_loss = (avg_loss * (p - 1) + losses[i]) / p
                rsi_arr[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

        rsi_s    = pd.Series(rsi_arr, index=daily_close.index)
        rsi_ma_s = rsi_s.rolling(self.rsi_ma_period).mean()
        return rsi_s, rsi_ma_s

    # ------------------------------------------------------------------
    # Scan (RSI filter only — RS is computed at 9:30 on trade day)
    # ------------------------------------------------------------------

    def _get_trading_days(self) -> list:
        dates: set = set()
        for df in self._stock_1m.values():
            dates.update(df['date'].unique())
        return sorted(dates)

    def _scan_day(self, scan_day) -> Optional[dict]:
        """
        EOD RSI filter on scan_day.
        Returns {'long_bag': [...], 'short_bag': [...]} or None if either is empty.
        Each item: {'symbol': str, 'rsi': float, 'rsi_ma': float}
        """
        long_bag: list = []
        short_bag: list = []

        for symbol in self.all_stocks:
            rsi_s    = self._daily_rsi.get(symbol)
            rsi_ma_s = self._daily_rsi_ma.get(symbol)
            if rsi_s is None or rsi_ma_s is None:
                continue

            rsi    = rsi_s.get(scan_day)
            rsi_ma = rsi_ma_s.get(scan_day)
            if rsi is None or rsi_ma is None or np.isnan(rsi) or np.isnan(rsi_ma):
                continue

            if 55 < rsi < 70 and rsi > rsi_ma:
                long_bag.append({'symbol': symbol, 'rsi': rsi, 'rsi_ma': rsi_ma})
            elif 30 < rsi < 45 and rsi < rsi_ma:
                short_bag.append({'symbol': symbol, 'rsi': rsi, 'rsi_ma': rsi_ma})

        if not long_bag or not short_bag:
            return None

        return {'long_bag': long_bag, 'short_bag': short_bag}

    def _compute_rs_at_930(self, symbol: str, trade_day) -> Optional[float]:
        """
        5-min RS(50) using data up to the 9:25 bar on trade_day (last bar complete at 9:30).
        Looks back across multiple trading days as needed.
        Formula: RS = (close / close[50]) / (nifty / nifty[50]) - 1
        """
        s5 = self._5min_close.get(symbol)
        if s5 is None or self._nifty_5min is None:
            return None

        # The 9:25 5-min bar is the last one fully closed at 9:30 AM
        cutoff = pd.Timestamp(str(trade_day) + ' 09:25').tz_localize('Asia/Kolkata')

        stock_data = s5[s5.index <= cutoff]
        nifty_data = self._nifty_5min[self._nifty_5min.index <= cutoff]

        aligned = pd.concat(
            [stock_data.rename('s'), nifty_data.rename('n')], axis=1, join='inner'
        ).dropna()

        # Need > rs_period bars so iloc[-(rs_period+1)] is valid
        if len(aligned) <= self.rs_period:
            return None

        s_now  = float(aligned['s'].iloc[-1])
        s_then = float(aligned['s'].iloc[-(self.rs_period + 1)])
        n_now  = float(aligned['n'].iloc[-1])
        n_then = float(aligned['n'].iloc[-(self.rs_period + 1)])

        if s_then == 0 or n_then == 0 or n_now == 0:
            return None

        return (s_now / s_then) / (n_now / n_then) - 1

    def _select_pair(self, long_bag: list, short_bag: list, trade_day) -> Optional[dict]:
        """
        Computes RS at 9:30 AM for every candidate, then picks:
          - Long:  highest RS (ties broken by PREF_ORDER)
          - Short: lowest  RS (ties broken by PREF_ORDER)
        Returns {'long': {...}, 'short': {...}} or None.
        """
        for item in long_bag + short_bag:
            item['rs'] = self._compute_rs_at_930(item['symbol'], trade_day)

        long_bag  = [x for x in long_bag  if x['rs'] is not None]
        short_bag = [x for x in short_bag if x['rs'] is not None]

        if not long_bag or not short_bag:
            return None

        def pref(sym: str) -> int:
            return _PREF_RANK.get(sym, _PREF_FALLBACK)

        best_long  = min(long_bag,  key=lambda x: (-x['rs'], pref(x['symbol'])))
        best_short = min(short_bag, key=lambda x: ( x['rs'], pref(x['symbol'])))

        return {'long': best_long, 'short': best_short}

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute_trade(self, trade_day, scan_day, bags: dict) -> Optional[PairTrade]:
        pair = self._select_pair(bags['long_bag'], bags['short_bag'], trade_day)
        if pair is None:
            return None

        li = pair['long']
        si = pair['short']

        long_df  = self._get_day_1m(li['symbol'], trade_day)
        short_df = self._get_day_1m(si['symbol'], trade_day)
        if long_df is None or short_df is None:
            return None

        # Entry at open of 9:30 candle
        long_930  = long_df[long_df['time'] == '09:30']
        short_930 = short_df[short_df['time'] == '09:30']
        if long_930.empty or short_930.empty:
            return None

        long_entry  = float(long_930.iloc[0]['open'])
        short_entry = float(short_930.iloc[0]['open'])
        if long_entry <= 0 or short_entry <= 0:
            return None

        # Exit check: candle closes from 9:30 onwards
        l_close = long_df[long_df['time'] >= '09:30'].set_index('time')['close']
        s_close = short_df[short_df['time'] >= '09:30'].set_index('time')['close']
        times   = l_close.index.intersection(s_close.index)
        if len(times) == 0:
            return None

        exit_lp = exit_sp = None
        exit_time = exit_reason = None

        for t in times:
            lc = float(l_close[t])
            sc = float(s_close[t])
            l_pnl    = (lc - long_entry)  / long_entry  * 100
            s_pnl    = (short_entry - sc) / short_entry * 100
            combined = l_pnl + s_pnl

            if combined >= self.tp_pct:
                exit_lp, exit_sp, exit_time, exit_reason = lc, sc, t, 'TP'
                break
            if combined <= -self.sl_pct:
                exit_lp, exit_sp, exit_time, exit_reason = lc, sc, t, 'SL'
                break

        if exit_reason is None:
            last        = times[-1]
            exit_lp     = float(l_close[last])
            exit_sp     = float(s_close[last])
            exit_time   = last
            exit_reason = 'EOD'

        l_pnl_pct = (exit_lp - long_entry)  / long_entry  * 100
        s_pnl_pct = (short_entry - exit_sp) / short_entry * 100
        l_pnl_inr = exit_lp - long_entry
        s_pnl_inr = short_entry - exit_sp

        # Pin combined to exact threshold for TP/SL; EOD uses actual candle-close value
        if exit_reason == 'TP':
            combined_pct = self.tp_pct
        elif exit_reason == 'SL':
            combined_pct = -self.sl_pct
        else:
            combined_pct = l_pnl_pct + s_pnl_pct

        return PairTrade(
            trade_date=str(trade_day),
            scan_date=str(scan_day),
            long_stock=li['symbol'],
            long_rsi=round(float(li['rsi']), 2),
            long_rsi_ma=round(float(li['rsi_ma']), 2),
            long_rs=round(float(li['rs']), 6),
            long_entry_price=round(long_entry, 2),
            long_exit_price=round(exit_lp, 2),
            long_pnl_pct=round(l_pnl_pct, 3),
            long_pnl_inr=round(l_pnl_inr, 2),
            short_stock=si['symbol'],
            short_rsi=round(float(si['rsi']), 2),
            short_rsi_ma=round(float(si['rsi_ma']), 2),
            short_rs=round(float(si['rs']), 6),
            short_entry_price=round(short_entry, 2),
            short_exit_price=round(exit_sp, 2),
            short_pnl_pct=round(s_pnl_pct, 3),
            short_pnl_inr=round(s_pnl_inr, 2),
            combined_pnl_pct=round(combined_pct, 3),
            combined_pnl_inr=round(l_pnl_inr + s_pnl_inr, 2),
            exit_time=str(exit_time),
            exit_reason=exit_reason,
        )

    def _get_day_1m(self, symbol: str, date) -> Optional[pd.DataFrame]:
        df = self._stock_1m.get(symbol)
        if df is None:
            return None
        day = df[
            (df['date'] == date) &
            (df['time'] >= '09:15') &
            (df['time'] <= '15:30')
        ]
        return day.reset_index(drop=True) if not day.empty else None


def trades_to_dataframe(trades: List[PairTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
