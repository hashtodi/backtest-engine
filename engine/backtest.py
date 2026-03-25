"""
Main backtest loop.

Strategy-agnostic backtesting engine.
Reads strategy config dict and executes:
  1. Load data + calculate indicators
  2. Day-by-day, minute-by-minute loop
  3. Signal detection -> observation
  4. Staggered entry -> fill parts
  5. SL / TP / EOD exit

Supports two trade modes:
  - "single_leg" (default): CE and PE tracked independently.
  - "straddle": single track, both ATM CE+PE sold/bought together.
    Signal fires on combined straddle price, SL/TP on combined price.
"""

import math
import pandas as pd
import logging
from datetime import datetime, time
from typing import Dict, List, Optional, Union

from engine.trade import Trade, StraddleTrade, parse_entry_config, parse_exit_config
from engine.signals import check_signal
from engine.detailed_logger import DetailedLogger
from engine.expiry_calendar import pick_expiry_code

logger = logging.getLogger(__name__)


def resolve_exit_levels(avg_entry, direction, exit_config, indicator_row):
    """
    Compute SL and TP price levels for this minute.

    Args:
        avg_entry: weighted average entry price
        direction: "sell" or "buy"
        exit_config: normalized dict from parse_exit_config()
        indicator_row: dict-like row with indicator values (or {} if N/A)

    Returns:
        (sl_level, tp_level) — either can be None if indicator is NaN/wrong-side
    """
    sl_cfg = exit_config["stop_loss"]
    tp_cfg = exit_config["target"]

    if sl_cfg["source"] == "ratio":
        tp_level = _resolve_one_side(tp_cfg, avg_entry, direction, "target", indicator_row)
        if tp_level is None:
            return None, None
        anchor_distance = abs(avg_entry - tp_level)
        multiplier = sl_cfg.get("multiplier", 1.0)
        derived_distance = anchor_distance * multiplier
        if direction == "sell":
            sl_level = avg_entry + derived_distance
        else:
            sl_level = avg_entry - derived_distance
        return sl_level, tp_level

    elif tp_cfg["source"] == "ratio":
        sl_level = _resolve_one_side(sl_cfg, avg_entry, direction, "stop_loss", indicator_row)
        if sl_level is None:
            return None, None
        anchor_distance = abs(avg_entry - sl_level)
        multiplier = tp_cfg.get("multiplier", 1.0)
        derived_distance = anchor_distance * multiplier
        if direction == "sell":
            tp_level = avg_entry - derived_distance
        else:
            tp_level = avg_entry + derived_distance
        return sl_level, tp_level

    else:
        sl_level = _resolve_one_side(sl_cfg, avg_entry, direction, "stop_loss", indicator_row)
        tp_level = _resolve_one_side(tp_cfg, avg_entry, direction, "target", indicator_row)
        return sl_level, tp_level


def _resolve_one_side(side_cfg, avg_entry, direction, side_type, indicator_row):
    """
    Resolve one exit side (SL or TP) to a price level.

    Returns float price level, or None if unavailable.
    """
    source = side_cfg["source"]

    if source == "percentage":
        value = side_cfg.get("value", 20 if side_type == "stop_loss" else 10)
        if side_type == "stop_loss":
            if direction == "sell":
                return avg_entry * (1 + value / 100)
            else:
                return avg_entry * (1 - value / 100)
        else:
            if direction == "sell":
                return avg_entry * (1 - value / 100)
            else:
                return avg_entry * (1 + value / 100)

    elif source == "indicator":
        ind_name = side_cfg.get("indicator", "")
        val = indicator_row.get(ind_name) if ind_name else None
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return None

        # Wrong-side guard
        if side_type == "stop_loss":
            if direction == "sell" and val <= avg_entry:
                return None
            if direction == "buy" and val >= avg_entry:
                return None
        else:
            if direction == "sell" and val >= avg_entry:
                return None
            if direction == "buy" and val <= avg_entry:
                return None

        return val

    return None


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
        self.trades: List[Union[Trade, StraddleTrade]] = []

        # Parse trading hours from strategy config
        self.entry_time = datetime.strptime(
            strategy.get('trading_start', '09:30'), '%H:%M'
        ).time()
        self.exit_time = datetime.strptime(
            strategy.get('trading_end', '14:30'), '%H:%M'
        ).time()

        # Exit config: percentage, indicator, or ratio
        self.exit_config = parse_exit_config(strategy)

        # Keep flat values for straddle mode (which still uses percentage only)
        self.stop_loss_pct = strategy.get('stop_loss_pct', 20)
        self.target_pct = strategy.get('target_pct', 10)

        # Direction: "sell" or "buy"
        self.direction = strategy.get('direction', 'sell')

        # Trade mode: "single_leg" (default) or "straddle"
        self.trade_mode = strategy.get('trade_mode', 'single_leg')

        # Expiry mode: "weekly" (default) or "monthly"
        self.expiry_mode = strategy.get('expiry_mode', 'weekly')

        # Signal conditions from strategy
        self.signal_conditions = strategy.get('signal_conditions', [])
        self.signal_logic = strategy.get('signal_logic', 'AND')

        # Entry config: parsed from strategy["entry"] dict
        self.entry_levels_config, self.entry_indicator = parse_entry_config(strategy)

        # Max trades per day (None = unlimited)
        self.max_trades_per_day = strategy.get('max_trades_per_day', None)

        # Max SL hits per day (None = unlimited). Stop new entries for the day.
        self.max_sl_per_day = strategy.get('max_sl_per_day', None)

        # Build SL/TP display string for logging
        sl_src = self.exit_config["stop_loss"]["source"]
        tp_src = self.exit_config["target"]["source"]
        if sl_src == "percentage":
            sl_display = f"{self.exit_config['stop_loss']['value']}%"
        elif sl_src == "indicator":
            sl_display = f"indicator({self.exit_config['stop_loss']['indicator']})"
        else:
            sl_display = f"ratio({self.exit_config['stop_loss']['multiplier']}x)"
        if tp_src == "percentage":
            tp_display = f"{self.exit_config['target']['value']}%"
        elif tp_src == "indicator":
            tp_display = f"indicator({self.exit_config['target']['indicator']})"
        else:
            tp_display = f"ratio({self.exit_config['target']['multiplier']}x)"

        logger.info(f"Initialized backtest for {instrument} | "
                     f"direction={self.direction} | "
                     f"trade_mode={self.trade_mode} | "
                     f"expiry_mode={self.expiry_mode} | "
                     f"SL={sl_display} | TP={tp_display} | "
                     f"max_trades/day={self.max_trades_per_day or 'unlimited'} | "
                     f"max_sl/day={self.max_sl_per_day or 'unlimited'}")

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
    # INDICATOR LEVEL ENTRY CHECK
    # ------------------------------------------
    def _check_indicator_entry(self, trade: Trade, minute_data, t) -> List[str]:
        """
        Check if price touches the dynamic indicator level for entry.

        The indicator value is the limit order price. If the candle's
        low-high range contains the indicator value, the price must have
        passed through that level. Fill at the indicator value.

        Returns list of event messages for logging.
        """
        events = []
        if trade.status != 'WAITING_ENTRY':
            return events

        candle = self._get_contract_candle(trade, minute_data)
        if candle is None:
            return events

        # Read the indicator value from this candle's row
        ind_val = candle.get(self.entry_indicator)
        if ind_val is None or pd.isna(ind_val):
            return events

        # Check: did price touch the indicator level within this candle?
        if candle['low'] <= ind_val <= candle['high']:
            # Update the entry level target to the current indicator value
            trade.update_entry_target(ind_val)

            # Fill the single entry level at the indicator price
            next_level = trade.get_next_unfilled_level()
            if next_level is not None:
                trade.add_entry(next_level, t, ind_val)
                events.append(
                    f"ENTRY (indicator level): "
                    f"{self.entry_indicator}={ind_val:.2f} | "
                    f"range=[{candle['low']:.2f}, {candle['high']:.2f}] | "
                    f"filled @ {ind_val:.2f}"
                )

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

        # Skip SL/TP on the candle where entry just happened.
        # Entry is at close (last price of candle), so the candle's
        # high/low occurred before entry. Only EOD exit is allowed.
        if not is_exit_time and trade.last_entry_time == t:
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

        # Resolve dynamic SL/TP levels
        sl_price, tp_price = resolve_exit_levels(
            avg_entry, self.direction, self.exit_config, candle
        )

        if self.direction == 'sell':
            if sl_price is not None and candle['high'] >= sl_price:
                exit_reason = 'STOP_LOSS'
                exit_price = sl_price
                pnl_pct = -((sl_price - avg_entry) / avg_entry) * 100
                msg = (f"EXIT STOP_LOSS: high={candle['high']:.2f} >= "
                       f"SL={sl_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {sl_price:.2f} | pnl={pnl_pct:+.2f}%")
            elif tp_price is not None and candle['low'] <= tp_price:
                exit_reason = 'TARGET'
                exit_price = tp_price
                pnl_pct = ((avg_entry - tp_price) / avg_entry) * 100
                msg = (f"EXIT TARGET: low={candle['low']:.2f} <= "
                       f"TP={tp_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {tp_price:.2f} | pnl=+{pnl_pct:.2f}%")
        else:
            if sl_price is not None and candle['low'] <= sl_price:
                exit_reason = 'STOP_LOSS'
                exit_price = sl_price
                pnl_pct = -((avg_entry - sl_price) / avg_entry) * 100
                msg = (f"EXIT STOP_LOSS: low={candle['low']:.2f} <= "
                       f"SL={sl_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {sl_price:.2f} | pnl={pnl_pct:+.2f}%")
            elif tp_price is not None and candle['high'] >= tp_price:
                exit_reason = 'TARGET'
                exit_price = tp_price
                pnl_pct = ((tp_price - avg_entry) / avg_entry) * 100
                msg = (f"EXIT TARGET: high={candle['high']:.2f} >= "
                       f"TP={tp_price:.2f} | avg={avg_entry:.2f} | "
                       f"exit @ {tp_price:.2f} | pnl=+{pnl_pct:.2f}%")

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
            if self.entry_indicator and candle is not None:
                ind_val = candle.get(self.entry_indicator)
                lvl_str = (f"limit={ind_val:.2f}" if ind_val is not None
                           and pd.notna(ind_val) else "limit=NaN")
            else:
                next_lvl = trade.get_next_unfilled_level()
                lvl_str = f"L{next_lvl.level_num}={next_lvl.target_price:.2f}" if next_lvl else ""
            return (f"observing {strike} {opt} | {price_str} | "
                    f"waiting {lvl_str}")

        elif trade.status in ('PARTIAL_POSITION', 'FULL_POSITION'):
            avg = trade.get_avg_entry_price()
            n_filled = len(trade.parts)
            n_total = trade.num_levels
            sl, tp = resolve_exit_levels(
                avg, self.direction, self.exit_config, candle if candle is not None else {}
            )
            sl_str = f"{sl:.2f}" if sl is not None else "N/A"
            tp_str = f"{tp:.2f}" if tp is not None else "N/A"
            return (f"in position {strike} {opt} ({n_filled}/{n_total}) | "
                    f"{price_str} | avg={avg:.2f} SL={sl_str} TP={tp_str}")

        return trade.status

    # ------------------------------------------
    # STRADDLE: get candle for a specific contract
    # ------------------------------------------
    def _get_candle_by_contract(self, minute_data, strike, option_type, expiry_type, expiry_code):
        """Look up a candle by explicit contract fields (for straddle legs)."""
        match = minute_data[
            (minute_data['strike'] == strike) &
            (minute_data['option_type'] == option_type) &
            (minute_data['expiry_type'] == expiry_type) &
            (minute_data['expiry_code'] == expiry_code)
        ]
        return match.iloc[0] if len(match) > 0 else None

    # ------------------------------------------
    # STRADDLE: SL/TP exit check on combined price
    # ------------------------------------------
    def _check_straddle_exit(self, trade: StraddleTrade, minute_data, day_data,
                             t, is_exit_time: bool):
        """
        Check SL/TP/EOD exit for a straddle trade.

        Uses close-to-close comparison on combined straddle price
        (CE close + PE close) since intra-candle straddle high/low
        cannot be reconstructed from individual leg OHLC.

        Returns (closed: bool, event_msg: str or None).
        """
        # Skip SL/TP on entry candle
        if not is_exit_time and trade.last_entry_time == t:
            return False, None

        ce_candle = self._get_candle_by_contract(
            minute_data, trade.ce_strike, 'CE', trade.expiry_type, trade.expiry_code)
        pe_candle = self._get_candle_by_contract(
            minute_data, trade.pe_strike, 'PE', trade.expiry_type, trade.expiry_code)

        if ce_candle is None or pe_candle is None:
            if is_exit_time:
                # Try last available candles for EOD
                ce_exit, pe_exit = self._find_last_straddle_candles(
                    trade, day_data)
                if ce_exit is not None and pe_exit is not None:
                    trade.close_trade(t, ce_exit, pe_exit, 'EOD')
                else:
                    # No data at all — close flat
                    trade.close_trade(
                        t, trade.ce_entry_price, trade.pe_entry_price, 'EOD')
                self.trades.append(trade)
                msg = (f"EXIT EOD: CE={trade.ce_exit_price:.2f} "
                       f"PE={trade.pe_exit_price:.2f}")
                return True, msg
            return False, None

        ce_close = ce_candle['close']
        pe_close = pe_candle['close']
        current_straddle = ce_close + pe_close
        entry = trade.straddle_entry

        exit_reason = None
        msg = None

        if self.direction == 'sell':
            # Sell: SL when straddle rises, TP when straddle drops
            sl_price = entry * (1 + self.stop_loss_pct / 100)
            tp_price = entry * (1 - self.target_pct / 100)

            if current_straddle >= sl_price:
                exit_reason = 'STOP_LOSS'
                msg = (f"EXIT STOP_LOSS: straddle={current_straddle:.2f} >= "
                       f"SL={sl_price:.2f} | entry={entry:.2f} | "
                       f"pnl=-{self.stop_loss_pct}%")
            elif current_straddle <= tp_price:
                exit_reason = 'TARGET'
                msg = (f"EXIT TARGET: straddle={current_straddle:.2f} <= "
                       f"TP={tp_price:.2f} | entry={entry:.2f} | "
                       f"pnl=+{self.target_pct}%")
        else:
            # Buy: SL when straddle drops, TP when straddle rises
            sl_price = entry * (1 - self.stop_loss_pct / 100)
            tp_price = entry * (1 + self.target_pct / 100)

            if current_straddle <= sl_price:
                exit_reason = 'STOP_LOSS'
                msg = (f"EXIT STOP_LOSS: straddle={current_straddle:.2f} <= "
                       f"SL={sl_price:.2f} | entry={entry:.2f} | "
                       f"pnl=-{self.stop_loss_pct}%")
            elif current_straddle >= tp_price:
                exit_reason = 'TARGET'
                msg = (f"EXIT TARGET: straddle={current_straddle:.2f} >= "
                       f"TP={tp_price:.2f} | entry={entry:.2f} | "
                       f"pnl=+{self.target_pct}%")

        # EOD: force close at current closes
        if exit_reason is None and is_exit_time:
            exit_reason = 'EOD'
            pnl = entry - current_straddle if self.direction == 'sell' else current_straddle - entry
            pnl_pct = (pnl / entry) * 100
            msg = (f"EXIT EOD: straddle={current_straddle:.2f} | "
                   f"entry={entry:.2f} | pnl={pnl_pct:+.2f}%")

        if exit_reason:
            trade.close_trade(t, ce_close, pe_close, exit_reason)
            self.trades.append(trade)
            return True, msg

        return False, None

    def _find_last_straddle_candles(self, trade: StraddleTrade, day_data):
        """Find last available close prices for both CE and PE legs within trading hours."""
        ce_data = day_data[
            (day_data['strike'] == trade.ce_strike) &
            (day_data['option_type'] == 'CE') &
            (day_data['expiry_type'] == trade.expiry_type) &
            (day_data['expiry_code'] == trade.expiry_code) &
            (day_data['time_only'] <= self.exit_time)
        ]
        pe_data = day_data[
            (day_data['strike'] == trade.pe_strike) &
            (day_data['option_type'] == 'PE') &
            (day_data['expiry_type'] == trade.expiry_type) &
            (day_data['expiry_code'] == trade.expiry_code) &
            (day_data['time_only'] <= self.exit_time)
        ]
        ce_exit = ce_data.iloc[-1]['close'] if len(ce_data) > 0 else None
        pe_exit = pe_data.iloc[-1]['close'] if len(pe_data) > 0 else None
        return ce_exit, pe_exit

    # ------------------------------------------
    # STRADDLE: EOD safety net
    # ------------------------------------------
    def _eod_close_straddle(self, trade: StraddleTrade, day_data, date) -> str:
        """Safety net: close straddle at EOD if still open."""
        if trade.has_position():
            ce_exit, pe_exit = self._find_last_straddle_candles(trade, day_data)
            if ce_exit is not None and pe_exit is not None:
                trade.close_trade(
                    pd.Timestamp(f"{date} {self.exit_time}"),
                    ce_exit, pe_exit, 'EOD')
                msg = (f"EXIT EOD (safety net): straddle="
                       f"{ce_exit + pe_exit:.2f} | pnl={trade.total_pnl_pct:+.2f}%")
            else:
                trade.close_trade(
                    pd.Timestamp(f"{date} {self.exit_time}"),
                    trade.ce_entry_price, trade.pe_entry_price, 'EOD')
                msg = "EXIT EOD (safety net, no data, closed flat)"
            self.trades.append(trade)
            return msg
        return "EOD: no position"

    # ------------------------------------------
    # STRADDLE: status string for logging
    # ------------------------------------------
    def _get_straddle_status(self, trade: Optional[StraddleTrade], minute_data) -> str:
        """Get a short status string for the straddle track."""
        if trade is None:
            return "idle"

        ce_candle = self._get_candle_by_contract(
            minute_data, trade.ce_strike, 'CE', trade.expiry_type, trade.expiry_code)
        pe_candle = self._get_candle_by_contract(
            minute_data, trade.pe_strike, 'PE', trade.expiry_type, trade.expiry_code)

        if ce_candle is not None and pe_candle is not None:
            current = ce_candle['close'] + pe_candle['close']
            if self.direction == 'sell':
                sl = trade.straddle_entry * (1 + self.stop_loss_pct / 100)
                tp = trade.straddle_entry * (1 - self.target_pct / 100)
            else:
                sl = trade.straddle_entry * (1 - self.stop_loss_pct / 100)
                tp = trade.straddle_entry * (1 + self.target_pct / 100)
            return (f"straddle {int(trade.ce_strike)} | "
                    f"curr={current:.2f} entry={trade.straddle_entry:.2f} "
                    f"SL={sl:.2f} TP={tp:.2f}")
        return f"straddle {int(trade.ce_strike)} | no data"

    # ------------------------------------------
    # MAIN LOOP
    # ------------------------------------------
    def run(self) -> List[Union[Trade, StraddleTrade]]:
        """
        Run the full backtest.

        Dispatches to single-leg or straddle mode based on trade_mode config.
        Returns list of completed Trade or StraddleTrade objects.
        """
        if self.trade_mode == 'straddle':
            return self._run_straddle()
        return self._run_single_leg()

    # ------------------------------------------
    # STRADDLE MODE MAIN LOOP
    # ------------------------------------------
    def _run_straddle(self) -> List[StraddleTrade]:
        """
        Run straddle backtest.

        Single track: sell/buy both ATM CE and PE together.
        Signal fires on straddle indicator columns (e.g., straddle_bb_upper).
        SL/TP tracked on combined straddle price (close-to-close).
        """
        logger.info("=" * 60)
        logger.info(f"BACKTEST (STRADDLE): {self.instrument}")
        logger.info("=" * 60)

        active: Optional[StraddleTrade] = None
        dates = self.df['date'].unique()

        dlog = DetailedLogger(self.instrument, self.strategy, self.output_dir)
        dlog.open()

        logger.info(f"Processing {len(dates)} trading days (straddle mode)...")

        for day_num, date in enumerate(dates, 1):
            if day_num % 50 == 0:
                logger.info(
                    f"Day {day_num}/{len(dates)} | "
                    f"Trades so far: {len(self.trades)}"
                )

            # For monthly expiry: pick the best expiry_code for this day.
            # Uses the calendar-based 15-day rule, with ATM data fallback.
            if self.expiry_mode == 'monthly':
                day_data_all = self.df[self.df['date'] == date]
                expiry_code = pick_expiry_code(self.instrument, day_data_all, date)
                if expiry_code is None:
                    continue  # no ATM data at all today
                day_data = day_data_all[day_data_all['expiry_code'] == expiry_code]
            else:
                day_data = self.df[self.df['date'] == date]

            minutes = day_data['datetime'].unique()
            day_trade_count = 0
            day_sl_count = 0

            dlog.day_header(date)

            for minute in minutes:
                t = pd.Timestamp(minute)
                t_only = t.time()

                if t_only < self.entry_time:
                    continue
                if t_only > self.exit_time:
                    continue

                is_exit_time = (t_only >= self.exit_time)
                minute_data = day_data[day_data['datetime'] == minute]
                events = []

                # ATM data for this minute
                atm_data = minute_data[minute_data['moneyness'] == 'ATM']

                # Build ATM info for logging
                atm_info = {
                    'ce_strike': '--', 'ce_rsi': '--',
                    'pe_strike': '--', 'pe_rsi': '--',
                }
                ce_row = None
                pe_row = None
                for _, row in atm_data.iterrows():
                    if row['option_type'] == 'CE':
                        ce_row = row
                        atm_info['ce_strike'] = str(int(row['strike']))
                    elif row['option_type'] == 'PE':
                        pe_row = row
                        atm_info['pe_strike'] = str(int(row['strike']))

                # --- SIGNAL DETECTION (straddle mode) ---
                day_limit_hit = (
                    self.max_trades_per_day is not None
                    and day_trade_count >= self.max_trades_per_day
                )
                sl_limit_hit = (
                    self.max_sl_per_day is not None
                    and day_sl_count >= self.max_sl_per_day
                )

                if (not is_exit_time and not day_limit_hit and not sl_limit_hit
                        and active is None
                        and ce_row is not None and pe_row is not None):
                    # Check signal on any ATM row (straddle columns are same for all)
                    # Use CE row — straddle_close, straddle_bb_* are merged by datetime
                    fired, reason = check_signal(
                        ce_row, self.signal_conditions, self.signal_logic,
                    )
                    if fired:
                        # Create straddle trade: sell both legs at current ATM closes
                        active = StraddleTrade(
                            signal_time=t,
                            ce_strike=ce_row['strike'],
                            pe_strike=pe_row['strike'],
                            ce_entry_price=ce_row['close'],
                            pe_entry_price=pe_row['close'],
                            expiry_type=ce_row['expiry_type'],
                            expiry_code=ce_row['expiry_code'],
                            instrument=self.instrument,
                            direction=self.direction,
                            lot_size=self.lot_size,
                        )
                        day_trade_count += 1
                        events.append(
                            f"STRADDLE SIGNAL: {reason} | "
                            f"CE {int(ce_row['strike'])}={ce_row['close']:.2f} + "
                            f"PE {int(pe_row['strike'])}={pe_row['close']:.2f} = "
                            f"{active.straddle_entry:.2f}"
                        )

                # --- EXIT CHECK ---
                if active is not None:
                    closed, exit_msg = self._check_straddle_exit(
                        active, minute_data, day_data, t, is_exit_time
                    )
                    if closed:
                        if active.exit_reason == 'STOP_LOSS':
                            day_sl_count += 1
                        events.append(f"STRADDLE {exit_msg}")
                        active = None

                # --- WRITE LOG LINE ---
                time_str = t_only.strftime('%H:%M')
                straddle_status = self._get_straddle_status(active, minute_data)
                dlog.log_minute(
                    time_str, atm_info,
                    straddle_status, "-- (straddle mode)", events)

            # --- END OF DAY ---
            if active is not None:
                msg = self._eod_close_straddle(active, day_data, date)
                dlog.log_event(f"STRADDLE {msg}")
                active = None

        dlog.close(len(self.trades))
        logger.info(f"Backtest done (straddle). Total trades: {len(self.trades)}")
        return self.trades

    # ------------------------------------------
    # SINGLE-LEG MODE MAIN LOOP (existing behavior)
    # ------------------------------------------
    def _run_single_leg(self) -> List[Trade]:
        """
        Run the original single-leg backtest.

        CE and PE tracked independently. This is the default mode.
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
            day_sl_count = 0

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
                # Skip new signals if daily trade or SL limit reached
                day_limit_hit = (
                    self.max_trades_per_day is not None
                    and day_trade_count >= self.max_trades_per_day
                )
                sl_limit_hit = (
                    self.max_sl_per_day is not None
                    and day_sl_count >= self.max_sl_per_day
                )

                if not is_exit_time and not day_limit_hit and not sl_limit_hit:
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
                            if self.entry_indicator:
                                ind_val = row.get(self.entry_indicator)
                                entry_info = (
                                    f"entry_indicator={self.entry_indicator}"
                                    f"({ind_val:.2f})" if pd.notna(ind_val)
                                    else f"entry_indicator={self.entry_indicator}(NaN)"
                                )
                            else:
                                entry_info = " ".join(
                                    f"L{lvl.level_num}={lvl.target_price:.2f}"
                                    for lvl in active_ce.entry_levels
                                )
                            events.append(
                                f"CE SIGNAL: {reason} on "
                                f"{int(row['strike'])} CE | "
                                f"base={row['close']:.2f} | {entry_info}"
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
                            if self.entry_indicator:
                                ind_val = row.get(self.entry_indicator)
                                entry_info = (
                                    f"entry_indicator={self.entry_indicator}"
                                    f"({ind_val:.2f})" if pd.notna(ind_val)
                                    else f"entry_indicator={self.entry_indicator}(NaN)"
                                )
                            else:
                                entry_info = " ".join(
                                    f"L{lvl.level_num}={lvl.target_price:.2f}"
                                    for lvl in active_pe.entry_levels
                                )
                            events.append(
                                f"PE SIGNAL: {reason} on "
                                f"{int(row['strike'])} PE | "
                                f"base={row['close']:.2f} | {entry_info}"
                            )

                # --- ENTRY CHECK (not at exit time) ---
                # Uses indicator level entry if entry_indicator is set,
                # otherwise falls back to staggered/direct entry.
                if not is_exit_time:
                    for opt_label, active in [("CE", active_ce), ("PE", active_pe)]:
                        if active is None:
                            continue
                        if active.status not in ('WAITING_ENTRY', 'PARTIAL_POSITION'):
                            continue

                        if self.entry_indicator:
                            entry_events = self._check_indicator_entry(
                                active, minute_data, t
                            )
                        else:
                            entry_events = self._check_staggered_entry(
                                active, minute_data, t
                            )
                        events.extend([f"{opt_label} {e}" for e in entry_events])

                # --- EXIT MANAGEMENT ---
                if active_ce:
                    closed, exit_msg = self._check_exit(
                        active_ce, minute_data, day_data, t, is_exit_time
                    )
                    if closed:
                        if active_ce.exit_reason == 'STOP_LOSS':
                            day_sl_count += 1
                        events.append(f"CE {exit_msg}")
                        active_ce = None

                if active_pe:
                    closed, exit_msg = self._check_exit(
                        active_pe, minute_data, day_data, t, is_exit_time
                    )
                    if closed:
                        if active_pe.exit_reason == 'STOP_LOSS':
                            day_sl_count += 1
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
