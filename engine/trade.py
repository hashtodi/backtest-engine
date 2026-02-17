"""
Trade and PositionPart classes.

A Trade represents a complete trade cycle:
  1. Signal fires -> base_price set, observation starts
  2. Entry levels calculated from strategy config
  3. Parts filled as price reaches each level (same day only)
  4. Exit on SL / TP / EOD

Supports:
  - Variable number of staggered entry levels
  - Buy or sell direction
  - Capital % allocation per level
  - P&L always based on weighted avg entry price
"""

from typing import Dict, List, Optional


# ============================================
# POSITION PART (one leg of staggered entry)
# ============================================
class PositionPart:
    """One part of a staggered entry."""

    def __init__(self, level_num: int, entry_time, entry_price: float, capital_pct: float):
        self.level_num = level_num      # 1-based index
        self.entry_time = entry_time    # datetime of fill
        self.entry_price = entry_price  # price at which we entered
        self.capital_pct = capital_pct  # % of total capital for this level


# ============================================
# ENTRY LEVEL (target price for one staggered part)
# ============================================
class EntryLevel:
    """One staggered entry level with its target price."""

    def __init__(self, level_num: int, pct_from_base: float, capital_pct: float, target_price: float):
        self.level_num = level_num          # 1-based index
        self.pct_from_base = pct_from_base  # e.g., 5 means +5% from base
        self.capital_pct = capital_pct      # e.g., 33.33
        self.target_price = target_price    # actual price to trigger entry
        self.filled = False                 # whether this level has been filled


# ============================================
# TRADE (complete trade cycle)
# ============================================
class Trade:
    """
    A complete trade cycle with configurable staggered entries.

    Lifecycle:
      1. Signal fires -> base_price set, entry levels calculated
      2. Parts filled as price reaches each level (same day only)
      3. Exit on SL / TP / EOD (based on weighted avg entry price)
    """

    def __init__(
        self,
        signal_time,
        base_price: float,
        option_type: str,
        strike: float,
        expiry_type: str,
        expiry_code: int,
        instrument: str,
        direction: str,
        entry_levels_config: List[Dict],
        lot_size: int,
    ):
        # Signal info
        self.signal_time = signal_time
        self.base_price = base_price
        self.option_type = option_type
        self.strike = strike
        self.expiry_type = expiry_type
        self.expiry_code = expiry_code
        self.instrument = instrument
        self.direction = direction  # "sell" or "buy"
        self.lot_size = lot_size

        # Build entry levels from strategy config
        # For sell: entry triggers when price rises above base (we sell high)
        # For buy: entry triggers when price drops below base (we buy low)
        self.entry_levels: List[EntryLevel] = []
        for i, level_cfg in enumerate(entry_levels_config):
            pct = level_cfg['pct_above_base']
            cap_pct = level_cfg['capital_pct']

            if direction == 'sell':
                # Sell: enter when price rises by pct%
                target = base_price * (1 + pct / 100)
            else:
                # Buy: enter when price drops by pct%
                target = base_price * (1 - pct / 100)

            self.entry_levels.append(EntryLevel(i + 1, pct, cap_pct, target))

        # Parts tracking (filled entries)
        self.parts: List[PositionPart] = []

        # Exit info
        self.exit_time = None
        self.exit_price = None
        self.exit_reason = None
        self.status = 'WAITING_ENTRY'

        # P&L (set on close)
        self.total_pnl = 0.0
        self.total_pnl_pct = 0.0

    @property
    def num_levels(self) -> int:
        """Total number of staggered entry levels."""
        return len(self.entry_levels)

    def get_unfilled_levels(self) -> List[EntryLevel]:
        """Return entry levels that haven't been filled yet."""
        return [lvl for lvl in self.entry_levels if not lvl.filled]

    def get_next_unfilled_level(self) -> Optional[EntryLevel]:
        """Return the next unfilled level (levels fill in order)."""
        for lvl in self.entry_levels:
            if not lvl.filled:
                return lvl
        return None

    def add_entry(self, level: EntryLevel, entry_time, entry_price: float):
        """Fill one entry level."""
        part = PositionPart(level.level_num, entry_time, entry_price, level.capital_pct)
        self.parts.append(part)
        level.filled = True

        # Update status
        all_filled = all(lvl.filled for lvl in self.entry_levels)
        self.status = 'FULL_POSITION' if all_filled else 'PARTIAL_POSITION'

    def get_avg_entry_price(self) -> Optional[float]:
        """Weighted average entry price across filled parts (by capital_pct)."""
        if not self.parts:
            return None
        total_weighted = sum(p.entry_price * p.capital_pct for p in self.parts)
        total_weight = sum(p.capital_pct for p in self.parts)
        return total_weighted / total_weight if total_weight > 0 else None

    def has_position(self) -> bool:
        """True if at least one entry level has been filled."""
        return len(self.parts) > 0

    def close_trade(self, exit_time, exit_price: float, exit_reason: str):
        """
        Close entire position.

        P&L depends on direction:
          - sell: profit = avg_entry - exit_price (profit when price drops)
          - buy: profit = exit_price - avg_entry (profit when price rises)
        """
        if not self.parts:
            return

        self.exit_time = exit_time
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.status = 'CLOSED'

        avg = self.get_avg_entry_price()
        if avg and avg > 0:
            if self.direction == 'sell':
                self.total_pnl = avg - exit_price
            else:
                self.total_pnl = exit_price - avg
            self.total_pnl_pct = (self.total_pnl / avg) * 100
        else:
            self.total_pnl = 0.0
            self.total_pnl_pct = 0.0

    def get_money_pnl(self) -> float:
        """Actual money P&L = option price P&L * lot size."""
        return self.total_pnl * self.lot_size

    def to_dict(self) -> Dict:
        """Flat dictionary for CSV export. Supports up to 5 entry levels."""
        avg = self.get_avg_entry_price()
        d = {
            'instrument': self.instrument,
            'direction': self.direction,
            'option_type': self.option_type,
            'strike': self.strike,
            'expiry_type': self.expiry_type,
            'expiry_code': self.expiry_code,
            'signal_time': self.signal_time,
            'base_price': self.base_price,
            'parts_filled': len(self.parts),
            'total_levels': self.num_levels,
            'avg_entry_price': avg,
            'exit_time': self.exit_time,
            'exit_price': self.exit_price,
            'exit_reason': self.exit_reason,
            'pnl': self.total_pnl,
            'pnl_pct': self.total_pnl_pct,
            'money_pnl': self.total_pnl * self.lot_size,
            'lot_size': self.lot_size,
            'status': self.status,
        }

        # Add entry level details (target prices)
        for i, lvl in enumerate(self.entry_levels):
            d[f'level_{i+1}_target'] = lvl.target_price
            d[f'level_{i+1}_pct'] = lvl.pct_from_base
            d[f'level_{i+1}_capital_pct'] = lvl.capital_pct

        # Add filled part details (actual fills)
        for i, part in enumerate(self.parts):
            d[f'part_{i+1}_time'] = part.entry_time
            d[f'part_{i+1}_price'] = part.entry_price

        return d
