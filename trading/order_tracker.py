"""
Order tracker — polls order status and tracks fills.

In live mode, queries the Dhan API to check if orders have been
filled, partially filled, or rejected. Updates the local order
state accordingly.

In paper mode, all orders are assumed to fill instantly.
"""

import logging
import time
from datetime import datetime
from typing import Optional, Dict, List

import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')

# How often to poll for order status (seconds)
DEFAULT_POLL_INTERVAL = 2.0

# Maximum time to wait for a fill before giving up (seconds)
DEFAULT_FILL_TIMEOUT = 30.0


class OrderTracker:
    """
    Tracks live orders and reports their fill status.

    Used by the forward test engine to confirm whether entries/exits
    actually went through on the exchange.
    """

    def __init__(self, rest_client=None,
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 fill_timeout: float = DEFAULT_FILL_TIMEOUT):
        """
        Args:
            rest_client:    DhanRestClient instance (None for paper mode)
            poll_interval:  seconds between status polls
            fill_timeout:   max wait for a fill before timing out
        """
        self._rest = rest_client
        self.poll_interval = poll_interval
        self.fill_timeout = fill_timeout

        # Tracked orders: order_id -> status dict
        self._orders: Dict[str, dict] = {}

    def track(self, order_id: str, expected_action: str = "ENTRY"):
        """
        Start tracking an order.

        Args:
            order_id:        Dhan order ID
            expected_action: "ENTRY" or "EXIT" (for logging)
        """
        self._orders[order_id] = {
            "order_id": order_id,
            "action": expected_action,
            "status": "PENDING",
            "tracked_at": datetime.now(IST),
            "filled_price": None,
            "filled_qty": 0,
        }
        logger.debug(f"Tracking order {order_id} ({expected_action})")

    def check_status(self, order_id: str) -> Optional[dict]:
        """
        Check the current status of a tracked order.

        Returns:
            Status dict with keys: status, filled_price, filled_qty
            or None if order is not tracked
        """
        if order_id not in self._orders:
            return None

        if self._rest is None:
            # Paper mode — assume instant fill
            self._orders[order_id]["status"] = "TRADED"
            return self._orders[order_id]

        try:
            response = self._rest.dhan.get_order_by_id(order_id)

            if response:
                status = response.get('orderStatus', 'UNKNOWN')
                self._orders[order_id]["status"] = status
                self._orders[order_id]["filled_price"] = response.get(
                    'averageTradedPrice', 0
                )
                self._orders[order_id]["filled_qty"] = response.get(
                    'filledQty', 0
                )

                if status in ('TRADED', 'PART_TRADED'):
                    logger.info(
                        f"Order {order_id} {status}: "
                        f"price={self._orders[order_id]['filled_price']} "
                        f"qty={self._orders[order_id]['filled_qty']}"
                    )
                elif status in ('REJECTED', 'CANCELLED', 'EXPIRED'):
                    logger.warning(
                        f"Order {order_id} {status}: "
                        f"{response.get('omsErrorDescription', 'N/A')}"
                    )

            return self._orders[order_id]

        except Exception as e:
            logger.error(f"Error checking order {order_id}: {e}")
            return self._orders.get(order_id)

    def wait_for_fill(self, order_id: str) -> dict:
        """
        Poll until order is filled, rejected, or timeout.

        Returns:
            Final status dict
        """
        if self._rest is None:
            return {"status": "TRADED", "filled_price": 0, "filled_qty": 0}

        start = time.time()
        while time.time() - start < self.fill_timeout:
            status = self.check_status(order_id)
            if status and status.get("status") in (
                'TRADED', 'REJECTED', 'CANCELLED', 'EXPIRED'
            ):
                return status
            time.sleep(self.poll_interval)

        logger.warning(
            f"Order {order_id} timed out after {self.fill_timeout}s"
        )
        return self._orders.get(order_id, {"status": "TIMEOUT"})

    def get_all_orders(self) -> List[dict]:
        """Return all tracked orders."""
        return list(self._orders.values())

    def get_pending_orders(self) -> List[str]:
        """Return order IDs that are still pending."""
        return [
            oid for oid, info in self._orders.items()
            if info.get("status") in ("PENDING", "TRANSIT")
        ]
