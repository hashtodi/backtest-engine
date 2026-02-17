"""
Main backtest loop.

Strategy-agnostic backtesting engine.
Reads strategy config dict and executes:
  1. Load data + calculate indicators
  2. Day-by-day, minute-by-minute loop
  3. Signal detection -> observation
  4. Staggered entry -> fill parts
  5. SL / TP / EOD exit

CE and PE are tracked independently:
  - Can observe/trade one CE and one PE at the same time
  - Cannot have two CEs or two PEs active simultaneously
  - Both reset at end of day
"""

import pandas as pd
import logging
from datetime import datetime, time
from typing import Dict, List, Optional

from engine.trade import Trade
from engine.signals import check_signal
from engine.detailed_logger import DetailedLogger

logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    Main backtesting engine.

    Takes a strategy config dict and a pre-loaded DataFrame.
    Runs the backtest and returns a list of Trade objects.
    """

    def __init__(self, instrument: str, df: pd.DataFrame, strategy: Dict, lot_size: int,
                 output_dir: str = "."):
        """
        Args:
            instrument: "NIFTY" or "SENSEX"
            df: DataFrame from data_loader.load_data() + calculate_indicators()
            strategy: strategy config dict (see strategies/rsi_70_sell.py)
            lot_size: contract lot size for this instrument
            output_dir: directory for output files (detailed logs)
        """
        self.instrument = instrument
        self.df = df
        self.strategy = strategy
        self.lot_size = lot_size
        self.output_dir = output_dir
        self.trades: List[Trade] = []

        # Parse trading hours from strategy config
        self.entry_time = datetime.strptime(
            strategy.get('trading_start', '09:30'), '%H:%M'
        ).time()
        self.exit_time = datetime.strptime(
            strategy.get('trading_end', '14:30'), '%H:%M'
        ).time()

        # SL / TP from strategy config
        self.stop_loss_pct = strategy.get('stop_loss_pct', 20)
        self.target_pct = strategy.get('target_pct', 10)

        # Direction: "sell" or "buy"
        self.direction = strategy.get('direction', 'sell')

        # Signal conditions from strategy
        self.signal_conditions = strategy.get('signal_conditions', [])
        self.signal_logic = strategy.get('signal_logic', 'AND')

        # Entry levels from strategy
        self.entry_levels_config = strategy.get('entry_levels', [])

        # Max trades per day (None = unlimited)
        self.max_trades_per_day = strategy.get('max_trades_per_day', None)

        logger.info(f"Initialized backtest for {instrument} | "
                     f"direction={self.direction} | "
                     f"SL={self.stop_loss_pct}% | TP={self.target_pct}% | "
                     f"max_trades/day={self.max_trades_per_day or 'unlimited'}")

    # ------------------------------------------
    # CONTRACT CANDLE LOOKUP
    # ------------------------------------------
    def _get_contract_candle(self, trade: Trade, minute_data):
        """Look up the specific contract's candle (no moneyness filter)."""
        match = minute_data[
            (minute_data['strike'] == trade.strike) &
            (minute_data['option_type'] == trade.option_type) &
            (minute_data['expiry_type'] == trade.expiry_type) &
            (minute_data['expiry_code'] == trade.expiry_code)
        ]
        return match.iloc[0] if len(match) > 0 else None

    # ------------------------------------------
    # STAGGERED ENTRY CHECK
    # ------------------------------------------
    def _check_staggered_entry(self, trade: Trade, minute_data, t) -> List[str]:
        """
        Check if any staggered entry levels are hit.

        For sell: price must rise to hit level (candle high >= target).
        For buy: price must drop to hit level (candle low <= target).

        Levels must fill in order (level 1 before level 2, etc.)
        Returns list of event messages for logging.
        """
        events = []
        candle = self._get_contract_candle(trade, minute_data)
        if candle is None:
            return events

        # Get next unfilled level
        next_level = trade.get_next_unfilled_level()
        while next_level is not None:
            if self.direction == 'sell':
                # Sell: enter when candle high reaches the target price
                if candle['high'] >= next_level.target_price:
                    trade.add_entry(next_level, t, next_level.target_price)
                    events.append(
                        f"ENTRY Part{next_level.level_num}: "
                        f"high={candle['high']:.2f} >= "
                        f"L{next_level.level_num}={next_level.target_price:.2f} | "
                        f"filled @ {next_level.target_price:.2f}"
                    )
                    # Check if another level can also be filled this same candle
                    next_level = trade.get_next_unfilled_level()
                else:
                    break
            else:
                # Buy: enter when candle low drops to the target price
                if candle['low'] <= next_level.target_price:
                    trade.add_entry(next_level, t, next_level.target_price)
                    events.append(
                        f"ENTRY Part{next_level.level_num}: "
                        f"low={candle['low']:.2f} <= "
                        f"L{next_level.level_num}={next_level.target_price:.2f} | "
                        f"filled @ {next_level.target_price:.2f}"
                    )
                    next_level = trade.get_next_unfilled_level()
                else:
                    break

        return events

    # ------------------------------------------
    # EXIT CHECK (SL / TP / EOD)
    # ------------------------------------------
    def _check_exit(self, trade: Trade, minute_data, day_data, t, is_exit_time: bool):
        """
        Check SL/TP/EOD exit for this trade.

        SL and TP are exact fills (no slippage).
        For sell direction:
          - SL: price rises too much (candle high >= sl_price)
          - TP: price drops enough (candle low <= tp_price)
        For buy direction:
          - SL: price drops too much (candle low <= sl_price)
          - TP: price rises enough (candle high >= tp_price)

        Returns (closed: bool, event_msg: str or None).
        """
        if not trade.has_position():
            return False, None

        candle = self._get_contract_candle(trade, minute_data)

        if candle is None:
            # No data at this minute. If exit time, find last available candle.
            if is_exit_time:
                last_candle_df = day_data[
                    (day_data['strike'] == trade.strike) &
                    (day_data['option_type'] == trade.option_type) &
                    (day_data['expiry_type'] == trade.expiry_type) &
                    (day_data['expiry_code'] == trade.expiry_code) &
                    (day_data['time_only'] <= self.exit_time)
                ]
                if len(last_candle_df) > 0:
                    lc = last_candle_df.iloc[-1]
                    trade.close_trade(t, lc['close'], 'EOD')
                    msg = (f"EXIT EOD (no data at exit time, "
                           f"used last candle close={lc['close']:.2f})")
                else:
                    avg = trade.get_avg_entry_price()
                    trade.close_trade(t, avg, 'EOD')
                    msg = f"EXIT EOD (no data, closed flat at avg_entry={avg:.2f})"
                self.trades.append(trade)
                return True, msg
            return False, None

        avg_entry = trade.get_avg_entry_price()
        if avg_entry is None:
            return False, None

        exit_reason = None
        exit_price = None
        msg = None

        if self.direction == 'sell':
            # Sell direction: SL when price rises, TP when price drops
            sl_price = avg_entry * (1 + self.stop_loss_pct / 100)
            tp_price = avg_entry * (1 - self.target_pct / 100)

            if candle['high'] >= sl_price:
                exit_reason = 'STOP_LOSS'
                exit_price = sl_price
                msg = (f"EXIT STOP_LOSS: high={candle['high']:.2f} >= "
                       f"SL={sl_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {sl_price:.2f} | pnl=-{self.stop_loss_pct}%")
            elif candle['low'] <= tp_price:
                exit_reason = 'TARGET'
                exit_price = tp_price
                msg = (f"EXIT TARGET: low={candle['low']:.2f} <= "
                       f"TP={tp_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {tp_price:.2f} | pnl=+{self.target_pct}%")
        else:
            # Buy direction: SL when price drops, TP when price rises
            sl_price = avg_entry * (1 - self.stop_loss_pct / 100)
            tp_price = avg_entry * (1 + self.target_pct / 100)

            if candle['low'] <= sl_price:
                exit_reason = 'STOP_LOSS'
                exit_price = sl_price
                msg = (f"EXIT STOP_LOSS: low={candle['low']:.2f} <= "
                       f"SL={sl_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {sl_price:.2f} | pnl=-{self.stop_loss_pct}%")
            elif candle['high'] >= tp_price:
                exit_reason = 'TARGET'
                exit_price = tp_price
                msg = (f"EXIT TARGET: high={candle['high']:.2f} >= "
                       f"TP={tp_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {tp_price:.2f} | pnl=+{self.target_pct}%")

        # EOD: at exit time, force close at candle close
        if exit_reason is None and is_exit_time:
            exit_reason = 'EOD'
            exit_price = candle['close']
            pnl = avg_entry - exit_price if self.direction == 'sell' else exit_price - avg_entry
            pnl_pct = (pnl / avg_entry) * 100
            msg = (f"EXIT EOD: close={candle['close']:.2f} | "
                   f"avg={avg_entry:.2f} | pnl={pnl_pct:+.2f}%")

        if exit_reason:
            trade.close_trade(t, exit_price, exit_reason)
            self.trades.append(trade)
            return True, msg

        return False, None

    # ------------------------------------------
    # EOD SAFETY NET
    # ------------------------------------------
    def _eod_close_trade(self, trade: Trade, day_data, date) -> str:
        """Safety net: close a trade at EOD if still open. Returns event message."""
        if trade.has_position():
            contract_data = day_data[
                (day_data['strike'] == trade.strike) &
                (day_data['option_type'] == trade.option_type) &
                (day_data['expiry_type'] == trade.expiry_type) &
                (day_data['expiry_code'] == trade.expiry_code) &
                (day_data['time_only'] <= self.exit_time)
            ]
            if len(contract_data) > 0:
                last_row = contract_data.iloc[-1]
                trade.close_trade(last_row['datetime'], last_row['close'], 'EOD')
                avg = trade.get_avg_entry_price()
                if self.direction == 'sell':
                    pnl_pct = ((avg - last_row['close']) / avg) * 100 if avg else 0
                else:
                    pnl_pct = ((last_row['close'] - avg) / avg) * 100 if avg else 0
                msg = (f"EXIT EOD (safety net): close={last_row['close']:.2f} | "
                       f"pnl={pnl_pct:+.2f}%")
            else:
                avg = trade.get_avg_entry_price()
                trade.close_trade(
                    pd.Timestamp(f"{date} {self.exit_time}"),
                    avg, 'EOD'
                )
                msg = "EXIT EOD (safety net, no data, closed flat)"
            self.trades.append(trade)
            return msg
        # No position was taken -- just discard the observation
        return "EOD: observation expired (no entry taken)"

    # ------------------------------------------
    # TRACK STATUS (for detailed log)
    # ------------------------------------------
    def _get_track_status(self, trade: Optional[Trade], minute_data) -> str:
        """Get a short status string for a CE or PE track."""
        if trade is None:
            return "idle"

        opt = trade.option_type
        strike = int(trade.strike)

        candle = self._get_contract_candle(trade, minute_data)
        price_str = (
            f"close={candle['close']:.2f} high={candle['high']:.2f} "
            f"low={candle['low']:.2f}"
        ) if candle is not None else "no data"

        if trade.status == 'WAITING_ENTRY':
            next_lvl = trade.get_next_unfilled_level()
            lvl_str = f"L{next_lvl.level_num}={next_lvl.target_price:.2f}" if next_lvl else ""
            return (f"observing {strike} {opt} | {price_str} | "
                    f"waiting {lvl_str}")

        elif trade.status in ('PARTIAL_POSITION', 'FULL_POSITION'):
            avg = trade.get_avg_entry_price()
            n_filled = len(trade.parts)
            n_total = trade.num_levels
            if self.direction == 'sell':
                sl = avg * (1 + self.stop_loss_pct / 100)
                tp = avg * (1 - self.target_pct / 100)
            else:
                sl = avg * (1 - self.stop_loss_pct / 100)
                tp = avg * (1 + self.target_pct / 100)
            return (f"in position {strike} {opt} ({n_filled}/{n_total}) | "
                    f"{price_str} | avg={avg:.2f} SL={sl:.2f} TP={tp:.2f}")

        return trade.status

    # ------------------------------------------
    # MAIN LOOP
    # ------------------------------------------
    def run(self) -> List[Trade]:
        """
        Run the full backtest.

        Returns list of completed Trade objects.
        """
        logger.info("=" * 60)
        logger.info(f"BACKTEST: {self.instrument}")
        logger.info("=" * 60)

        # Independent tracks for CE and PE
        active_ce: Optional[Trade] = None
        active_pe: Optional[Trade] = None
        dates = self.df['date'].unique()

        # Open detailed log
        dlog = DetailedLogger(self.instrument, self.strategy, self.output_dir)
        dlog.open()

        logger.info(f"Processing {len(dates)} trading days...")

        for day_num, date in enumerate(dates, 1):
            if day_num % 50 == 0:
                logger.info(
                    f"Day {day_num}/{len(dates)} | "
                    f"Trades so far: {len(self.trades)}"
                )

            day_data = self.df[self.df['date'] == date]
            minutes = day_data['datetime'].unique()

            # Track how many new trades (observations) were opened today
            day_trade_count = 0

            # Day header in detailed log
            dlog.day_header(date)

            for minute in minutes:
                t = pd.Timestamp(minute)
                t_only = t.time()

                # Skip if outside trading hours
                if t_only < self.entry_time:
                    continue
                if t_only > self.exit_time:
                    continue

                is_exit_time = (t_only >= self.exit_time)
                minute_data = day_data[day_data['datetime'] == minute]

                # Collect events for this minute
                events = []

                # --- ATM RSI info for logging ---
                atm_data = minute_data[minute_data['moneyness'] == 'ATM']
                atm_info = {
                    'ce_strike': '--', 'ce_rsi': '--',
                    'pe_strike': '--', 'pe_rsi': '--',
                }
                for _, row in atm_data.iterrows():
                    # Get the first indicator name for RSI display
                    # Use the first indicator's column name
                    rsi_col = None
                    for ind_cfg in self.strategy.get('indicators', []):
                        if ind_cfg['type'] == 'RSI':
                            rsi_col = ind_cfg['name']
                            break

                    rsi_val = "--"
                    if rsi_col and rsi_col in row and pd.notna(row[rsi_col]):
                        rsi_val = f"{row[rsi_col]:.2f}"

                    if row['option_type'] == 'CE':
                        atm_info['ce_rsi'] = rsi_val
                        atm_info['ce_strike'] = str(int(row['strike']))
                    elif row['option_type'] == 'PE':
                        atm_info['pe_rsi'] = rsi_val
                        atm_info['pe_strike'] = str(int(row['strike']))

                # --- SIGNAL DETECTION (ATM only, not at exit time) ---
                # Skip new signals if daily trade limit reached
                day_limit_hit = (
                    self.max_trades_per_day is not None
                    and day_trade_count >= self.max_trades_per_day
                )

                if not is_exit_time and not day_limit_hit:
                    for _, row in atm_data.iterrows():
                        # Check if signal conditions are met
                        fired, reason = check_signal(
                            row,
                            self.signal_conditions,
                            self.signal_logic,
                        )
                        if not fired:
                            continue

                        opt_type = row['option_type']

                        if opt_type == 'CE' and active_ce is None:
                            active_ce = Trade(
                                signal_time=t,
                                base_price=row['close'],
                                option_type='CE',
                                strike=row['strike'],
                                expiry_type=row['expiry_type'],
                                expiry_code=row['expiry_code'],
                                instrument=self.instrument,
                                direction=self.direction,
                                entry_levels_config=self.entry_levels_config,
                                lot_size=self.lot_size,
                            )
                            day_trade_count += 1
                            levels_str = " ".join(
                                f"L{lvl.level_num}={lvl.target_price:.2f}"
                                for lvl in active_ce.entry_levels
                            )
                            events.append(
                                f"CE SIGNAL: {reason} on "
                                f"{int(row['strike'])} CE | "
                                f"base={row['close']:.2f} | {levels_str}"
                            )

                        elif opt_type == 'PE' and active_pe is None:
                            active_pe = Trade(
                                signal_time=t,
                                base_price=row['close'],
                                option_type='PE',
                                strike=row['strike'],
                                expiry_type=row['expiry_type'],
                                expiry_code=row['expiry_code'],
                                instrument=self.instrument,
                                direction=self.direction,
                                entry_levels_config=self.entry_levels_config,
                                lot_size=self.lot_size,
                            )
                            day_trade_count += 1
                            levels_str = " ".join(
                                f"L{lvl.level_num}={lvl.target_price:.2f}"
                                for lvl in active_pe.entry_levels
                            )
                            events.append(
                                f"PE SIGNAL: {reason} on "
                                f"{int(row['strike'])} PE | "
                                f"base={row['close']:.2f} | {levels_str}"
                            )

                # --- STAGGERED ENTRY (not at exit time) ---
                if not is_exit_time:
                    if active_ce and active_ce.status in (
                        'WAITING_ENTRY', 'PARTIAL_POSITION'
                    ):
                        entry_events = self._check_staggered_entry(
                            active_ce, minute_data, t
                        )
                        events.extend([f"CE {e}" for e in entry_events])

                    if active_pe and active_pe.status in (
                        'WAITING_ENTRY', 'PARTIAL_POSITION'
                    ):
                        entry_events = self._check_staggered_entry(
                            active_pe, minute_data, t
                        )
                        events.extend([f"PE {e}" for e in entry_events])

                # --- EXIT MANAGEMENT ---
                if active_ce:
                    closed, exit_msg = self._check_exit(
                        active_ce, minute_data, day_data, t, is_exit_time
                    )
                    if closed:
                        events.append(f"CE {exit_msg}")
                        active_ce = None

                if active_pe:
                    closed, exit_msg = self._check_exit(
                        active_pe, minute_data, day_data, t, is_exit_time
                    )
                    if closed:
                        events.append(f"PE {exit_msg}")
                        active_pe = None

                # --- WRITE LOG LINE ---
                time_str = t_only.strftime('%H:%M')
                ce_status = self._get_track_status(active_ce, minute_data)
                pe_status = self._get_track_status(active_pe, minute_data)
                dlog.log_minute(time_str, atm_info, ce_status, pe_status, events)

            # --- END OF DAY: reset both tracks ---
            if active_ce:
                msg = self._eod_close_trade(active_ce, day_data, date)
                dlog.log_event(f"CE {msg}")
                active_ce = None
            if active_pe:
                msg = self._eod_close_trade(active_pe, day_data, date)
                dlog.log_event(f"PE {msg}")
                active_pe = None

        dlog.close(len(self.trades))
        logger.info(f"Backtest done. Total trades: {len(self.trades)}")

        return self.trades
