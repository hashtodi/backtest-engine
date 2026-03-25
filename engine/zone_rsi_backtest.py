"""
Zone RSI Bias backtest engine.

Custom strategy that needs its own engine because:
  - Bias state tracking (BULLISH/BEARISH/NEUTRAL transitions)
  - Conditional direction (CE on bullish shift, PE on bearish shift)
  - Indicator-based exit (RSI vs RSI_MA threshold)
  - Stop loss on option entry price (not generic SL/TP framework)

Zone (on Nifty spot 1-min candle):
  BULLISH:  spot > VWAP AND spot > EMA10
  BEARISH:  spot < VWAP AND spot < EMA10
  NEUTRAL:  otherwise

Entry (on ATM option, nearest weekly expiry):
  non-BULLISH -> BULLISH:  Buy CE if RSI < 65 & (RSI_MA - RSI) < 1
  non-BEARISH -> BEARISH:  Buy PE if RSI < 65 & (RSI_MA - RSI) < 1

Exit (whichever first):
  1. RSI_MA - RSI >= 1 at candle close (RSI weakening)
  2. 4% option price SL intra-candle (on candle low)
  3. EOD

Indicators used (from indicators/ module via calculate_indicators):
  - EMA (spot, period=10)    -> spot_ema_10
  - RSI (option, period=14)  -> opt_rsi_14
  - RSI_MA (option, rsi=14, ma=14) -> opt_rsi_ma_14
  - VWAP (spot, from separate spot parquet) -> spot_vwap
"""

import pandas as pd
import logging
from datetime import datetime
from typing import Dict, List, Optional

from engine.trade import Trade
from engine.detailed_logger import DetailedLogger
from engine.data_loader import CONTRACT_GROUP_COLS
from indicators.vwap import VWAP as VWAPIndicator

logger = logging.getLogger(__name__)

BULLISH = "BULLISH"
BEARISH = "BEARISH"
NEUTRAL = "NEUTRAL"


# ============================================
# PREPROCESSING: Spot VWAP from separate file
# ============================================

def prepare_zone_data(
    df: pd.DataFrame, spot_path: str, start_date: str, end_date: str
) -> pd.DataFrame:
    """
    Load spot data, calculate VWAP (using existing VWAP indicator), merge into option df.

    EMA10 on spot is handled by calculate_indicators() with price_source="spot".
    VWAP needs volume from the separate spot parquet, so it's computed here.
    VWAP resets daily. Returns NaN when volume is 0.
    """
    logger.info(f"Loading spot data from {spot_path}...")
    spot = pd.read_parquet(spot_path)

    # Align timezone with option data (Asia/Kolkata)
    spot['datetime'] = pd.to_datetime(spot['datetime'], utc=True).dt.tz_convert('Asia/Kolkata')

    # Filter date range
    start = pd.to_datetime(start_date).tz_localize('Asia/Kolkata')
    end = pd.to_datetime(end_date).tz_localize('Asia/Kolkata') + pd.Timedelta(days=1)
    spot = spot[(spot['datetime'] >= start) & (spot['datetime'] < end)]
    spot = spot.sort_values('datetime').reset_index(drop=True)
    spot['date'] = spot['datetime'].dt.date
    logger.info(f"  Spot rows: {len(spot):,} | {spot['datetime'].min()} to {spot['datetime'].max()}")

    # VWAP on spot close (daily reset) — reuses existing VWAP indicator class
    vwap_ind = VWAPIndicator(name='spot_vwap')
    parts = []
    for _, day in spot.groupby('date'):
        day = day.sort_values('datetime').copy()
        day['spot_vwap'] = vwap_ind.calculate(day['close'], day['volume']).values
        parts.append(day)
    spot = pd.concat(parts, ignore_index=True)

    vwap_valid = spot['spot_vwap'].notna().sum()
    if vwap_valid == 0:
        logger.warning("  spot_vwap: ALL NaN — volume is likely 0. Fill volume to enable VWAP.")
    else:
        logger.info(f"  spot_vwap: {vwap_valid:,} non-null values")

    # Merge spot VWAP into option df by datetime
    df = df.merge(spot[['datetime', 'spot_vwap']], on='datetime', how='left')

    # _prev column per contract for crossover detection
    df['spot_vwap_prev'] = df.groupby(CONTRACT_GROUP_COLS)['spot_vwap'].shift(1)

    return df


# ============================================
# ENGINE
# ============================================

class ZoneRsiBiasEngine:
    """Backtest engine for the Zone RSI Bias strategy."""

    def __init__(self, instrument: str, df: pd.DataFrame, strategy: Dict,
                 lot_size: int, output_dir: str = "."):
        self.instrument = instrument
        self.df = df
        self.strategy = strategy
        self.lot_size = lot_size
        self.output_dir = output_dir
        self.trades: List[Trade] = []

        # Trading hours
        self.entry_time = datetime.strptime(
            strategy.get('trading_start', '09:20'), '%H:%M'
        ).time()
        self.exit_time = datetime.strptime(
            strategy.get('trading_end', '15:15'), '%H:%M'
        ).time()

        # Zone columns (from spot data)
        self.vwap_col = strategy.get('vwap_indicator', 'spot_vwap')
        self.ema_col = strategy.get('ema_indicator', 'spot_ema_10')

        # RSI columns (from option data, per contract)
        self.rsi_col = strategy.get('rsi_indicator', 'opt_rsi_14')
        self.rsi_ma_col = strategy.get('rsi_ma_indicator', 'opt_rsi_ma_14')

        # Entry thresholds
        self.rsi_entry_max = strategy.get('rsi_entry_threshold', 65)
        self.rsi_ma_diff_max = strategy.get('rsi_ma_diff_threshold', 1)

        # Exit thresholds
        self.rsi_exit_diff = strategy.get('rsi_exit_diff', 1)

        # SL on option entry price (e.g. 4% drop from entry)
        self.stop_loss_pct = strategy.get('stop_loss_pct', 4)

        logger.info(
            f"ZoneRsiBias: {instrument} | "
            f"Entry: RSI<{self.rsi_entry_max}, MA_diff<{self.rsi_ma_diff_max} | "
            f"Exit: diff>={self.rsi_exit_diff} or {self.stop_loss_pct}% SL"
        )

    # ------------------------------------------
    # HELPERS
    # ------------------------------------------

    def _get_bias(self, row) -> str:
        """Zone bias: spot vs VWAP and EMA10."""
        spot = row.get('spot')
        vwap = row.get(self.vwap_col)
        ema = row.get(self.ema_col)
        if pd.isna(spot) or pd.isna(vwap) or pd.isna(ema):
            return NEUTRAL
        if spot > vwap and spot > ema:
            return BULLISH
        if spot < vwap and spot < ema:
            return BEARISH
        return NEUTRAL

    def _check_rsi_entry(self, row) -> bool:
        """Entry: RSI < threshold AND RSI_MA not more than diff_max above RSI."""
        rsi = row.get(self.rsi_col)
        rsi_ma = row.get(self.rsi_ma_col)
        if pd.isna(rsi) or pd.isna(rsi_ma):
            return False
        return rsi < self.rsi_entry_max and (rsi_ma - rsi) < self.rsi_ma_diff_max

    def _check_rsi_exit(self, row) -> bool:
        """Exit: RSI_MA - RSI >= threshold (RSI dropped below its MA)."""
        rsi = row.get(self.rsi_col)
        rsi_ma = row.get(self.rsi_ma_col)
        if pd.isna(rsi) or pd.isna(rsi_ma):
            return False
        return (rsi_ma - rsi) >= self.rsi_exit_diff

    def _get_contract_candle(self, trade: Trade, minute_data):
        """Look up the exact contract's candle row."""
        match = minute_data[
            (minute_data['strike'] == trade.strike) &
            (minute_data['option_type'] == trade.option_type) &
            (minute_data['expiry_type'] == trade.expiry_type) &
            (minute_data['expiry_code'] == trade.expiry_code)
        ]
        return match.iloc[0] if len(match) > 0 else None

    def _sl_price(self, entry_price: float) -> float:
        """SL on option price: entry * (1 - stop_loss_pct/100)."""
        return entry_price * (1 - self.stop_loss_pct / 100)

    def _create_trade(self, t, atm_row) -> Trade:
        """Create a buy trade with direct entry at candle close."""
        trade = Trade(
            signal_time=t,
            base_price=atm_row['close'],
            option_type=atm_row['option_type'],
            strike=atm_row['strike'],
            expiry_type=atm_row['expiry_type'],
            expiry_code=atm_row['expiry_code'],
            instrument=self.instrument,
            direction='buy',
            entry_levels_config=[{"pct_from_base": 0, "capital_pct": 100}],
            lot_size=self.lot_size,
        )
        level = trade.get_next_unfilled_level()
        trade.add_entry(level, t, atm_row['close'])
        return trade

    # ------------------------------------------
    # EXIT LOGIC
    # ------------------------------------------

    def _check_exit(self, trade: Trade, minute_data, t, is_exit_time) -> List[str]:
        """
        Check exit conditions. Priority: Option SL > RSI exit > EOD.
        Returns event messages. Closes trade + appends to self.trades if exited.
        """
        events = []
        candle = self._get_contract_candle(trade, minute_data)
        entry_price = trade.get_avg_entry_price()

        # Store exit RSI/RSI_MA from the candle (useful for results CSV)
        def _store_exit_rsi(candle_row):
            rsi = candle_row.get(self.rsi_col) if candle_row is not None else None
            rsi_ma = candle_row.get(self.rsi_ma_col) if candle_row is not None else None
            trade.exit_rsi = float(rsi) if pd.notna(rsi) else 0.0
            trade.exit_rsi_ma = float(rsi_ma) if pd.notna(rsi_ma) else 0.0

        # Skip SL/RSI checks on entry candle (entry is at close, HL happened before)
        if candle is not None and trade.last_entry_time != t:
            # 1. Option price SL — intra-candle: check low (buy direction → loss on drop)
            sl = self._sl_price(entry_price)
            if candle['low'] <= sl:
                _store_exit_rsi(candle)
                trade.close_trade(t, sl, 'STOP_LOSS')
                self.trades.append(trade)
                pnl = (sl - entry_price) * self.lot_size
                events.append(
                    f"EXIT SL: low={candle['low']:.2f} <= SL={sl:.2f} | pnl=Rs {pnl:,.0f}"
                )
                return events

            # 2. RSI exit — at candle close: RSI_MA - RSI >= threshold
            if self._check_rsi_exit(candle):
                rsi = candle.get(self.rsi_col)
                rsi_ma = candle.get(self.rsi_ma_col)
                _store_exit_rsi(candle)
                trade.close_trade(t, candle['close'], 'RSI_EXIT')
                self.trades.append(trade)
                pnl = (candle['close'] - entry_price) * self.lot_size
                events.append(
                    f"EXIT RSI: RSI={rsi:.2f} MA={rsi_ma:.2f} "
                    f"diff={rsi_ma - rsi:.2f} | exit={candle['close']:.2f} pnl=Rs {pnl:,.0f}"
                )
                return events

        # 3. EOD — force close at exit time
        if is_exit_time:
            if candle is not None:
                exit_price = candle['close']
                _store_exit_rsi(candle)
            else:
                exit_price = entry_price
                trade.exit_rsi = 0.0
                trade.exit_rsi_ma = 0.0
            trade.close_trade(t, exit_price, 'EOD')
            self.trades.append(trade)
            pnl = (exit_price - entry_price) * self.lot_size
            events.append(f"EXIT EOD: {exit_price:.2f} | pnl=Rs {pnl:,.0f}")

        return events

    def _force_eod_close(self, trade: Trade, day_data, date) -> List[str]:
        """Safety net: force close if still open after minute loop."""
        events = []
        contract = day_data[
            (day_data['strike'] == trade.strike) &
            (day_data['option_type'] == trade.option_type) &
            (day_data['expiry_type'] == trade.expiry_type) &
            (day_data['expiry_code'] == trade.expiry_code) &
            (day_data['time_only'] <= self.exit_time)
        ]
        entry = trade.get_avg_entry_price()
        if len(contract) > 0:
            last = contract.iloc[-1]
            rsi = last.get(self.rsi_col)
            rsi_ma = last.get(self.rsi_ma_col)
            trade.exit_rsi = float(rsi) if pd.notna(rsi) else 0.0
            trade.exit_rsi_ma = float(rsi_ma) if pd.notna(rsi_ma) else 0.0
            trade.close_trade(last['datetime'], last['close'], 'EOD')
            pnl = (last['close'] - entry) * self.lot_size
            events.append(f"EOD safety: {last['close']:.2f} | pnl=Rs {pnl:,.0f}")
        else:
            trade.exit_rsi = 0.0
            trade.exit_rsi_ma = 0.0
            trade.close_trade(pd.Timestamp(f"{date} {self.exit_time}"), entry, 'EOD')
            events.append("EOD safety: no data, closed flat")
        self.trades.append(trade)
        return events

    # ------------------------------------------
    # MAIN LOOP
    # ------------------------------------------

    def _trade_status_str(self, trade: Optional[Trade]) -> str:
        """Short status string for a CE or PE track."""
        if trade is None:
            return "idle"
        ep = trade.get_avg_entry_price()
        sl = self._sl_price(ep)
        return f"{trade.option_type} {int(trade.strike)} entry={ep:.2f} SL={sl:.2f}"

    # ------------------------------------------
    # MAIN LOOP
    # ------------------------------------------

    def run(self) -> List[Trade]:
        """Run the zone RSI bias backtest. Returns list of completed trades."""
        logger.info("=" * 60)
        logger.info(f"ZONE RSI BIAS BACKTEST: {self.instrument}")
        logger.info("=" * 60)

        dates = self.df['date'].unique()
        dlog = DetailedLogger(self.instrument, self.strategy, self.output_dir)
        dlog.open()
        logger.info(f"Processing {len(dates)} trading days...")

        for day_num, date in enumerate(dates, 1):
            if day_num % 50 == 0:
                logger.info(f"  Day {day_num}/{len(dates)} | Trades so far: {len(self.trades)}")

            day_data = self.df[self.df['date'] == date]
            minutes = day_data['datetime'].unique()

            # Bias resets to NEUTRAL each day (VWAP resets daily too)
            bias = NEUTRAL
            # Independent tracks for CE and PE (both can be active at same time)
            active_ce: Optional[Trade] = None
            active_pe: Optional[Trade] = None
            dlog.day_header(date)

            for minute in minutes:
                t = pd.Timestamp(minute)
                t_only = t.time()
                if t_only < self.entry_time or t_only > self.exit_time:
                    continue

                is_exit_time = (t_only >= self.exit_time)
                minute_data = day_data[day_data['datetime'] == minute]
                if len(minute_data) == 0:
                    continue

                events = []
                any_row = minute_data.iloc[0]
                new_bias = self._get_bias(any_row)

                # --- ATM rows for entry checks ---
                atm = minute_data[minute_data['moneyness'] == 'ATM']
                atm_ce, atm_pe = None, None
                atm_info = {
                    'ce_strike': '--', 'ce_rsi': '--',
                    'pe_strike': '--', 'pe_rsi': '--',
                }
                for _, r in atm.iterrows():
                    rv = r.get(self.rsi_col)
                    rs = f"{rv:.2f}" if pd.notna(rv) else "--"
                    if r['option_type'] == 'CE':
                        atm_ce = r
                        atm_info['ce_strike'] = str(int(r['strike']))
                        atm_info['ce_rsi'] = rs
                    elif r['option_type'] == 'PE':
                        atm_pe = r
                        atm_info['pe_strike'] = str(int(r['strike']))
                        atm_info['pe_rsi'] = rs

                # --- ENTRY: on bias state change (CE and PE checked independently) ---
                if not is_exit_time:
                    # Non-BULLISH → BULLISH: buy CE (only if no CE trade active)
                    if (active_ce is None
                            and bias != BULLISH and new_bias == BULLISH
                            and atm_ce is not None and self._check_rsi_entry(atm_ce)):
                        active_ce = self._create_trade(t, atm_ce)
                        rsi = atm_ce.get(self.rsi_col)
                        rsi_ma = atm_ce.get(self.rsi_ma_col)
                        # Store entry context for results CSV
                        active_ce.entry_rsi = float(rsi) if pd.notna(rsi) else 0.0
                        active_ce.entry_rsi_ma = float(rsi_ma) if pd.notna(rsi_ma) else 0.0
                        active_ce.bias_change = f"{bias}\u2192{new_bias}"
                        active_ce.entry_spot = float(any_row.get('spot', 0))
                        events.append(
                            f"BUY CE: {bias}->{new_bias} | "
                            f"{int(atm_ce['strike'])} @ {atm_ce['close']:.2f} | "
                            f"RSI={rsi:.2f} MA={rsi_ma:.2f}"
                        )

                    # Non-BEARISH → BEARISH: buy PE (only if no PE trade active)
                    if (active_pe is None
                            and bias != BEARISH and new_bias == BEARISH
                            and atm_pe is not None and self._check_rsi_entry(atm_pe)):
                        active_pe = self._create_trade(t, atm_pe)
                        rsi = atm_pe.get(self.rsi_col)
                        rsi_ma = atm_pe.get(self.rsi_ma_col)
                        # Store entry context for results CSV
                        active_pe.entry_rsi = float(rsi) if pd.notna(rsi) else 0.0
                        active_pe.entry_rsi_ma = float(rsi_ma) if pd.notna(rsi_ma) else 0.0
                        active_pe.bias_change = f"{bias}\u2192{new_bias}"
                        active_pe.entry_spot = float(any_row.get('spot', 0))
                        events.append(
                            f"BUY PE: {bias}->{new_bias} | "
                            f"{int(atm_pe['strike'])} @ {atm_pe['close']:.2f} | "
                            f"RSI={rsi:.2f} MA={rsi_ma:.2f}"
                        )

                # --- EXIT: each track checked independently ---
                if active_ce is not None and active_ce.has_position():
                    exit_events = self._check_exit(active_ce, minute_data, t, is_exit_time)
                    events.extend([f"CE {e}" for e in exit_events])
                    if active_ce.status == 'CLOSED':
                        active_ce = None

                if active_pe is not None and active_pe.has_position():
                    exit_events = self._check_exit(active_pe, minute_data, t, is_exit_time)
                    events.extend([f"PE {e}" for e in exit_events])
                    if active_pe.status == 'CLOSED':
                        active_pe = None

                # Update bias for next candle
                bias = new_bias

                # --- LOG ---
                spot = any_row.get('spot', 0)
                vw = any_row.get(self.vwap_col)
                em = any_row.get(self.ema_col)
                vw_s = f"{vw:.0f}" if pd.notna(vw) else "NaN"
                em_s = f"{em:.0f}" if pd.notna(em) else "NaN"
                bias_str = f"{new_bias} spt={spot:.0f} vwap={vw_s} ema={em_s}"

                ce_str = self._trade_status_str(active_ce)
                pe_str = self._trade_status_str(active_pe)
                dlog.log_minute(
                    t_only.strftime('%H:%M'), atm_info, ce_str, pe_str, events
                )

            # --- EOD safety net for both tracks ---
            if active_ce is not None and active_ce.has_position():
                eod_events = self._force_eod_close(active_ce, day_data, date)
                for e in eod_events:
                    dlog.log_event(f"CE {e}")
                active_ce = None

            if active_pe is not None and active_pe.has_position():
                eod_events = self._force_eod_close(active_pe, day_data, date)
                for e in eod_events:
                    dlog.log_event(f"PE {e}")
                active_pe = None

        dlog.close(len(self.trades))
        logger.info(f"Done. Total trades: {len(self.trades)}")
        return self.trades


# ============================================
# RESULTS CSV BUILDER (custom columns)
# ============================================

def build_results_df(trades: List[Trade], lot_size: int) -> pd.DataFrame:
    """
    Build a clean results DataFrame for the zone RSI bias strategy.

    Extra attrs (entry_rsi, entry_rsi_ma, bias_change, entry_spot,
    exit_rsi, exit_rsi_ma) are set on Trade objects during run().
    """
    rows = []
    for i, t in enumerate(trades, 1):
        entry_price = t.get_avg_entry_price()
        sig_time = t.signal_time
        exit_t = t.exit_time

        # Date from signal_time
        date_str = sig_time.strftime('%Y-%m-%d') if hasattr(sig_time, 'strftime') else ''
        time_str = sig_time.strftime('%H:%M') if hasattr(sig_time, 'strftime') else str(sig_time)
        exit_str = exit_t.strftime('%H:%M') if hasattr(exit_t, 'strftime') else str(exit_t)

        # Duration in minutes between entry and exit
        duration = 0
        if hasattr(sig_time, 'timestamp') and hasattr(exit_t, 'timestamp'):
            duration = int((exit_t - sig_time).total_seconds() / 60)

        # SL trigger price (4% below entry)
        sl_price = round(entry_price * (1 - 4 / 100), 2)

        pnl = round((t.exit_price - entry_price) * lot_size, 2)

        rows.append({
            '#': i,
            'Date': date_str,
            'Time': time_str,
            'Type': t.option_type,
            'Strike': int(t.strike),
            'Spot': round(getattr(t, 'entry_spot', 0), 2),
            'Entry': round(entry_price, 2),
            'SL': sl_price,
            'RSI': round(getattr(t, 'entry_rsi', 0), 2),
            'RSI_MA': round(getattr(t, 'entry_rsi_ma', 0), 2),
            'Bias Change': getattr(t, 'bias_change', ''),
            'Exit Time': exit_str,
            'Exit Price': round(t.exit_price, 2),
            'Exit RSI': round(getattr(t, 'exit_rsi', 0), 2),
            'Exit RSI_MA': round(getattr(t, 'exit_rsi_ma', 0), 2),
            'Exit Reason': t.exit_reason or '',
            'Duration': duration,
            'P&L': pnl,
        })

    return pd.DataFrame(rows)
