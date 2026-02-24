"""
Dhan WebSocket live market data feed.

Provides real-time tick data via Dhan's WebSocket API (DhanFeed).
Runs in a background thread and stores latest prices in a thread-safe cache.

Used by the forward test engine for live data instead of REST API polling.
This eliminates rate-limiting issues and gives sub-second price updates.

Key features:
  - Background thread for continuous data reception
  - Thread-safe LTP cache keyed by integer security_id
  - Automatic reconnection on disconnection with exponential backoff
  - Resubscribe support when ATM shifts beyond subscribed range

Architecture:
  - DhanFeed (from dhanhq) runs in a dedicated background thread
  - Each tick updates a shared dict protected by a threading.Lock
  - The forward engine reads latest prices via get_ltp() (lock-free read pattern)
  - REST API is used as fallback when WebSocket data is unavailable
"""

import asyncio
import logging
import os
import ssl
import threading
import time
from typing import Dict, List, Optional, Tuple

# Fix SSL certificate verification on macOS.
# Python on macOS often can't find system CA certs; certifi provides them.
try:
    import certifi
    os.environ.setdefault('SSL_CERT_FILE', certifi.where())
except ImportError:
    pass

from dhanhq import DhanFeed
from dhanhq.marketfeed import (
    IDX, NSE, NSE_FNO, BSE, BSE_FNO, Ticker, Quote,
)

logger = logging.getLogger(__name__)

# Map REST API exchange strings -> WebSocket exchange integer codes
EXCHANGE_WS_MAP = {
    'IDX_I': IDX,        # 0 — Index
    'NSE_EQ': NSE,       # 1 — NSE Equity
    'NSE_FNO': NSE_FNO,  # 2 — NSE Futures & Options
    'BSE_EQ': BSE,       # 4 — BSE Equity
    'BSE_FNO': BSE_FNO,  # 8 — BSE Futures & Options
}

# Reverse map for logging
WS_EXCHANGE_NAMES = {v: k for k, v in EXCHANGE_WS_MAP.items()}


# ============================================
# 1-MINUTE CANDLE AGGREGATOR
# ============================================

class CandleAggregator:
    """
    Aggregates real-time ticks into 1-minute OHLC candles.

    For each security_id, tracks the current (forming) candle and
    the last completed candle. Thread-safe — called from the WS thread,
    read from the engine thread.

    Minute boundary = when the minute component of the timestamp changes.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # security_id -> current forming candle dict
        self._current: Dict[int, dict] = {}
        # security_id -> last completed candle dict
        self._completed: Dict[int, dict] = {}

    def on_tick(self, security_id: int, ltp: float, tick_time: float):
        """
        Process an incoming tick. Called from the WebSocket thread.

        Args:
            security_id: instrument security ID
            ltp: last traded price (float)
            tick_time: epoch timestamp of the tick
        """
        # Determine which minute this tick belongs to
        minute_key = int(tick_time) // 60

        with self._lock:
            candle = self._current.get(security_id)

            if candle is None or candle["minute_key"] != minute_key:
                # New minute — close the old candle and start fresh
                if candle is not None:
                    self._completed[security_id] = candle
                self._current[security_id] = {
                    "minute_key": minute_key,
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "ticks": 1,
                }
            else:
                # Same minute — update OHLC
                candle["high"] = max(candle["high"], ltp)
                candle["low"] = min(candle["low"], ltp)
                candle["close"] = ltp
                candle["ticks"] += 1

    def get_completed_candle(self, security_id: int) -> Optional[dict]:
        """
        Get the last completed 1-minute candle for a security.

        Returns dict with open, high, low, close, ticks or None.
        """
        with self._lock:
            return self._completed.get(security_id)

    def get_current_candle(self, security_id: int) -> Optional[dict]:
        """
        Get the in-progress (forming) candle for a security.

        Useful for intra-minute high/low. Returns None if no ticks yet.
        """
        with self._lock:
            c = self._current.get(security_id)
            return dict(c) if c else None  # return a copy

    def clear(self):
        """Clear all candle data (e.g. on resubscribe)."""
        with self._lock:
            self._current.clear()
            self._completed.clear()


def _patch_ssl_connect(feed: DhanFeed):
    """
    Monkey-patch DhanFeed.connect to pass an SSL context with certifi certs.

    The websockets library creates its own SSL context when connecting to
    wss:// URLs. On macOS, Python often can't find system root CA certs,
    causing CERTIFICATE_VERIFY_FAILED. This patch injects a proper SSL
    context so the handshake succeeds.
    """
    try:
        import certifi
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        # No certifi — try system certs (may still fail on macOS)
        ssl_ctx = ssl.create_default_context()

    import websockets

    _original_connect = feed.connect

    async def _patched_connect():
        """Wrapper that passes ssl= to websockets.connect."""
        if not feed.ws or feed.ws.state == websockets.protocol.State.CLOSED:
            if feed.version == 'v2':
                url = (
                    f"wss://api-feed.dhan.co?version=2"
                    f"&token={feed.access_token}"
                    f"&clientId={feed.client_id}"
                    f"&authType=2"
                )
                feed.ws = await websockets.connect(url, ssl=ssl_ctx)
            else:
                feed.ws = await websockets.connect(
                    'wss://api-feed.dhan.co', ssl=ssl_ctx
                )
                await feed.authorize()
            await feed.subscribe_instruments()
        else:
            try:
                await feed.ws.ping()
            except websockets.ConnectionClosed:
                feed.ws = None
                await _patched_connect()

    feed.connect = _patched_connect


class DhanWebSocketFeed:
    """
    Real-time market data feed via Dhan WebSocket.

    Runs DhanFeed in a background thread and stores latest
    tick data (LTP) per security_id in a thread-safe dict.

    Usage:
        ws = DhanWebSocketFeed(client_id, access_token)
        ws.start(instruments)
        ...
        ltp = ws.get_ltp(security_id)
        ...
        ws.stop()
    """

    def __init__(self, client_id: str, access_token: str):
        self._client_id = client_id
        self._access_token = access_token

        # Thread-safe price cache: security_id (int) -> latest tick dict
        self._cache: Dict[int, dict] = {}
        self._lock = threading.Lock()

        # 1-minute OHLC candle aggregator (built from ticks)
        self.candles = CandleAggregator()

        # Background thread management
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected = threading.Event()
        self._feed: Optional[DhanFeed] = None

        # Current instrument list (for reconnection)
        self._instruments: List[Tuple] = []

        # Set of subscribed security IDs (for quick membership check)
        self._subscribed_ids: set = set()

        # Stats
        self._tick_count = 0
        self._last_tick_time = 0.0

    @property
    def is_connected(self) -> bool:
        """True if WebSocket is connected and receiving data."""
        return self._connected.is_set()

    @property
    def tick_count(self) -> int:
        return self._tick_count

    def start(self, instruments: List[Tuple]):
        """
        Start WebSocket connection in a background thread.

        Args:
            instruments: List of (exchange_code, "security_id_str", request_code)
                         e.g. [(0, "13", 15), (2, "48211", 15)]
        """
        if self._thread and self._thread.is_alive():
            logger.warning("WebSocket feed already running, stopping first")
            self.stop()

        self._instruments = list(instruments)
        self._subscribed_ids = {
            int(tup[1]) for tup in instruments
        }
        self._stop_event.clear()
        self._connected.clear()

        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="dhan-ws-feed",
        )
        self._thread.start()

        # Wait for initial connection (with timeout)
        connected = self._connected.wait(timeout=15)
        if connected:
            logger.info(
                f"WebSocket feed started: {len(instruments)} instruments, "
                f"connected in background thread"
            )
        else:
            logger.warning(
                "WebSocket connection timeout (15s). "
                "Will keep trying in background."
            )

    def stop(self):
        """Stop the WebSocket feed and background thread."""
        self._stop_event.set()

        if self._feed:
            try:
                self._feed.close_connection()
            except Exception:
                pass

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        self._connected.clear()
        logger.info("WebSocket feed stopped")

    def get_ltp(self, security_id: int) -> Optional[float]:
        """
        Get latest LTP for a security. Thread-safe.

        Returns float price or None if not available.
        """
        with self._lock:
            data = self._cache.get(security_id)
        if data is None:
            return None

        ltp = data.get("LTP")
        if ltp is None:
            return None

        # LTP comes as formatted string from DhanFeed binary parser
        try:
            return float(ltp)
        except (ValueError, TypeError):
            return None

    def get_quote(self, security_id: int) -> Optional[dict]:
        """
        Get full quote data for a security. Thread-safe.

        Returns dict with ltp, open, high, low, close, volume etc.
        Only available if subscribed with Quote mode (17).
        """
        with self._lock:
            data = self._cache.get(security_id)
        if data is None:
            return None

        result = {}
        for key in ("LTP", "open", "high", "low", "close"):
            val = data.get(key)
            if val is not None:
                try:
                    result[key.lower()] = float(val)
                except (ValueError, TypeError):
                    pass

        for key in ("volume", "OI"):
            val = data.get(key)
            if val is not None:
                try:
                    result[key.lower()] = int(val)
                except (ValueError, TypeError):
                    pass

        return result if result else None

    def is_subscribed(self, security_id: int) -> bool:
        """Check if a security_id is in our subscription list."""
        return security_id in self._subscribed_ids

    def resubscribe(self, instruments: List[Tuple]):
        """
        Replace current subscriptions with a new instrument list.

        Disconnects and reconnects with the new set. This is more
        reliable than dynamic subscribe/unsubscribe since DhanFeed's
        asyncio.ensure_future() doesn't work safely across threads.
        """
        logger.info(
            f"Resubscribing WebSocket: "
            f"{len(self._instruments)} -> {len(instruments)} instruments"
        )
        self.stop()
        self.candles.clear()

        # Brief pause between disconnect and reconnect
        time.sleep(1)
        self.start(instruments)

    # ------------------------------------------
    # Background thread
    # ------------------------------------------
    def _run_loop(self):
        """
        Background thread target.

        Connects to WebSocket and continuously reads ticks.
        Reconnects automatically on disconnection with exponential backoff.
        """
        MAX_RECONNECT_DELAY = 30
        reconnect_delay = 1

        while not self._stop_event.is_set():
            try:
                logger.info(
                    f"WebSocket connecting... "
                    f"({len(self._instruments)} instruments)"
                )

                # Create a fresh asyncio event loop for this thread.
                # DhanFeed uses loop.run_until_complete() internally,
                # so it needs a dedicated loop in this thread.
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                self._feed = DhanFeed(
                    self._client_id,
                    self._access_token,
                    self._instruments,
                    version='v2',
                )
                # Ensure DhanFeed uses our thread-local event loop
                self._feed.loop = loop

                # Patch DhanFeed.connect to pass an SSL context with
                # certifi CA bundle. Fixes "CERTIFICATE_VERIFY_FAILED"
                # on macOS where Python can't find system root certs.
                _patch_ssl_connect(self._feed)

                # Establish connection and subscribe
                self._feed.run_forever()
                self._connected.set()
                reconnect_delay = 1

                logger.info("WebSocket connected and subscribed")

                # Continuous tick reading loop
                while not self._stop_event.is_set():
                    try:
                        data = self._feed.get_data()
                    except Exception as recv_err:
                        logger.warning(f"WebSocket recv error: {recv_err}")
                        break

                    if data is None:
                        continue

                    # Handle server disconnection message (string)
                    if not isinstance(data, dict):
                        logger.warning(f"WebSocket non-dict data: {data}")
                        break

                    # Check for disconnection type
                    dtype = data.get("type", "")
                    if "Disconnected" in str(dtype):
                        logger.warning(f"Server disconnection: {data}")
                        break

                    # Store tick in cache and update candle aggregator
                    sec_id = data.get("security_id")
                    if sec_id is not None:
                        tick_time = time.time()
                        with self._lock:
                            self._cache[sec_id] = data
                        self._tick_count += 1
                        self._last_tick_time = tick_time

                        # Feed LTP into the candle aggregator for OHLC
                        ltp_str = data.get("LTP")
                        if ltp_str is not None:
                            try:
                                self.candles.on_tick(
                                    sec_id, float(ltp_str), tick_time
                                )
                            except (ValueError, TypeError):
                                pass

            except Exception as e:
                logger.error(f"WebSocket error: {e}", exc_info=True)

            finally:
                self._connected.clear()
                try:
                    if self._feed:
                        self._feed.close_connection()
                except Exception:
                    pass
                try:
                    loop.close()
                except Exception:
                    pass

            # Reconnect with exponential backoff (unless stopping)
            if not self._stop_event.is_set():
                logger.info(f"WebSocket reconnecting in {reconnect_delay}s...")
                self._stop_event.wait(timeout=reconnect_delay)
                reconnect_delay = min(
                    reconnect_delay * 2, MAX_RECONNECT_DELAY
                )

        logger.info("WebSocket background thread exiting")


# ============================================
# HELPER FUNCTIONS
# ============================================

def build_instrument_tuple(exchange_str: str, security_id: int,
                           mode: int = Ticker) -> Tuple:
    """
    Build a DhanFeed instrument tuple from REST API exchange string
    and integer security ID.

    Args:
        exchange_str: Exchange segment like 'IDX_I', 'NSE_FNO', 'BSE_FNO'
        security_id:  Integer security ID from Dhan
        mode:         Request mode — Ticker(15), Quote(17), Full(21)

    Returns:
        Tuple of (ws_exchange_code, security_id_str, mode)
    """
    ws_exchange = EXCHANGE_WS_MAP.get(exchange_str)
    if ws_exchange is None:
        raise ValueError(
            f"Unknown exchange: {exchange_str}. "
            f"Known: {list(EXCHANGE_WS_MAP.keys())}"
        )
    return (ws_exchange, str(security_id), mode)


def get_option_ws_exchange(instrument: str) -> int:
    """
    Get the WebSocket exchange code for options of a given instrument.

    NIFTY/BANKNIFTY -> NSE_FNO (2)
    SENSEX          -> BSE_FNO (8)
    Others          -> NSE_FNO (2)
    """
    if instrument == 'SENSEX':
        return BSE_FNO
    return NSE_FNO
