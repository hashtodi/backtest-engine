"""
Report generation for backtest results.

Handles:
  - generate_report(): builds a stats dict from Trade list
  - print_report(): prints formatted summary to console
  - save_csv(): exports trade details to CSV
  - write_trade_log(): writes detailed trade log file
  - write_summary(): writes markdown summary file

All functions are strategy-agnostic. They read from Trade objects
and strategy config dicts -- no hardcoded values.
"""

import logging
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional

from engine.trade import Trade

logger = logging.getLogger(__name__)


# ============================================
# GENERATE REPORT (builds stats dict)
# ============================================
def generate_report(
    trades: List[Trade],
    instrument: str,
    lot_size: int,
    initial_capital: float,
    start_date: str,
    end_date: str,
) -> Dict:
    """
    Build a report dict from completed trades.

    Args:
        trades: list of Trade objects
        instrument: "NIFTY" or "SENSEX"
        lot_size: contract lot size
        initial_capital: starting capital
        start_date: backtest start date string
        end_date: backtest end date string

    Returns:
        Dict with all report metrics + trades_df
    """
    if not trades:
        logger.warning(f"{instrument}: No trades to report")
        return {}

    # Build DataFrame from trade dicts
    trades_df = pd.DataFrame([t.to_dict() for t in trades])

    # Only trades that actually entered
    trades_df = trades_df[trades_df['parts_filled'] > 0].copy()
    if len(trades_df) == 0:
        logger.warning(f"{instrument}: No trades with entries")
        return {}

    total = len(trades_df)
    wins = len(trades_df[trades_df['pnl'] > 0])
    losses = len(trades_df[trades_df['pnl'] < 0])

    # Actual money P&L (option price P&L * lot size)
    total_money_pnl = trades_df['money_pnl'].sum()

    # Win/loss money stats
    wins_df = trades_df[trades_df['money_pnl'] > 0]
    loss_df = trades_df[trades_df['money_pnl'] < 0]

    report = {
        'instrument': instrument,
        'lot_size': lot_size,
        'period': f"{start_date} to {end_date}",
        'initial_capital': initial_capital,
        'final_capital': initial_capital + total_money_pnl,
        'total_pnl': trades_df['pnl'].sum(),
        'total_money_pnl': total_money_pnl,
        'return_pct': (total_money_pnl / initial_capital) * 100 if initial_capital else 0,
        'total_trades': total,
        'wins': wins,
        'losses': losses,
        'win_rate': (wins / total) * 100 if total else 0,
        'avg_pnl': trades_df['pnl'].mean(),
        'avg_money_pnl': trades_df['money_pnl'].mean(),
        'avg_win': wins_df['money_pnl'].mean() if len(wins_df) > 0 else 0,
        'avg_loss': loss_df['money_pnl'].mean() if len(loss_df) > 0 else 0,
        'max_win': trades_df['money_pnl'].max(),
        'max_loss': trades_df['money_pnl'].min(),
        'exit_reasons': trades_df['exit_reason'].value_counts().to_dict(),
        'ce_trades': len(trades_df[trades_df['option_type'] == 'CE']),
        'pe_trades': len(trades_df[trades_df['option_type'] == 'PE']),
        'ce_pnl': trades_df[trades_df['option_type'] == 'CE']['money_pnl'].sum(),
        'pe_pnl': trades_df[trades_df['option_type'] == 'PE']['money_pnl'].sum(),
        # Entry level fill stats (dynamic based on total_levels)
        'avg_parts': trades_df['parts_filled'].mean(),
        'parts_counts': trades_df['parts_filled'].value_counts().sort_index().to_dict(),
        'trades_df': trades_df,
    }
    return report


# ============================================
# PRINT REPORT (console output)
# ============================================
def print_report(report: Dict, strategy_name: str = ""):
    """Print formatted report to console."""
    if not report:
        print("No trades to report.")
        return

    r = report
    print("\n" + "=" * 60)
    title = f"  BACKTEST REPORT: {r['instrument']} (Lot Size: {r['lot_size']})"
    if strategy_name:
        title += f" | {strategy_name}"
    print(title)
    print("=" * 60)
    print(f"  Period:       {r['period']}")
    print("-" * 60)

    print(f"  Initial Capital:  Rs {r['initial_capital']:>12,.2f}")
    print(f"  Final Capital:    Rs {r['final_capital']:>12,.2f}")
    print(f"  Total P&L:        Rs {r['total_money_pnl']:>12,.2f}")
    print(f"  Return:           {r['return_pct']:>12.2f}%")
    print("-" * 60)

    print(f"  Total Trades:     {r['total_trades']:>6}")
    print(f"  Wins:             {r['wins']:>6} ({r['win_rate']:.1f}%)")
    print(f"  Losses:           {r['losses']:>6}")
    print(f"  Avg P&L:          Rs {r['avg_money_pnl']:>10,.2f}")
    print(f"  Avg Win:          Rs {r['avg_win']:>10,.2f}")
    print(f"  Avg Loss:         Rs {r['avg_loss']:>10,.2f}")
    print(f"  Max Win:          Rs {r['max_win']:>10,.2f}")
    print(f"  Max Loss:         Rs {r['max_loss']:>10,.2f}")
    print("-" * 60)

    # Parts breakdown
    print(f"  Avg Parts Filled: {r['avg_parts']:.2f}")
    for parts, count in r['parts_counts'].items():
        print(f"    {int(parts)} parts: {count} trades")
    print("-" * 60)

    print("  Exit Reasons:")
    for reason, count in r['exit_reasons'].items():
        pct = (count / r['total_trades']) * 100
        print(f"    {reason:20} {count:>4} ({pct:.1f}%)")
    print("-" * 60)

    print(f"  CE: {r['ce_trades']} trades | P&L: Rs {r['ce_pnl']:,.2f}")
    print(f"  PE: {r['pe_trades']} trades | P&L: Rs {r['pe_pnl']:,.2f}")
    print("=" * 60)


# ============================================
# SAVE CSV
# ============================================
def save_csv(report: Dict, filename: str):
    """Export trades to CSV."""
    if not report or 'trades_df' not in report:
        return
    report['trades_df'].to_csv(filename, index=False)
    logger.info(f"Saved {report['instrument']} -> {filename}")


# ============================================
# TRADE LOG WRITER
# ============================================
def write_trade_log(
    reports: Dict[str, Dict],
    strategy_config: Dict,
    output_path: str = "backtest_trades.log",
):
    """
    Write a detailed trade log file with all entries.

    Args:
        reports: {instrument: report_dict}
        strategy_config: strategy definition dict
        output_path: where to write the log
    """
    with open(output_path, 'w') as f:
        f.write("=" * 80 + "\n")
        f.write(f"BACKTEST TRADE LOG - {strategy_config.get('name', 'Strategy')}\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Direction: {strategy_config.get('direction', 'sell')}\n")
        f.write(f"SL: {strategy_config.get('stop_loss_pct', 0)}% | "
                f"TP: {strategy_config.get('target_pct', 0)}%\n")

        # Entry config summary
        entry = strategy_config.get('entry', {})
        etype = entry.get('type', 'direct')
        if etype == 'indicator_level':
            f.write(f"Entry: Indicator Level ({entry.get('indicator', '?')})\n")
        elif etype == 'staggered':
            levels_str = " / ".join(
                f"+{lvl['pct_from_base']}% ({lvl['capital_pct']}%)"
                for lvl in entry.get('levels', [])
            )
            f.write(f"Entry: Staggered at {levels_str}\n")
        else:
            f.write("Entry: Direct (100%)\n")

        f.write("=" * 80 + "\n\n")

        for instrument, report in reports.items():
            if not report:
                f.write(f"\n{instrument}: No trades\n")
                continue

            r = report
            lot = r['lot_size']
            f.write(f"\n{'=' * 60}\n")
            f.write(f"  {instrument} SUMMARY (Lot Size: {lot})\n")
            f.write(f"{'=' * 60}\n")
            f.write(f"  Trades: {r['total_trades']} | "
                    f"Wins: {r['wins']} ({r['win_rate']:.1f}%)\n")
            f.write(f"  Option P&L:  Rs {r['total_pnl']:,.2f} (price points)\n")
            f.write(f"  Money P&L:   Rs {r['total_money_pnl']:,.2f} "
                    f"(x{lot} lot) | Return: {r['return_pct']:.2f}%\n")
            f.write(f"  Capital:     Rs {r['initial_capital']:,.0f} -> "
                    f"Rs {r['final_capital']:,.0f}\n\n")

            # Individual trades
            trades_df = r['trades_df']
            num_levels = int(trades_df['total_levels'].iloc[0]) if len(trades_df) > 0 else 0

            for i, (_, trade) in enumerate(trades_df.iterrows(), 1):
                f.write(f"  --- Trade #{i} ---\n")
                f.write(f"  {trade.get('direction', 'sell').upper()} "
                        f"{trade['option_type']} | Strike: {trade['strike']} | "
                        f"{trade['expiry_type']} | Expiry Code: {trade['expiry_code']}\n")
                f.write(f"  Signal:     {trade['signal_time']} "
                        f"@ Rs {trade['base_price']:.2f}\n")

                # Entry level targets (dynamic)
                level_parts = []
                for lvl_i in range(1, num_levels + 1):
                    target_col = f'level_{lvl_i}_target'
                    pct_col = f'level_{lvl_i}_pct'
                    if target_col in trade and pd.notna(trade[target_col]):
                        level_parts.append(
                            f"+{trade[pct_col]}%={trade[target_col]:.2f}"
                        )
                if level_parts:
                    f.write(f"  Levels:     {' | '.join(level_parts)}\n")

                # Parts filled (dynamic)
                parts = int(trade['parts_filled'])
                for p_i in range(1, parts + 1):
                    time_col = f'part_{p_i}_time'
                    price_col = f'part_{p_i}_price'
                    if time_col in trade and pd.notna(trade[time_col]):
                        f.write(f"  Part {p_i}:     {trade[time_col]} "
                                f"@ Rs {trade[price_col]:.2f}\n")

                f.write(f"  Avg Entry:  Rs {trade['avg_entry_price']:.2f} "
                        f"({parts}/{num_levels} parts)\n")
                f.write(f"  Exit:       {trade['exit_time']} "
                        f"@ Rs {trade['exit_price']:.2f} "
                        f"[{trade['exit_reason']}]\n")
                f.write(f"  P&L:        Rs {trade['pnl']:.2f} "
                        f"({trade['pnl_pct']:.2f}%) | "
                        f"Money: Rs {trade['money_pnl']:,.2f}\n\n")

        f.write("\n" + "=" * 80 + "\n")
        f.write("END OF LOG\n")
        f.write("=" * 80 + "\n")

    logger.info(f"Trade log saved to {output_path}")


# ============================================
# SUMMARY FILE WRITER (markdown)
# ============================================
def write_summary(
    reports: Dict[str, Dict],
    strategy_config: Dict,
    output_path: str = "backtest_summary.md",
):
    """
    Write a markdown summary file.

    Args:
        reports: {instrument: report_dict}
        strategy_config: strategy definition dict
        output_path: where to write the summary
    """
    with open(output_path, 'w') as f:
        f.write("# Backtest Summary\n\n")

        # Strategy description
        f.write("## Strategy\n\n")
        f.write(f"- **Name**: {strategy_config.get('name', 'Unknown')}\n")
        f.write(f"- **Direction**: {strategy_config.get('direction', 'sell')}\n")

        # Signal conditions
        conditions = strategy_config.get('signal_conditions', [])
        if conditions:
            cond_strs = []
            for c in conditions:
                cond_strs.append(
                    f"{c['indicator']} {c['compare']} {c.get('value', c.get('other', ''))}"
                )
            logic = strategy_config.get('signal_logic', 'AND')
            f.write(f"- **Signal**: {f' {logic} '.join(cond_strs)}\n")

        # Entry config
        entry = strategy_config.get('entry', {})
        etype = entry.get('type', 'direct')
        if etype == 'indicator_level':
            f.write(f"- **Entry**: Indicator Level ({entry.get('indicator', '?')})\n")
        elif etype == 'staggered':
            levels_str = " / ".join(
                f"+{lvl['pct_from_base']}% ({lvl['capital_pct']}% capital)"
                for lvl in entry.get('levels', [])
            )
            f.write(f"- **Entry**: Staggered at {levels_str}\n")
        else:
            f.write("- **Entry**: Direct (100%)\n")

        f.write(f"- **Stop Loss**: {strategy_config.get('stop_loss_pct', 0)}% "
                f"(exact fill assumed)\n")
        f.write(f"- **Target**: {strategy_config.get('target_pct', 0)}% "
                f"(exact fill assumed)\n")
        f.write(f"- **Hours**: {strategy_config.get('trading_start', '09:30')} - "
                f"{strategy_config.get('trading_end', '14:30')} IST\n")
        f.write("- **Expiry**: Nearest weekly (expiry_code = 1)\n")
        f.write("- **Strikes**: ATM only\n")
        f.write("- **Intraday**: Signals expire at EOD, positions force-closed at EOD\n\n")

        # Combined summary (only if multiple instruments)
        if len(reports) > 1:
            total_money = sum(r['total_money_pnl'] for r in reports.values() if r)
            total_trades = sum(r['total_trades'] for r in reports.values() if r)
            total_wins = sum(r['wins'] for r in reports.values() if r)
            combined_capital = sum(r['initial_capital'] for r in reports.values() if r)

            f.write("---\n\n")
            f.write("## Combined Results\n\n")
            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Total Capital Deployed | Rs {combined_capital:,.0f} |\n")
            f.write(f"| Total Money P&L | Rs {total_money:,.2f} |\n")
            f.write(f"| Combined Return | "
                    f"{(total_money / combined_capital) * 100:.2f}% |\n")
            f.write(f"| Total Trades | {total_trades} |\n")
            if total_trades > 0:
                f.write(f"| Total Wins | {total_wins} "
                        f"({(total_wins / total_trades * 100):.1f}%) |\n\n")

        # Per-instrument tables
        for inst, r in reports.items():
            if not r:
                continue

            trades_df = r['trades_df']
            lot = r['lot_size']

            # Exit reason counts
            exit_counts = trades_df['exit_reason'].value_counts()

            # Option type splits
            ce_df = trades_df[trades_df['option_type'] == 'CE']
            pe_df = trades_df[trades_df['option_type'] == 'PE']

            f.write("---\n\n")
            f.write(f"## {inst} (Lot Size: {lot})\n\n")

            # Performance table
            f.write("### Performance\n\n")
            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Initial Capital | Rs {r['initial_capital']:,.0f} |\n")
            f.write(f"| Final Capital | Rs {r['final_capital']:,.0f} |\n")
            f.write(f"| Money P&L | Rs {r['total_money_pnl']:,.2f} |\n")
            f.write(f"| Return | {r['return_pct']:.2f}% |\n")
            f.write(f"| Option P&L (points) | Rs {r['total_pnl']:,.2f} |\n\n")

            # Trade stats
            f.write("### Trade Statistics\n\n")
            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Total Trades | {r['total_trades']} |\n")
            f.write(f"| Wins | {r['wins']} ({r['win_rate']:.1f}%) |\n")
            f.write(f"| Losses | {r['losses']} |\n")
            f.write(f"| Avg P&L per Trade | Rs {r['avg_money_pnl']:,.2f} |\n")
            f.write(f"| Avg Win | Rs {r['avg_win']:,.2f} |\n")
            f.write(f"| Avg Loss | Rs {r['avg_loss']:,.2f} |\n")
            f.write(f"| Max Win | Rs {r['max_win']:,.2f} |\n")
            f.write(f"| Max Loss | Rs {r['max_loss']:,.2f} |\n\n")

            # Exit reasons
            f.write("### Exit Reasons\n\n")
            f.write("| Reason | Count | % |\n")
            f.write("|--------|-------|---|\n")
            for reason, count in exit_counts.items():
                pct = (count / r['total_trades']) * 100
                f.write(f"| {reason} | {count} | {pct:.1f}% |\n")
            f.write("\n")

            # Option type split
            f.write("### By Option Type\n\n")
            f.write("| Type | Trades | Money P&L |\n")
            f.write("|------|--------|----------|\n")
            f.write(f"| CE | {len(ce_df)} | "
                    f"Rs {ce_df['money_pnl'].sum():,.2f} |\n")
            f.write(f"| PE | {len(pe_df)} | "
                    f"Rs {pe_df['money_pnl'].sum():,.2f} |\n\n")

            # Entry fill breakdown
            f.write("### Entry Fill Breakdown\n\n")
            f.write("| Parts Filled | Count |\n")
            f.write("|-------------|-------|\n")
            for parts, count in r['parts_counts'].items():
                total_lvls = int(trades_df['total_levels'].iloc[0])
                f.write(f"| {int(parts)}/{total_lvls} | {count} |\n")
            f.write(f"| Avg Parts | {r['avg_parts']:.2f} |\n\n")

    logger.info(f"Summary saved to {output_path}")
