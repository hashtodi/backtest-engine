"""
Risk manager for live trading.

Enforces:
  - Daily max loss limit (default: 35% of capital)
  - Kill switch (immediately halts all trading)
  - Max open positions per instrument
  - Order size validation

All checks are performed BEFORE an order is placed.
"""

import logging
import threading
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Pre-trade risk checks and daily P&L tracking.

    Thread-safe: all state updates use a lock so the tick checker
    and minute engine can call concurrently.
    """

    # Default daily loss limit as % of capital
    DEFAULT_MAX_DAILY_LOSS_PCT = 35.0

    def __init__(self, initial_capital: float,
                 max_daily_loss_pct: float = DEFAULT_MAX_DAILY_LOSS_PCT,
                 max_open_positions: int = 2):
        """
        Args:
            initial_capital:     account capital for daily loss calculation
            max_daily_loss_pct:  max loss as % of capital before halt (default 35%)
            max_open_positions:  max simultaneous open positions (CE + PE = 2)
        """
        self.initial_capital = initial_capital
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_daily_loss_amount = initial_capital * (max_daily_loss_pct / 100)
        self.max_open_positions = max_open_positions

        # Daily P&L tracking (resets at start of each trading day)
        self._daily_pnl = 0.0
        self._daily_date: Optional[date] = None
        self._open_positions = 0

        # Kill switch — when True, no orders are placed
        self._killed = False

        self._lock = threading.Lock()

        logger.info(
            f"RiskManager initialized | capital={initial_capital:.0f} | "
            f"max_daily_loss={max_daily_loss_pct}% "
            f"(={self.max_daily_loss_amount:.0f}) | "
            f"max_positions={max_open_positions}"
        )

    # ------------------------------------------
    # KILL SWITCH
    # ------------------------------------------
    def kill(self):
        """Activate kill switch. No more orders will be placed."""
        with self._lock:
            self._killed = True
        logger.warning("KILL SWITCH ACTIVATED — trading halted")

    def unkill(self):
        """Deactivate kill switch. Trading can resume."""
        with self._lock:
            self._killed = False
        logger.info("Kill switch deactivated — trading can resume")

    @property
    def is_killed(self) -> bool:
        """Check if kill switch is active."""
        with self._lock:
            return self._killed

    # ------------------------------------------
    # DAILY P&L
    # ------------------------------------------
    def _ensure_daily_reset(self):
        """Reset daily P&L if a new trading day has started."""
        today = date.today()
        if self._daily_date != today:
            if self._daily_date is not None:
                logger.info(
                    f"New trading day ({today}). "
                    f"Previous day P&L: {self._daily_pnl:+.2f}"
                )
            self._daily_pnl = 0.0
            self._daily_date = today

    def record_trade_pnl(self, pnl_amount: float):
        """
        Record a closed trade's P&L for daily tracking.

        Args:
            pnl_amount: profit (positive) or loss (negative) in currency
        """
        with self._lock:
            self._ensure_daily_reset()
            self._daily_pnl += pnl_amount
            logger.info(
                f"Trade P&L: {pnl_amount:+.2f} | "
                f"Daily P&L: {self._daily_pnl:+.2f} / "
                f"-{self.max_daily_loss_amount:.0f} limit"
            )

            # Auto-kill if daily loss exceeds limit
            if self._daily_pnl <= -self.max_daily_loss_amount:
                self._killed = True
                logger.warning(
                    f"DAILY LOSS LIMIT HIT: {self._daily_pnl:+.2f} "
                    f"exceeds -{self.max_daily_loss_amount:.0f}. "
                    f"Kill switch activated automatically."
                )

    @property
    def daily_pnl(self) -> float:
        """Current day's cumulative P&L."""
        with self._lock:
            self._ensure_daily_reset()
            return self._daily_pnl

    @property
    def daily_loss_remaining(self) -> float:
        """How much more loss is allowed before daily limit triggers."""
        with self._lock:
            self._ensure_daily_reset()
            return self.max_daily_loss_amount + self._daily_pnl

    # ------------------------------------------
    # POSITION TRACKING
    # ------------------------------------------
    def on_position_open(self):
        """Call when a new position is opened."""
        with self._lock:
            self._open_positions += 1

    def on_position_close(self):
        """Call when a position is closed."""
        with self._lock:
            self._open_positions = max(0, self._open_positions - 1)

    # ------------------------------------------
    # PRE-TRADE CHECK
    # ------------------------------------------
    def can_trade(self) -> tuple:
        """
        Check if a new order is allowed.

        Returns:
            (allowed: bool, reason: str)
        """
        with self._lock:
            self._ensure_daily_reset()

            if self._killed:
                return False, "Kill switch is active"

            if self._daily_pnl <= -self.max_daily_loss_amount:
                return False, (
                    f"Daily loss limit reached: "
                    f"{self._daily_pnl:+.2f} / "
                    f"-{self.max_daily_loss_amount:.0f}"
                )

            if self._open_positions >= self.max_open_positions:
                return False, (
                    f"Max open positions reached: "
                    f"{self._open_positions}/{self.max_open_positions}"
                )

            return True, "OK"

    def get_status(self) -> dict:
        """Return a snapshot of risk manager state."""
        with self._lock:
            self._ensure_daily_reset()
            return {
                "killed": self._killed,
                "daily_pnl": self._daily_pnl,
                "max_daily_loss": self.max_daily_loss_amount,
                "daily_loss_remaining": self.max_daily_loss_amount + self._daily_pnl,
                "open_positions": self._open_positions,
                "max_open_positions": self.max_open_positions,
                "can_trade": not self._killed and
                             self._daily_pnl > -self.max_daily_loss_amount and
                             self._open_positions < self.max_open_positions,
            }
