"""
Tick-level fast checker for entries, SL, and TP.

Called every ~1 second between minute boundaries.
Uses the latest WebSocket LTP — no indicator calculations,
no buffer updates. Only checks price against trade levels.
"""

import logging
from datetime import datetime
from typing import List, Optional

from engine.trade import Trade
from forward.helpers import IST, make_event

logger = logging.getLogger(__name__)


class TickChecker:
    """
    Lightweight tick-level checker for staggered entry and SL/TP exits.

    Operates on raw WebSocket LTP values — no indicator or buffer work.
    The ForwardTestEngine owns one instance and calls check() each second.
    """

    def __init__(self, direction: str, stop_loss_pct: float,
                 target_pct: float, instrument: str,
                 get_ws_option_ltp_fn=None, telegram=None):
        """
        Args:
            direction:            "buy" or "sell"
            stop_loss_pct:        SL as % from avg entry
            target_pct:           TP as % from avg entry
            instrument:           e.g. "NIFTY"
            get_ws_option_ltp_fn: callable(strike, opt_type) -> Optional[float]
            telegram:             optional TelegramNotifier
        """
        self.direction = direction
        self.stop_loss_pct = stop_loss_pct
        self.target_pct = target_pct
        self.instrument = instrument
        self._get_ws_ltp = get_ws_option_ltp_fn
        self.telegram = telegram

    def check(self, active_ce: Optional[Trade],
              active_pe: Optional[Trade],
              ws_feed) -> List[dict]:
        """
        Run tick-level entry/exit checks on active trades.

        Args:
            active_ce: currently active CE trade (or None)
            active_pe: currently active PE trade (or None)
            ws_feed:   DhanWebSocketFeed instance (or None)

        Returns:
            list of event dicts (entry/exit only)
        """
        events: List[dict] = []
        now = datetime.now(IST)

        if not ws_feed or not ws_feed.is_connected:
            return events

        for opt_type, active in [("CE", active_ce), ("PE", active_pe)]:
            if active is None:
                continue

            # Get live tick LTP for the trade's specific contract
            ltp = self._get_ws_ltp(active.strike, opt_type) if self._get_ws_ltp else None
            if ltp is None:
                continue

            # -- Staggered entry check --
            if active.status in ("WAITING_ENTRY", "PARTIAL_POSITION"):
                entry_msgs = _check_staggered_entry(
                    active, ltp, now, self.direction
                )
                for msg in entry_msgs:
                    events.append(make_event(
                        "entry",
                        f"[TICK] {opt_type} {msg}",
                        option_type=opt_type,
                    ))
                    self._send_telegram(f"ENTRY", opt_type, f"[tick] {msg}")

            # -- SL / TP exit check --
            if active.has_position():
                closed, exit_msg = _check_exit(
                    active, ltp, now, False,
                    self.direction, self.stop_loss_pct, self.target_pct,
                )
                if closed:
                    events.append(make_event(
                        "exit",
                        f"[TICK] {opt_type} {exit_msg}",
                        option_type=opt_type,
                    ))
                    self._send_telegram("EXIT", opt_type, f"[tick] {exit_msg}")

        return events

    def _send_telegram(self, tag: str, opt_type: str, msg: str):
        """Send telegram notification if configured."""
        if self.telegram:
            try:
                self.telegram.send_message(
                    f"<b>{tag}</b> {self.instrument} {opt_type}\n{msg}"
                )
            except Exception:
                pass


# ============================================
# SHARED TRADE-LEVEL FUNCTIONS
# ============================================
# These are used by both tick_checker and the main engine.

def check_indicator_entry(trade: Trade, prev_ltp: float, curr_ltp: float,
                          indicator_value: float, t: datetime,
                          direction: str) -> List[str]:
    """
    Check if price crossed through the indicator level between two ticks.

    The indicator value is the dynamic limit order price. If it falls
    between prev_ltp and curr_ltp, the price must have passed through it.
    Fill at the indicator value (single level, 100% capital).

    Returns list of event messages.
    """
    events = []
    if trade.status != "WAITING_ENTRY":
        return events

    # Check: did the price cross through the indicator level?
    lo = min(prev_ltp, curr_ltp)
    hi = max(prev_ltp, curr_ltp)

    if lo <= indicator_value <= hi:
        # Update target and fill
        trade.update_entry_target(indicator_value)
        next_level = trade.get_next_unfilled_level()
        if next_level is not None:
            trade.add_entry(next_level, t, indicator_value)
            events.append(
                f"ENTRY (indicator level): "
                f"LTP {prev_ltp:.2f}->{curr_ltp:.2f} crossed "
                f"indicator={indicator_value:.2f} | "
                f"filled @ {indicator_value:.2f}"
            )

    return events


def _check_staggered_entry(trade: Trade, ltp: float, t: datetime,
                           direction: str) -> List[str]:
    """
    Check if staggered entry levels are hit at current LTP.

    Returns list of event messages.
    """
    events = []
    next_level = trade.get_next_unfilled_level()

    while next_level is not None:
        if direction == "sell":
            if ltp >= next_level.target_price:
                trade.add_entry(next_level, t, next_level.target_price)
                events.append(
                    f"ENTRY Part{next_level.level_num}: "
                    f"LTP={ltp:.2f} >= "
                    f"L{next_level.level_num}={next_level.target_price:.2f}"
                )
                next_level = trade.get_next_unfilled_level()
            else:
                break
        else:
            if ltp <= next_level.target_price:
                trade.add_entry(next_level, t, next_level.target_price)
                events.append(
                    f"ENTRY Part{next_level.level_num}: "
                    f"LTP={ltp:.2f} <= "
                    f"L{next_level.level_num}={next_level.target_price:.2f}"
                )
                next_level = trade.get_next_unfilled_level()
            else:
                break

    return events


def _check_exit(trade: Trade, ltp: float, t: datetime,
                is_exit_time: bool, direction: str,
                stop_loss_pct: float, target_pct: float) -> tuple:
    """
    Check SL / TP / EOD exit for an active trade.

    Returns (closed: bool, event_msg: str or None).
    """
    if not trade.has_position():
        return False, None

    avg_entry = trade.get_avg_entry_price()
    if avg_entry is None:
        return False, None

    exit_reason = None
    exit_price = None
    msg = None

    if direction == "sell":
        sl_price = avg_entry * (1 + stop_loss_pct / 100)
        tp_price = avg_entry * (1 - target_pct / 100)

        if ltp >= sl_price:
            exit_reason = "STOP_LOSS"
            exit_price = sl_price
            pnl_pct = -stop_loss_pct
            msg = (f"EXIT SL: LTP={ltp:.2f} >= SL={sl_price:.2f} | "
                   f"avg={avg_entry:.2f} | pnl={pnl_pct:+.2f}%")
        elif ltp <= tp_price:
            exit_reason = "TARGET"
            exit_price = tp_price
            pnl_pct = target_pct
            msg = (f"EXIT TP: LTP={ltp:.2f} <= TP={tp_price:.2f} | "
                   f"avg={avg_entry:.2f} | pnl=+{pnl_pct:.2f}%")
    else:
        sl_price = avg_entry * (1 - stop_loss_pct / 100)
        tp_price = avg_entry * (1 + target_pct / 100)

        if ltp <= sl_price:
            exit_reason = "STOP_LOSS"
            exit_price = sl_price
            pnl_pct = -stop_loss_pct
            msg = (f"EXIT SL: LTP={ltp:.2f} <= SL={sl_price:.2f} | "
                   f"avg={avg_entry:.2f} | pnl={pnl_pct:+.2f}%")
        elif ltp >= tp_price:
            exit_reason = "TARGET"
            exit_price = tp_price
            pnl_pct = target_pct
            msg = (f"EXIT TP: LTP={ltp:.2f} >= TP={tp_price:.2f} | "
                   f"avg={avg_entry:.2f} | pnl=+{pnl_pct:.2f}%")

    # EOD: force close at current LTP
    if exit_reason is None and is_exit_time:
        exit_reason = "EOD"
        exit_price = ltp
        pnl = (avg_entry - ltp if direction == "sell" else ltp - avg_entry)
        pnl_pct = (pnl / avg_entry) * 100
        msg = (f"EXIT EOD: LTP={ltp:.2f} | avg={avg_entry:.2f} | "
               f"pnl={pnl_pct:+.2f}%")

    if exit_reason:
        trade.close_trade(t, exit_price, exit_reason)
        return True, msg

    return False, None
