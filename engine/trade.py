"""
Trade and PositionPart classes.

A Trade represents a complete single-leg trade cycle:
  1. Signal fires -> base_price set, observation starts
  2. Entry levels calculated from strategy config
  3. Parts filled as price reaches each level (same day only)
  4. Exit on SL / TP / EOD

A StraddleTrade represents a combined CE+PE straddle trade:
  1. Signal fires on combined straddle price (CE close + PE close)
  2. Both ATM CE and PE are sold (or bought) simultaneously
  3. SL/TP tracked on combined straddle price
  4. Both legs exit together

Supports:
  - Variable number of staggered entry levels
  - Buy or sell direction
  - Capital % allocation per level
  - P&L always based on weighted avg entry price
"""

from typing import Dict, List, Optional, Tuple


def parse_entry_config(strategy: Dict) -> Tuple[List[Dict], Optional[str]]:
    """
    Parse the 'entry' dict from a strategy config into internal format.

    New JSON format:
      "entry": {"type": "direct"}
      "entry": {"type": "staggered", "levels": [{"pct_from_base": 5, ...}]}
      "entry": {"type": "indicator_level", "indicator": "opt_st_3_10_value"}

    Returns:
        (entry_levels_config, entry_indicator)
        - entry_levels_config: list of dicts for Trade constructor
        - entry_indicator: indicator column name, or None
    """
    entry = strategy.get("entry", {})
    entry_type = entry.get("type", "direct")

    if entry_type == "indicator_level":
        levels = [{"pct_from_base": 0, "capital_pct": 100.0}]
        return levels, entry.get("indicator")

    if entry_type == "staggered":
        return entry.get("levels", []), None

    # "direct" — single level at base price, 100% capital
    return [{"pct_from_base": 0, "capital_pct": 100.0}], None


def parse_exit_config(strategy: Dict) -> Dict:
    """
    Parse exit config from strategy, with auto-migration for old format.

    Old format (flat fields):
        {"stop_loss_pct": 20, "target_pct": 10}

    New format (structured):
        {"exit": {"stop_loss": {"source": "percentage", "value": 20}, ...}}

    Returns normalized dict:
        {"stop_loss": {"source": ..., ...}, "target": {"source": ..., ...}}
    """
    if "exit" in strategy:
        cfg = strategy["exit"]
    else:
        # Auto-migrate old flat config
        cfg = {
            "stop_loss": {
                "source": "percentage",
                "value": strategy.get("stop_loss_pct", 20),
            },
            "target": {
                "source": "percentage",
                "value": strategy.get("target_pct", 10),
            },
        }

    sl = cfg.get("stop_loss", {"source": "percentage", "value": 20})
    tp = cfg.get("target", {"source": "percentage", "value": 10})

    # Validate: both can't be ratio (circular dependency)
    if sl.get("source") == "ratio" and tp.get("source") == "ratio":
        raise ValueError("Exit config invalid: both stop_loss and target cannot be 'ratio' (circular)")

    # Validate: straddle mode cannot use indicator/ratio exits
    if strategy.get("trade_mode") == "straddle":
        for label, side in [("stop_loss", sl), ("target", tp)]:
            if side.get("source") in ("indicator", "ratio"):
                raise ValueError(
                    f"Exit config invalid: {label} source '{side['source']}' "
                    f"not supported in straddle mode"
                )

    return {"stop_loss": sl, "target": tp}


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
            pct = level_cfg['pct_from_base']
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
        self.last_entry_time = None  # candle when latest part was filled

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

    def update_entry_target(self, new_price: float):
        """
        Update the target price of the next unfilled entry level.

        Used by "Indicator Level" entry type where the limit order price
        changes each minute as the indicator recalculates.
        """
        level = self.get_next_unfilled_level()
        if level is not None:
            level.target_price = new_price

    def add_entry(self, level: EntryLevel, entry_time, entry_price: float):
        """Fill one entry level."""
        part = PositionPart(level.level_num, entry_time, entry_price, level.capital_pct)
        self.parts.append(part)
        level.filled = True

        # Track the candle where the latest part was filled.
        # Exit checks should skip this candle because entry is at close
        # and the candle's high/low happened before the close.
        self.last_entry_time = entry_time

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


# ============================================
# STRADDLE TRADE (combined CE + PE)
# ============================================
class StraddleTrade:
    """
    A combined straddle trade that sells (or buys) both ATM CE and PE together.

    Lifecycle:
      1. Signal fires on combined straddle price (CE close + PE close)
      2. Both legs enter simultaneously at their individual ATM prices
      3. SL/TP tracked on combined straddle price
      4. Both legs exit together

    P&L = (CE entry - CE exit) + (PE entry - PE exit) for sell direction.
    """

    def __init__(
        self,
        signal_time,
        ce_strike: float,
        pe_strike: float,
        ce_entry_price: float,
        pe_entry_price: float,
        expiry_type: str,
        expiry_code: int,
        instrument: str,
        direction: str,
        lot_size: int,
    ):
        self.signal_time = signal_time
        self.instrument = instrument
        self.direction = direction  # "sell" or "buy"
        self.lot_size = lot_size
        self.expiry_type = expiry_type
        self.expiry_code = expiry_code

        # CE leg
        self.ce_strike = ce_strike
        self.ce_entry_price = ce_entry_price
        self.ce_exit_price: Optional[float] = None

        # PE leg
        self.pe_strike = pe_strike
        self.pe_entry_price = pe_entry_price
        self.pe_exit_price: Optional[float] = None

        # Combined straddle entry = CE + PE
        self.straddle_entry = ce_entry_price + pe_entry_price

        # Entry time (same candle for both legs)
        self.entry_time = signal_time
        self.last_entry_time = signal_time

        # Exit info
        self.exit_time = None
        self.exit_reason = None
        self.status = 'FULL_POSITION'

        # P&L (set on close)
        self.total_pnl = 0.0
        self.total_pnl_pct = 0.0

    def has_position(self) -> bool:
        """Always True once created (both legs enter immediately)."""
        return self.status in ('FULL_POSITION',)

    def close_trade(self, exit_time, ce_exit: float, pe_exit: float, exit_reason: str):
        """
        Close both legs of the straddle.

        For sell: profit = entry - exit (profit when premium drops).
        For buy:  profit = exit - entry (profit when premium rises).
        """
        self.exit_time = exit_time
        self.ce_exit_price = ce_exit
        self.pe_exit_price = pe_exit
        self.exit_reason = exit_reason
        self.status = 'CLOSED'

        straddle_exit = ce_exit + pe_exit
        if self.direction == 'sell':
            self.total_pnl = self.straddle_entry - straddle_exit
        else:
            self.total_pnl = straddle_exit - self.straddle_entry

        if self.straddle_entry > 0:
            self.total_pnl_pct = (self.total_pnl / self.straddle_entry) * 100
        else:
            self.total_pnl_pct = 0.0

    def get_money_pnl(self) -> float:
        """Actual money P&L = combined option pnl * lot size."""
        return self.total_pnl * self.lot_size

    def to_dict(self) -> Dict:
        """Flat dictionary for CSV export."""
        straddle_exit = None
        if self.ce_exit_price is not None and self.pe_exit_price is not None:
            straddle_exit = self.ce_exit_price + self.pe_exit_price

        return {
            'instrument': self.instrument,
            'direction': self.direction,
            'option_type': 'STRADDLE',
            'strike': self.ce_strike,
            'expiry_type': self.expiry_type,
            'expiry_code': self.expiry_code,
            'signal_time': self.signal_time,
            'base_price': self.straddle_entry,
            'parts_filled': 1,
            'total_levels': 1,
            'avg_entry_price': self.straddle_entry,
            'exit_time': self.exit_time,
            'exit_price': straddle_exit,
            'exit_reason': self.exit_reason,
            'pnl': self.total_pnl,
            'pnl_pct': self.total_pnl_pct,
            'money_pnl': self.total_pnl * self.lot_size,
            'lot_size': self.lot_size,
            'status': self.status,
            # Leg details
            'ce_strike': self.ce_strike,
            'pe_strike': self.pe_strike,
            'ce_entry_price': self.ce_entry_price,
            'pe_entry_price': self.pe_entry_price,
            'ce_exit_price': self.ce_exit_price,
            'pe_exit_price': self.pe_exit_price,
        }
