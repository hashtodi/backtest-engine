"""
Paper trader — thin wrapper around ForwardTestEngine.

Responsibilities:
  - Tracks completed trades and running P&L
  - Logs every event with timestamp to a log file
  - Provides get_summary() for the UI
  - Optionally sends Telegram notifications on key events
"""

import logging
import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pytz

from engine.trade import Trade

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Directory for forward test logs
LOG_DIR = Path("forward_test_logs")


class PaperTrader:
    """
    Collects events from the ForwardTestEngine and maintains a session summary.

    Usage:
        pt = PaperTrader("NIFTY", "my_strategy")
        engine = ForwardTestEngine(strategy, feed, "NIFTY", 75)
        engine.run_loop(on_event=pt.on_event)
        print(pt.get_summary())
    """

    def __init__(self, instrument: str, strategy_name: str,
                 lot_size: int = 1):
        self.instrument = instrument
        self.strategy_name = strategy_name
        self.lot_size = lot_size

        # Event log (kept in memory for UI display)
        self.events: List[dict] = []

        # Trade tracking (populated from engine.completed_trades)
        self.completed_trades: List[Trade] = []

        # Session counters
        self.signals_count = 0
        self.entries_count = 0
        self.exits_count = 0

        # Log file
        LOG_DIR.mkdir(exist_ok=True)
        ts = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
        self.log_path = LOG_DIR / f"{instrument}_{strategy_name}_{ts}.log"
        self._log_file = None

    # ------------------------------------------
    # EVENT HANDLER (passed to engine.run_loop)
    # ------------------------------------------
    def on_event(self, event: dict):
        """
        Callback for each engine event.

        Stores in memory, writes to log file, and updates counters.
        """
        self.events.append(event)

        # Update counters
        etype = event.get("type", "")
        if etype == "signal":
            self.signals_count += 1
        elif etype == "entry":
            self.entries_count += 1
        elif etype == "exit":
            self.exits_count += 1

        # Write to log file
        self._write_log_line(event)

        # Also log to Python logger (with appropriate level)
        msg = event.get("message", "")
        if etype == "error":
            logger.error(msg)
        elif etype in ("signal", "entry", "exit"):
            # Important events — always visible
            tag = etype.upper()
            logger.info(f"[{tag:6s}] {msg}")
        elif etype == "info":
            logger.info(msg)
        else:
            logger.debug(msg)

    # ------------------------------------------
    # LOG FILE
    # ------------------------------------------
    def _write_log_line(self, event: dict):
        """Append one event line to the log file."""
        try:
            time_str = event.get("time", datetime.now(IST))
            if hasattr(time_str, "strftime"):
                time_str = time_str.strftime("%Y-%m-%d %H:%M:%S")

            line = (
                f"[{time_str}] "
                f"[{event.get('type', '').upper():6s}] "
                f"{event.get('option_type', ''):2s} "
                f"{event.get('message', '')}\n"
            )

            with open(self.log_path, "a") as f:
                f.write(line)
        except Exception as e:
            logger.warning(f"Failed to write log line: {e}")

    # ------------------------------------------
    # TRADE TRACKING
    # ------------------------------------------
    def sync_trades(self, trades: List[Trade]):
        """
        Sync completed trades from the engine.
        Call this periodically or at the end of the session.
        """
        self.completed_trades = list(trades)

    # ------------------------------------------
    # SUMMARY
    # ------------------------------------------
    def get_summary(self) -> dict:
        """
        Return a session summary dict for UI display.

        Keys:
          - instrument, strategy_name
          - signals, entries, exits
          - total_trades, winning_trades, losing_trades
          - win_rate
          - total_pnl_pct, total_money_pnl
          - trades: list of trade dicts
        """
        total = len(self.completed_trades)
        winners = [t for t in self.completed_trades if t.total_pnl > 0]
        losers = [t for t in self.completed_trades if t.total_pnl < 0]
        flat = [t for t in self.completed_trades if t.total_pnl == 0]

        total_pnl_pct = sum(t.total_pnl_pct for t in self.completed_trades)
        total_money = sum(t.get_money_pnl() for t in self.completed_trades)
        win_rate = (len(winners) / total * 100) if total > 0 else 0.0

        return {
            "instrument": self.instrument,
            "strategy_name": self.strategy_name,
            "signals": self.signals_count,
            "entries": self.entries_count,
            "exits": self.exits_count,
            "total_trades": total,
            "winning_trades": len(winners),
            "losing_trades": len(losers),
            "flat_trades": len(flat),
            "win_rate": win_rate,
            "total_pnl_pct": total_pnl_pct,
            "total_money_pnl": total_money,
            "log_file": str(self.log_path),
            "trades": [t.to_dict() for t in self.completed_trades],
        }

    def get_recent_events(self, n: int = 50) -> List[dict]:
        """Return the last N events for UI display."""
        return self.events[-n:]
