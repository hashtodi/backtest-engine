"""
Backtest entry point.

Usage:
    python run_backtest.py                          # runs default strategy (RSI 70 Sell)
    python run_backtest.py --strategy rsi_70_sell    # specify strategy by name

Loads a strategy config from strategies/ folder,
runs the backtest engine for each instrument,
and generates reports (CSV, trade log, summary).
"""

import sys
import logging
import importlib
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


def load_strategy(strategy_name: str) -> Dict:
    """
    Load a strategy config dict from strategies/ folder.

    Args:
        strategy_name: module name in strategies/ (e.g., "rsi_70_sell")

    Returns:
        Strategy config dict
    """
    try:
        module = importlib.import_module(f"strategies.{strategy_name}")
        return module.STRATEGY
    except ModuleNotFoundError:
        logger.error(f"Strategy '{strategy_name}' not found in strategies/")
        sys.exit(1)


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
    # Parse strategy name from command line
    strategy_name = "rsi_70_sell"  # default
    if len(sys.argv) > 1:
        if sys.argv[1] == "--strategy" and len(sys.argv) > 2:
            strategy_name = sys.argv[2]
        else:
            strategy_name = sys.argv[1]

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
