"""
CLI entry point for forward testing (paper trading).

Loads a saved strategy, initialises the Dhan data feed and
ForwardTestEngine, then runs the per-minute loop until market close
or Ctrl+C.

Usage:
    python forward_test_runner.py --strategy rsi_70_sell
    python forward_test_runner.py --strategy rsi_70_sell --instrument NIFTY
"""

import argparse
import logging
import os
import signal
import sys
import threading
from datetime import datetime

import pytz

from config import LOT_SIZE
from datafeed import DhanDataFeed
from forward.engine import ForwardTestEngine
from forward.paper_trader import PaperTrader
from ui.strategy_store import load_saved_strategy, list_saved_strategies

IST = pytz.timezone("Asia/Kolkata")

# ============================================
# LOGGING SETUP
# ============================================

def _setup_logging():
    """Configure root logger for console + file output."""
    fmt = "%(asctime)s [%(levelname)-5s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler("forward_test.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Reduce noise from the websocket background thread
    logging.getLogger("ws_feed").setLevel(logging.WARNING)
    logging.getLogger("dhanhq").setLevel(logging.WARNING)


# ============================================
# ENV FILE LOADER
# ============================================

def _load_env(path: str = ".env.local"):
    """Load environment variables from a dotenv-style file."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip("\"'")


# ============================================
# MAIN
# ============================================

def main():
    _setup_logging()
    logger = logging.getLogger("forward_test_runner")

    parser = argparse.ArgumentParser(
        description="Forward test (paper trading) a saved strategy."
    )
    parser.add_argument(
        "--strategy", "-s", default=None,
        help="Slug of the saved strategy (e.g. 'rsi_70_sell')",
    )
    parser.add_argument(
        "--instrument", "-i", default=None,
        help="Override instrument (default: first in strategy's list)",
    )
    parser.add_argument(
        "--list", "-l", action="store_true",
        help="List all saved strategies and exit",
    )
    parser.add_argument(
        "--no-websocket", action="store_true",
        help="Disable WebSocket feed; use REST API polling only",
    )
    args = parser.parse_args()

    # List mode
    if args.list:
        strategies = list_saved_strategies()
        if not strategies:
            print("No saved strategies found.")
        for s in strategies:
            print(f"  {s['slug']:30s}  {s['name']}")
        return

    # Strategy is required when not listing
    if not args.strategy:
        parser.error("--strategy is required (use --list to see available)")

    # Load env for Dhan credentials
    _load_env()
    client_id = os.getenv("CLIENT_ID")
    access_token = os.getenv("ACCESS_TOKEN")

    if not client_id or not access_token:
        logger.error(
            "Missing Dhan credentials. Set CLIENT_ID and ACCESS_TOKEN "
            "in .env.local or as environment variables."
        )
        sys.exit(1)

    # Load strategy
    strategy = load_saved_strategy(args.strategy)
    if strategy is None:
        logger.error(f"Strategy '{args.strategy}' not found.")
        logger.info("Available strategies:")
        for s in list_saved_strategies():
            logger.info(f"  {s['slug']}")
        sys.exit(1)

    # Determine instrument
    instrument = args.instrument
    if instrument is None:
        instruments = strategy.get("instruments", [])
        if not instruments:
            logger.error("Strategy has no instruments configured.")
            sys.exit(1)
        instrument = instruments[0]

    lot_size = LOT_SIZE.get(instrument, 1)

    logger.info("=" * 60)
    logger.info("FORWARD TEST (Paper Trading)")
    logger.info("=" * 60)
    logger.info(f"Strategy : {strategy.get('name', args.strategy)}")
    logger.info(f"Instrument: {instrument}")
    logger.info(f"Direction : {strategy.get('direction', 'sell')}")
    logger.info(f"Lot size  : {lot_size}")
    logger.info(f"SL: {strategy.get('stop_loss_pct', 20)}%  "
                f"TP: {strategy.get('target_pct', 10)}%")
    use_ws = not args.no_websocket
    logger.info(f"Hours: {strategy.get('trading_start', '09:30')} - "
                f"{strategy.get('trading_end', '14:30')}")
    feed_desc = "WebSocket (tick-level SL/TP/entry)" if use_ws else "REST API only (minute-level)"
    logger.info(f"Data feed: {feed_desc}")
    logger.info("=" * 60)

    # Init Dhan data feed (REST, always needed for warmup and fallback)
    feed = DhanDataFeed(client_id, access_token)

    # Init engine (WebSocket enabled by default, starts after warmup)
    engine = ForwardTestEngine(
        strategy=strategy,
        data_feed=feed,
        instrument=instrument,
        lot_size=lot_size,
        use_websocket=use_ws,
    )

    # Init paper trader
    paper = PaperTrader(instrument, args.strategy, lot_size)

    # Graceful shutdown on Ctrl+C
    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        logger.info("\nShutdown requested (Ctrl+C). Closing positions...")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Run the loop (blocks until market close or stop)
    engine.run_loop(on_event=paper.on_event, stop_event=stop_event)

    # Sync final trades
    paper.sync_trades(engine.completed_trades)

    # Print summary
    summary = paper.get_summary()
    logger.info("")
    logger.info("=" * 60)
    logger.info("SESSION SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Signals      : {summary['signals']}")
    logger.info(f"Entries      : {summary['entries']}")
    logger.info(f"Exits        : {summary['exits']}")
    logger.info(f"Total trades : {summary['total_trades']}")
    logger.info(f"Winners      : {summary['winning_trades']}")
    logger.info(f"Losers       : {summary['losing_trades']}")
    logger.info(f"Win rate     : {summary['win_rate']:.1f}%")
    logger.info(f"Total P&L %  : {summary['total_pnl_pct']:+.2f}%")
    logger.info(f"Total P&L Rs : {summary['total_money_pnl']:+.2f}")
    logger.info(f"Log file     : {summary['log_file']}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
