"""
Backtest entry point (CLI).

Usage:
    python run_backtest.py --strategy rsi_ema_cross
    python run_backtest.py --strategy rsi_ema_cross --instrument NIFTY
    python run_backtest.py --list

Loads a strategy JSON from saved_strategies/ folder,
runs the backtest engine for each instrument,
and generates reports (CSV, trade log, summary).
"""

import sys
import json
import logging
import os
from typing import Dict, Optional

import config
from engine.data_loader import load_data, calculate_indicators
from engine.backtest import BacktestEngine
from engine import reporter

# ============================================
# LOGGING SETUP
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

STRATEGIES_DIR = os.path.join(os.path.dirname(__file__), "saved_strategies")


def load_strategy(strategy_name: str) -> Dict:
    """
    Load a strategy config dict from saved_strategies/ folder.

    Args:
        strategy_name: JSON filename without extension (e.g., "rsi_ema_cross")

    Returns:
        Strategy config dict
    """
    path = os.path.join(STRATEGIES_DIR, f"{strategy_name}.json")
    if not os.path.exists(path):
        logger.error(f"Strategy file not found: {path}")
        logger.info(f"Available: {list_strategies()}")
        sys.exit(1)
    with open(path, "r") as f:
        return json.load(f)


def list_strategies() -> list:
    """Return names of all saved strategies."""
    if not os.path.isdir(STRATEGIES_DIR):
        return []
    return [f.replace(".json", "") for f in sorted(os.listdir(STRATEGIES_DIR))
            if f.endswith(".json")]


def run_for_instrument(instrument: str, strategy: Dict) -> Optional[Dict]:
    """
    Run backtest for one instrument.

    Steps:
      1. Load data from parquet
      2. Calculate indicators
      3. Run backtest engine
      4. Generate report

    Returns report dict or None on error.
    """
    data_path = config.DATA_PATH.get(instrument)
    if not data_path:
        logger.error(f"No data path configured for {instrument}")
        return None

    lot_size = config.LOT_SIZE.get(instrument, 1)

    # 1. Load and filter data
    df = load_data(
        data_path=data_path,
        start_date=strategy.get('backtest_start', '2025-01-01'),
        end_date=strategy.get('backtest_end', '2025-12-31'),
    )

    # 2. Calculate indicators (generic, from strategy config)
    df = calculate_indicators(df, strategy.get('indicators', []))

    # 3. Run backtest (output_dir defaults to "." for CLI usage)
    engine = BacktestEngine(instrument, df, strategy, lot_size, output_dir=".")
    trades = engine.run()

    # 4. Generate report
    report = reporter.generate_report(
        trades=trades,
        instrument=instrument,
        lot_size=lot_size,
        initial_capital=strategy.get('initial_capital', 200000),
        start_date=strategy.get('backtest_start', '2025-01-01'),
        end_date=strategy.get('backtest_end', '2025-12-31'),
    )

    # Print to console
    reporter.print_report(report, strategy.get('name', ''))

    return report


def main():
    """Main entry point."""
    # --list flag: print available strategies and exit
    if "--list" in sys.argv:
        print("Available strategies:")
        for name in list_strategies():
            print(f"  {name}")
        sys.exit(0)

    # Parse strategy name from command line
    strategy_name = None
    if "--strategy" in sys.argv:
        idx = sys.argv.index("--strategy")
        if idx + 1 < len(sys.argv):
            strategy_name = sys.argv[idx + 1]
    elif len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        strategy_name = sys.argv[1]

    if strategy_name is None:
        print("Usage: python run_backtest.py --strategy <name>")
        print("       python run_backtest.py --list")
        sys.exit(1)

    logger.info(f"Loading strategy: {strategy_name}")
    strategy = load_strategy(strategy_name)
    logger.info(f"Strategy: {strategy.get('name', 'Unknown')}")
    logger.info(f"Description: {strategy.get('description', '')}")

    # Get instruments from strategy config
    instruments = strategy.get('instruments', ['NIFTY', 'SENSEX'])
    reports: Dict[str, Dict] = {}

    for inst in instruments:
        try:
            report = run_for_instrument(inst, strategy)
            if report:
                reports[inst] = report
        except Exception as e:
            logger.error(f"Error backtesting {inst}: {e}", exc_info=True)

    # Save outputs
    if reports:
        # CSV per instrument
        for inst, report in reports.items():
            filename = f"backtest_results_{inst}.csv"
            reporter.save_csv(report, filename)

        # Trade log + markdown summary
        reporter.write_trade_log(reports, strategy)
        reporter.write_summary(reports, strategy)

    logger.info("All done.")


if __name__ == "__main__":
    main()
