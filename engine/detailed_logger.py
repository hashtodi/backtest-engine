"""
Minute-by-minute detailed log writer.

Writes a human-readable log of every minute during the backtest:
  - ATM RSI values for CE and PE
  - Current trade status (observing, position, idle)
  - Events: signals, entries, exits with reasons

Used for manual verification of backtest decisions.
"""

import logging
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)


class DetailedLogger:
    """
    Writes a detailed minute-by-minute backtest log.

    Usage:
        dlog = DetailedLogger("NIFTY", strategy_config)
        dlog.open()
        dlog.day_header(date)
        dlog.log_minute(time_str, atm_info, ce_status, pe_status, events)
        dlog.close(total_trades)
    """

    def __init__(self, instrument: str, strategy_config: Dict, output_dir: str = "."):
        self.instrument = instrument
        self.strategy_config = strategy_config
        self.file = None

        # Build output path (defaults to current dir for backward compat)
        self.path = f"{output_dir}/detailed_{instrument}.log"

    def open(self):
        """Open log file and write header."""
        self.file = open(self.path, 'w')

        cfg = self.strategy_config
        self.file.write(f"{'=' * 100}\n")
        self.file.write(f"DETAILED BACKTEST LOG: {self.instrument}\n")
        self.file.write(f"Strategy: {cfg.get('name', 'Unknown')}\n")
        self.file.write(f"Direction: {cfg.get('direction', 'sell')}\n")
        self.file.write(f"SL: {cfg.get('stop_loss_pct', 0)}% | "
                        f"TP: {cfg.get('target_pct', 0)}%\n")

        # Entry config
        entry = cfg.get('entry', {})
        etype = entry.get('type', 'direct')
        if etype == 'indicator_level':
            self.file.write(f"Entry: Indicator Level ({entry.get('indicator', '?')})\n")
        elif etype == 'staggered':
            levels_str = " / ".join(
                f"+{lvl['pct_from_base']}% ({lvl['capital_pct']}%)"
                for lvl in entry.get('levels', [])
            )
            self.file.write(f"Entry: Staggered at {levels_str}\n")
        else:
            self.file.write("Entry: Direct (100%)\n")

        # Signal conditions
        conditions = cfg.get('signal_conditions', [])
        if conditions:
            for c in conditions:
                self.file.write(
                    f"Signal: {c['indicator']} {c['compare']} "
                    f"{c.get('value', c.get('other', ''))}\n"
                )

        self.file.write(f"Hours: {cfg.get('trading_start', '09:30')} - "
                        f"{cfg.get('trading_end', '14:30')}\n")
        self.file.write(f"{'=' * 100}\n\n")

        logger.info(f"Detailed log opened: {self.path}")

    def day_header(self, date):
        """Write day separator."""
        if not self.file:
            return
        self.file.write(f"\n{'=' * 100}\n")
        self.file.write(f"  DATE: {date}\n")
        self.file.write(f"{'=' * 100}\n")

    def log_minute(
        self,
        time_str: str,
        atm_info: Dict[str, str],
        ce_status: str,
        pe_status: str,
        events: List[str],
    ):
        """
        Write one minute line + any events.

        Args:
            time_str: e.g. "09:30"
            atm_info: {
                'ce_strike': "23500", 'ce_rsi': "72.15",
                'pe_strike': "23500", 'pe_rsi': "65.30"
            }
            ce_status: short string for CE track state
            pe_status: short string for PE track state
            events: list of event description strings
        """
        if not self.file:
            return

        ce_strike = atm_info.get('ce_strike', '--')
        ce_rsi = atm_info.get('ce_rsi', '--')
        pe_strike = atm_info.get('pe_strike', '--')
        pe_rsi = atm_info.get('pe_rsi', '--')

        self.file.write(
            f"[{time_str}] "
            f"ATM CE {ce_strike} RSI={ce_rsi} | "
            f"ATM PE {pe_strike} RSI={pe_rsi} | "
            f"CE: {ce_status} | PE: {pe_status}\n"
        )

        # Write events indented below the minute line
        for event in events:
            self.file.write(f"         >>> {event}\n")

    def log_event(self, event: str):
        """Write a standalone event line (e.g., EOD close)."""
        if not self.file:
            return
        self.file.write(f"         >>> {event}\n")

    def close(self, total_trades: int):
        """Write footer and close file."""
        if not self.file:
            return

        self.file.write(f"\n{'=' * 100}\n")
        self.file.write(f"END OF LOG | Total trades: {total_trades}\n")
        self.file.write(f"{'=' * 100}\n")
        self.file.close()
        self.file = None

        logger.info(f"Detailed log saved to {self.path}")
