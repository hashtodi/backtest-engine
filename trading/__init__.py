"""
Trading module — paper and live order execution.

Provides a unified interface for order placement that works in
both paper (simulated) and live (real money via Dhan API) modes.

Components:
  - order_executor.py  — places/cancels orders (paper or live)
  - risk_manager.py    — daily loss limit, kill switch, position sizing
  - order_tracker.py   — polls order status, tracks fills
"""

from trading.order_executor import OrderExecutor
from trading.risk_manager import RiskManager
from trading.order_tracker import OrderTracker

__all__ = ["OrderExecutor", "RiskManager", "OrderTracker"]
