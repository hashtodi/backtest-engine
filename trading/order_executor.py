"""
Order executor — places orders in paper or live mode.

In paper mode: logs the order and returns a fake order ID.
In live mode: calls Dhan API to place/cancel real orders.

The forward test engine calls execute_entry() and execute_exit()
without knowing which mode is active.
"""

import logging
import uuid
from datetime import datetime
from typing import Optional, Dict

import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')

# Trading modes
MODE_PAPER = "paper"
MODE_LIVE = "live"


class OrderExecutor:
    """
    Unified order executor for paper and live trading.

    In paper mode, orders are logged but not sent to the exchange.
    In live mode, orders go through the Dhan API with full safety checks.
    """

    def __init__(self, mode: str = MODE_PAPER, rest_client=None,
                 risk_manager=None, client_id: str = ""):
        """
        Args:
            mode:         "paper" or "live"
            rest_client:  DhanRestClient (required for live mode)
            risk_manager: RiskManager instance (optional but recommended)
            client_id:    Dhan client ID (required for live mode)
        """
        if mode not in (MODE_PAPER, MODE_LIVE):
            raise ValueError(f"Invalid mode: {mode}. Use 'paper' or 'live'.")

        self.mode = mode
        self._rest = rest_client
        self._risk = risk_manager
        self._client_id = client_id

        # Order history (paper and live)
        self.order_log = []

        if mode == MODE_LIVE and rest_client is None:
            raise ValueError("rest_client is required for live mode")

        logger.info(f"OrderExecutor initialized in {mode.upper()} mode")

    @property
    def is_live(self) -> bool:
        return self.mode == MODE_LIVE

    # ------------------------------------------
    # ENTRY ORDER
    # ------------------------------------------
    def execute_entry(self, security_id: int, instrument: str,
                      strike: int, option_type: str, quantity: int,
                      direction: str, price: float,
                      level_num: int = 1) -> Optional[str]:
        """
        Place an entry order (buy or sell).

        Args:
            security_id:  Dhan security ID for the option contract
            instrument:   e.g. "NIFTY"
            strike:       strike price
            option_type:  "CE" or "PE"
            quantity:     number of lots
            direction:    "buy" or "sell" (strategy direction)
            price:        limit price (0 for market order)
            level_num:    staggered entry level (1, 2, 3...)

        Returns:
            Order ID string (real for live, fake for paper), or None on failure
        """
        # Risk check
        if self._risk:
            allowed, reason = self._risk.can_trade()
            if not allowed:
                logger.warning(
                    f"Order blocked by risk manager: {reason}"
                )
                return None

        # Map strategy direction to transaction type
        # "buy" strategy → BUY options
        # "sell" strategy → SELL options
        txn_type = "BUY" if direction == "buy" else "SELL"

        order_info = {
            "time": datetime.now(IST).isoformat(),
            "action": "ENTRY",
            "mode": self.mode,
            "instrument": instrument,
            "strike": strike,
            "option_type": option_type,
            "txn_type": txn_type,
            "quantity": quantity,
            "price": price,
            "level": level_num,
            "security_id": security_id,
        }

        if self.mode == MODE_PAPER:
            order_id = f"PAPER-{uuid.uuid4().hex[:8]}"
            order_info["order_id"] = order_id
            order_info["status"] = "FILLED"
            self.order_log.append(order_info)
            logger.info(
                f"[PAPER] ENTRY L{level_num}: {txn_type} {quantity} "
                f"{instrument} {strike} {option_type} @ {price:.2f}"
            )
            return order_id

        # --- LIVE MODE ---
        return self._place_live_order(order_info, txn_type, security_id,
                                       quantity, price, instrument)

    # ------------------------------------------
    # EXIT ORDER
    # ------------------------------------------
    def execute_exit(self, security_id: int, instrument: str,
                     strike: int, option_type: str, quantity: int,
                     direction: str, price: float,
                     exit_reason: str = "UNKNOWN") -> Optional[str]:
        """
        Place an exit order (opposite of entry direction).

        Args:
            security_id:  Dhan security ID
            instrument:   e.g. "NIFTY"
            strike:       strike price
            option_type:  "CE" or "PE"
            quantity:     number of lots to close
            direction:    original strategy direction ("buy" or "sell")
            price:        limit price (0 for market)
            exit_reason:  "STOP_LOSS", "TARGET", "EOD", etc.

        Returns:
            Order ID string or None on failure
        """
        # Exit is opposite of entry direction
        txn_type = "SELL" if direction == "buy" else "BUY"

        order_info = {
            "time": datetime.now(IST).isoformat(),
            "action": "EXIT",
            "mode": self.mode,
            "instrument": instrument,
            "strike": strike,
            "option_type": option_type,
            "txn_type": txn_type,
            "quantity": quantity,
            "price": price,
            "exit_reason": exit_reason,
            "security_id": security_id,
        }

        if self.mode == MODE_PAPER:
            order_id = f"PAPER-{uuid.uuid4().hex[:8]}"
            order_info["order_id"] = order_id
            order_info["status"] = "FILLED"
            self.order_log.append(order_info)
            logger.info(
                f"[PAPER] EXIT ({exit_reason}): {txn_type} {quantity} "
                f"{instrument} {strike} {option_type} @ {price:.2f}"
            )
            if self._risk:
                self._risk.on_position_close()
            return order_id

        # --- LIVE MODE ---
        order_id = self._place_live_order(
            order_info, txn_type, security_id, quantity, price, instrument
        )
        if order_id and self._risk:
            self._risk.on_position_close()
        return order_id

    # ------------------------------------------
    # LIVE ORDER PLACEMENT
    # ------------------------------------------
    def _place_live_order(self, order_info: dict, txn_type: str,
                          security_id: int, quantity: int,
                          price: float,
                          instrument: str) -> Optional[str]:
        """
        Place a real order via Dhan API.

        Uses MARKET order for speed. If price > 0, uses LIMIT.
        """
        try:
            # Determine exchange segment
            if instrument in ('NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY'):
                exchange_segment = self._rest.dhan.NSE_FNO
            elif instrument == 'SENSEX':
                exchange_segment = self._rest.dhan.BSE_FNO
            else:
                exchange_segment = self._rest.dhan.NSE_FNO

            # Order type: MARKET for instant fills, LIMIT if price specified
            if price > 0:
                order_type = self._rest.dhan.LIMIT
            else:
                order_type = self._rest.dhan.MARKET

            txn = (self._rest.dhan.BUY if txn_type == "BUY"
                   else self._rest.dhan.SELL)

            # Correlation ID for tracking
            corr_id = f"RSI-{uuid.uuid4().hex[:12]}"

            logger.info(
                f"[LIVE] Placing order: {txn_type} {quantity} "
                f"sec_id={security_id} @ "
                f"{'MARKET' if price == 0 else f'{price:.2f}'}"
            )

            response = self._rest.dhan.place_order(
                security_id=str(security_id),
                exchange_segment=exchange_segment,
                transaction_type=txn,
                quantity=quantity,
                order_type=order_type,
                product_type=self._rest.dhan.INTRA,
                price=price if price > 0 else 0,
                validity='DAY',
                tag=corr_id,
            )

            if response and response.get('orderId'):
                order_id = response['orderId']
                status = response.get('orderStatus', 'UNKNOWN')
                order_info["order_id"] = order_id
                order_info["status"] = status
                order_info["correlation_id"] = corr_id
                self.order_log.append(order_info)
                logger.info(
                    f"[LIVE] Order placed: id={order_id} status={status}"
                )
                if self._risk:
                    self._risk.on_position_open()
                return order_id
            else:
                logger.error(f"[LIVE] Order failed: {response}")
                order_info["status"] = "FAILED"
                order_info["error"] = str(response)
                self.order_log.append(order_info)
                return None

        except Exception as e:
            logger.error(f"[LIVE] Order exception: {e}", exc_info=True)
            order_info["status"] = "ERROR"
            order_info["error"] = str(e)
            self.order_log.append(order_info)
            return None

    # ------------------------------------------
    # CANCEL ORDER
    # ------------------------------------------
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a pending order.

        Args:
            order_id: Dhan order ID

        Returns:
            True if cancelled (or paper mode), False on failure
        """
        if self.mode == MODE_PAPER:
            logger.info(f"[PAPER] Order {order_id} cancelled")
            return True

        try:
            response = self._rest.dhan.cancel_order(order_id)
            if response and response.get('orderStatus') == 'CANCELLED':
                logger.info(f"[LIVE] Order {order_id} cancelled")
                return True
            logger.warning(f"[LIVE] Cancel failed: {response}")
            return False
        except Exception as e:
            logger.error(f"[LIVE] Cancel exception: {e}")
            return False

    # ------------------------------------------
    # STATUS / SUMMARY
    # ------------------------------------------
    def get_order_log(self) -> list:
        """Return the full order log."""
        return list(self.order_log)

    def get_summary(self) -> dict:
        """Return order count summary."""
        entries = [o for o in self.order_log if o["action"] == "ENTRY"]
        exits = [o for o in self.order_log if o["action"] == "EXIT"]
        failed = [o for o in self.order_log
                  if o.get("status") in ("FAILED", "ERROR")]
        return {
            "mode": self.mode,
            "total_orders": len(self.order_log),
            "entries": len(entries),
            "exits": len(exits),
            "failed": len(failed),
        }
