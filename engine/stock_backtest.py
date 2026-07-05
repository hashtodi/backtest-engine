"""
Stock Gap + EMA5 Volume Backtest Engine.

Strategy:
  Signal conditions (checked per candle within 9:15-9:30):
    1. Gap >= 1.5% from prev close  →  direction (long if gap up, short if gap down)
    2. Volume >= 8x avg of last 3 trading days (all-minute avg)
    3. Volume * close >= 4 crore

  Entry:
    - Once signal fires, take first EMA5 touch within 9:15-9:30
    - Signal and EMA5 touch may be on same candle
    - Entry price = EMA5 value at touch candle
    - Max 1 trade per stock per day, qty = 1 share

  Exit:
    - SL: 1% from entry
    - TP: 1.5% from entry
    - EOD: force exit at last candle of the day
"""

import os
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd

STOCKS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'stocks')


@dataclass
class StockTrade:
    symbol: str
    date: str
    direction: str          # 'long' or 'short'
    gap_pct: float
    avg_3d_volume: float
    signal_time: str        # when volume condition first fired
    signal_volume: int
    entry_time: str         # when EMA5 was touched
    entry_price: float      # EMA5 value at entry candle
    sl_price: float
    tp_price: float
    exit_time: str
    exit_price: float
    exit_reason: str        # 'SL' | 'TP' | 'EOD'
    pnl: float              # in ₹ (qty = 1)
    pnl_pct: float


class StockBacktestEngine:
    """Backtests spot stock gap + EMA5 volume strategy across multiple stocks."""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        stocks: Optional[list] = None,
        gap_pct_threshold: float = 1.5,
        volume_multiplier: float = 8.0,
        min_value_cr: float = 4.0,
        ema_period: int = 5,
        sl_pct: float = 1.0,
        tp_pct: float = 1.5,
        entry_start: str = '09:15',
        entry_end: str = '09:30',
    ):
        self.start_date = pd.Timestamp(start_date).date()
        self.end_date = pd.Timestamp(end_date).date()
        self.stocks = stocks
        self.gap_pct_threshold = gap_pct_threshold
        self.volume_multiplier = volume_multiplier
        self.min_value = min_value_cr * 1e7   # crore → rupees
        self.ema_period = ema_period
        self.sl_pct = sl_pct / 100
        self.tp_pct = tp_pct / 100
        self.entry_start = entry_start
        self.entry_end = entry_end

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get_available_stocks(self) -> list:
        if not os.path.exists(STOCKS_DIR):
            return []
        return sorted(
            d for d in os.listdir(STOCKS_DIR)
            if os.path.isdir(os.path.join(STOCKS_DIR, d))
        )

    def run(self, progress_callback=None) -> list:
        """
        Run backtest across all (or selected) stocks.

        progress_callback(i, total, symbol) is called before each stock.
        Returns list of StockTrade dataclasses.
        """
        symbols = self.stocks if self.stocks else self.get_available_stocks()
        all_trades = []
        for i, symbol in enumerate(symbols):
            if progress_callback:
                progress_callback(i, len(symbols), symbol)
            all_trades.extend(self._run_stock(symbol))
        return all_trades

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_stock(self, symbol: str) -> Optional[pd.DataFrame]:
        path = os.path.join(STOCKS_DIR, symbol, f'{symbol}_1m.parquet')
        if not os.path.exists(path):
            return None

        df = pd.read_parquet(path)
        dt = pd.to_datetime(df['datetime'])
        if dt.dt.tz is None:
            dt = dt.dt.tz_localize('Asia/Kolkata')
        else:
            dt = dt.dt.tz_convert('Asia/Kolkata')

        df = df.copy()
        df['date'] = dt.dt.date
        df['time'] = dt.dt.strftime('%H:%M')
        df = df.sort_values('ts').reset_index(drop=True)

        # EMA5 calculated continuously across all history (not reset per day)
        df['ema5'] = df['close'].ewm(span=self.ema_period, adjust=False).mean()
        return df

    def _run_stock(self, symbol: str) -> list:
        df = self._load_stock(symbol)
        if df is None or df.empty:
            return []

        all_dates = sorted(df['date'].unique())

        # Precompute per-day aggregates (avoids repeated groupby inside loop)
        vol_sum = df.groupby('date')['volume'].sum()
        vol_count = df.groupby('date')['volume'].count()
        day_open = df.groupby('date')['open'].first()
        day_close = df.groupby('date')['close'].last()

        trades = []
        for idx, date in enumerate(all_dates):
            if date < self.start_date or date > self.end_date:
                continue
            if idx < 3:
                continue  # need 3 prior days for avg volume

            trade = self._process_day(
                df, date, all_dates, idx,
                vol_sum, vol_count, day_open, day_close,
                symbol,
            )
            if trade:
                trades.append(trade)

        return trades

    def _process_day(
        self, df, date, all_dates, idx,
        vol_sum, vol_count, day_open, day_close,
        symbol,
    ) -> Optional[StockTrade]:

        # ---- Gap check ----
        prev_date = all_dates[idx - 1]
        prev_close = day_close.get(prev_date)
        today_open = day_open.get(date)
        if not prev_close or not today_open or prev_close == 0:
            return None

        gap_pct = (today_open - prev_close) / prev_close * 100
        if gap_pct >= self.gap_pct_threshold:
            direction = 'long'
        elif gap_pct <= -self.gap_pct_threshold:
            direction = 'short'
        else:
            return None

        # ---- Avg volume: mean of all 1-min candles across prev 3 trading days ----
        prev_3 = all_dates[idx - 3:idx]
        total_vol = sum(vol_sum.get(d, 0) for d in prev_3)
        total_cnt = sum(vol_count.get(d, 0) for d in prev_3)
        avg_vol = total_vol / total_cnt if total_cnt > 0 else 0
        if avg_vol == 0:
            return None

        # ---- Today's data (reset index → label == position) ----
        today = df[df['date'] == date].copy().reset_index(drop=True)

        # ---- Entry window: 9:15–9:30 ----
        ew = today[(today['time'] >= self.entry_start) & (today['time'] <= self.entry_end)].copy()
        if ew.empty:
            return None

        # Signal condition per candle
        ew['_signal'] = (
            (ew['volume'] >= self.volume_multiplier * avg_vol) &
            (ew['volume'] * ew['close'] >= self.min_value)
        )
        # EMA5 touch: candle's price range includes EMA5
        ew['_ema_touch'] = (ew['low'] <= ew['ema5']) & (ew['ema5'] <= ew['high'])

        # First candle where signal fires
        signal_candles = ew[ew['_signal']]
        if signal_candles.empty:
            return None
        first_signal_label = signal_candles.index[0]
        first_signal = ew.loc[first_signal_label]

        # EMA5 touch starts from the NEXT candle after signal.
        # Signal candle's volume/EMA5 are only known at candle close —
        # we can only act on that data from the following candle onwards.
        after_signal = ew.loc[first_signal_label + 1:]
        touch_candles = after_signal[after_signal['_ema_touch']]
        if touch_candles.empty:
            return None

        entry_label = touch_candles.index[0]
        entry_row = today.loc[entry_label]
        entry_price = float(entry_row['ema5'])

        # ---- SL / TP ----
        if direction == 'long':
            sl_price = entry_price * (1 - self.sl_pct)
            tp_price = entry_price * (1 + self.tp_pct)
        else:
            sl_price = entry_price * (1 + self.sl_pct)
            tp_price = entry_price * (1 - self.tp_pct)

        # ---- Exit: candles strictly after entry ----
        after_entry = today.loc[entry_label + 1:]

        if after_entry.empty:
            # Entry was on last candle of day
            return self._make_trade(
                symbol, date, direction, gap_pct, avg_vol,
                first_signal, entry_row, entry_price, sl_price, tp_price,
                exit_row=entry_row, exit_price=float(entry_row['close']), exit_reason='EOD',
            )

        if direction == 'long':
            sl_hit = after_entry['low'] <= sl_price
            tp_hit = after_entry['high'] >= tp_price
        else:
            sl_hit = after_entry['high'] >= sl_price
            tp_hit = after_entry['low'] <= tp_price

        any_exit = sl_hit | tp_hit

        if not any_exit.any():
            # No SL/TP hit — EOD exit at last candle
            e = after_entry.iloc[-1]
            return self._make_trade(
                symbol, date, direction, gap_pct, avg_vol,
                first_signal, entry_row, entry_price, sl_price, tp_price,
                exit_row=e, exit_price=float(e['close']), exit_reason='EOD',
            )

        first_exit_label = any_exit[any_exit].index[0]
        e = after_entry.loc[first_exit_label]

        if sl_hit[first_exit_label]:
            exit_price, exit_reason = sl_price, 'SL'
        else:
            exit_price, exit_reason = tp_price, 'TP'

        return self._make_trade(
            symbol, date, direction, gap_pct, avg_vol,
            first_signal, entry_row, entry_price, sl_price, tp_price,
            exit_row=e, exit_price=exit_price, exit_reason=exit_reason,
        )

    def _make_trade(
        self, symbol, date, direction, gap_pct, avg_vol,
        first_signal, entry_row, entry_price, sl_price, tp_price,
        exit_row, exit_price, exit_reason,
    ) -> StockTrade:
        pnl = (exit_price - entry_price) if direction == 'long' else (entry_price - exit_price)
        return StockTrade(
            symbol=symbol,
            date=str(date),
            direction=direction,
            gap_pct=round(float(gap_pct), 3),
            avg_3d_volume=round(float(avg_vol), 2),
            signal_time=str(first_signal['time']),
            signal_volume=int(first_signal['volume']),
            entry_time=str(entry_row['time']),
            entry_price=round(float(entry_price), 2),
            sl_price=round(float(sl_price), 2),
            tp_price=round(float(tp_price), 2),
            exit_time=str(exit_row['time']),
            exit_price=round(float(exit_price), 2),
            exit_reason=exit_reason,
            pnl=round(float(pnl), 2),
            pnl_pct=round(float(pnl / entry_price * 100), 3),
        )


def trades_to_dataframe(trades: list) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])
