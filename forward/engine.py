"""
Forward test engine.

Core loop: fetch live data -> buffer prices -> calculate indicators
-> check signals -> manage virtual trades (paper trading).

Reuses existing components:
  - indicators/*.py           for indicator calculations
  - engine/signals.py         for signal condition checking
  - engine/trade.py           for trade lifecycle management
  - dhan_datafeed.py          for REST API data (warmup, expiry, fallback)
  - ws_feed.py                for real-time WebSocket tick data (primary)

Split into submodules:
  - forward/price_buffer.py   PriceBuffer class
  - forward/warmup.py         multi-day warmup logic
  - forward/tick_checker.py   tick-level SL/TP/entry functions
  - forward/helpers.py        event builders, timestamp utils
"""

import math
import time
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional, Callable

import config
from indicators import get_indicator
from indicators.base import Indicator
from engine.signals import check_signal
from engine.trade import Trade, parse_entry_config
from ws_feed import (
    DhanWebSocketFeed, build_instrument_tuple,
    get_option_ws_exchange, EXCHANGE_WS_MAP,
)
from dhanhq.marketfeed import IDX, Ticker

from forward.helpers import IST, make_event
from forward.price_buffer import PriceBuffer
from forward.warmup import run_warmup, WARMUP_LOOKBACK_DAYS
from forward.tick_checker import (
    TickChecker, _check_staggered_entry, _check_exit,
    check_indicator_entry,
)

logger = logging.getLogger(__name__)

# Number of strikes above and below ATM to warm up and subscribe via WebSocket.
WARMUP_STRIKE_RANGE = 20


class ForwardTestEngine:
    """
    Paper-trading engine that runs once per minute during market hours.

    Mirrors the backtest engine logic:
      - Independent CE and PE tracks
      - Signal detection on ATM data
      - Staggered entry
      - SL / TP / EOD exit
    """

    def __init__(self, strategy: dict, data_feed, instrument: str,
                 lot_size: int, telegram=None, use_websocket: bool = True,
                 warmup_strike_range: int = WARMUP_STRIKE_RANGE):
        self.strategy = strategy
        self.data_feed = data_feed
        self.instrument = instrument
        self.lot_size = lot_size
        self.telegram = telegram
        self.use_websocket = use_websocket
        self.warmup_strike_range = warmup_strike_range

        # Parse strategy parameters
        self.direction = strategy.get("direction", "sell")
        self.stop_loss_pct = strategy.get("stop_loss_pct", 20)
        self.target_pct = strategy.get("target_pct", 10)
        self.signal_conditions = strategy.get("signal_conditions", [])
        self.signal_logic = strategy.get("signal_logic", "AND")
        # Entry config: parsed from strategy["entry"] dict
        self.entry_levels_config, self.entry_indicator = parse_entry_config(strategy)
        self.max_trades_per_day = strategy.get("max_trades_per_day", None)

        self.entry_time = datetime.strptime(
            strategy.get("trading_start", "09:30"), "%H:%M"
        ).time()
        self.exit_time = datetime.strptime(
            strategy.get("trading_end", "14:30"), "%H:%M"
        ).time()

        # Build indicator instances (one per config entry)
        self.indicator_configs = strategy.get("indicators", [])
        self.indicators: List[Indicator] = []
        for cfg in self.indicator_configs:
            params = {k: v for k, v in cfg.items()
                      if k not in ("type", "name", "price_source")}
            self.indicators.append(
                get_indicator(cfg["type"], name=cfg["name"], **params)
            )

        # Price buffer
        self.buffer = PriceBuffer()

        # Active trades — one per option type, just like the backtest
        self.active_ce: Optional[Trade] = None
        self.active_pe: Optional[Trade] = None

        # Completed trades
        self.completed_trades: List[Trade] = []

        # Day counter for max_trades_per_day
        self.day_trade_count = 0
        self._current_date: Optional[object] = None

        # Cached expiry string (refreshed each day)
        self._expiry: Optional[str] = None

        # Current ATM strike (updated each minute)
        self._atm_strike: Optional[int] = None

        # WebSocket feed (created after warmup if use_websocket=True)
        self._ws_feed: Optional[DhanWebSocketFeed] = None

        # Cached indicator rows from last run_one_minute().
        # Used by tick-level signal checking so we don't recalculate
        # indicators every second — only the live price changes.
        self._cached_indicator_rows: Dict[str, dict] = {}

        # Previous tick LTP per option type, for crossover detection
        # between consecutive ticks.
        self._prev_tick_ltp: Dict[str, float] = {}

        # Previous tick LTP per option type for indicator-level entry.
        # Tracks the last LTP seen so we can detect when price crosses
        # through the indicator level between two ticks.
        self._prev_entry_ltp: Dict[str, float] = {}

        # Tick-level checker (created after init, uses self._get_ws_option_ltp)
        self._tick_checker = TickChecker(
            direction=self.direction,
            stop_loss_pct=self.stop_loss_pct,
            target_pct=self.target_pct,
            instrument=self.instrument,
            get_ws_option_ltp_fn=self._get_ws_option_ltp,
            telegram=self.telegram,
        )

        logger.info(
            f"ForwardTestEngine initialised | {instrument} | "
            f"direction={self.direction} | SL={self.stop_loss_pct}% | "
            f"TP={self.target_pct}% | indicators={len(self.indicators)} | "
            f"websocket={'enabled' if use_websocket else 'disabled'}"
        )

    # ------------------------------------------
    # WARMUP
    # ------------------------------------------
    def warmup(self) -> List[dict]:
        """
        Fetch multi-day intraday data and pre-fill the PriceBuffer.
        Delegates to forward.warmup.run_warmup().
        """
        result = run_warmup(
            self.data_feed, self.instrument, self.buffer,
            self.warmup_strike_range,
        )
        self._atm_strike = result.get("atm_strike")
        self._expiry = result.get("expiry")
        self._current_date = datetime.now(IST).date()
        return result["events"]

    # ------------------------------------------
    # WEBSOCKET FEED MANAGEMENT
    # ------------------------------------------
    def _build_ws_instruments(self) -> list:
        """
        Build the WebSocket instrument list from warmed-up security IDs.
        """
        instruments = []

        # 1. Spot index
        spot_info = self.data_feed.SECURITY_IDS.get(self.instrument)
        if spot_info:
            ws_exch = EXCHANGE_WS_MAP.get(spot_info['exchange'], IDX)
            instruments.append(
                (ws_exch, str(spot_info['security_id']), Ticker)
            )

        # 2. All option contracts from the security ID cache
        opt_ws_exch = get_option_ws_exchange(self.instrument)
        prefix = f"{self.instrument}_"
        for cache_key, sec_id in self.data_feed.option_symbols_cache.items():
            if cache_key.startswith(prefix):
                instruments.append((opt_ws_exch, str(sec_id), Ticker))

        logger.info(
            f"Built WebSocket instrument list: {len(instruments)} total "
            f"(1 spot + {len(instruments) - 1} options)"
        )
        return instruments

    def _init_websocket(self) -> List[dict]:
        """Create and start the WebSocket feed after warmup."""
        events: List[dict] = []

        if not self.use_websocket:
            return events

        try:
            instruments = self._build_ws_instruments()
            if len(instruments) < 2:
                events.append(make_event(
                    "info",
                    "WebSocket skipped: not enough instruments to subscribe"
                ))
                return events

            self._ws_feed = DhanWebSocketFeed(
                self.data_feed.client_id,
                self.data_feed.access_token,
            )
            self._ws_feed.start(instruments)

            if self._ws_feed.is_connected:
                events.append(make_event(
                    "info",
                    f"WebSocket connected: {len(instruments)} instruments "
                    f"(spot + {len(instruments) - 1} options). "
                    f"Live tick data active."
                ))
            else:
                events.append(make_event(
                    "info",
                    "WebSocket connecting in background. "
                    "Using REST API as fallback until connected."
                ))

        except Exception as e:
            logger.error(f"WebSocket init failed: {e}", exc_info=True)
            events.append(make_event(
                "error",
                f"WebSocket init failed: {e}. Using REST API only."
            ))
            self._ws_feed = None

        return events

    def _stop_websocket(self):
        """Stop the WebSocket feed if running."""
        if self._ws_feed:
            try:
                self._ws_feed.stop()
            except Exception as e:
                logger.warning(f"Error stopping WebSocket: {e}")
            self._ws_feed = None

    def _check_ws_resubscribe(self, atm_strike: int):
        """Resubscribe if the new ATM is outside our WS subscription range."""
        if not self._ws_feed or not self._ws_feed.is_connected:
            return

        for opt_type in ("CE", "PE"):
            sec_id = self.data_feed.get_option_security_id(
                self.instrument, atm_strike, self._expiry, opt_type
            )
            if sec_id and not self._ws_feed.is_subscribed(sec_id):
                logger.info(
                    f"ATM {atm_strike} {opt_type} (sec_id={sec_id}) "
                    f"not in WebSocket subscription. Resubscribing..."
                )
                new_instruments = self._build_ws_instruments()
                self._ws_feed.resubscribe(new_instruments)
                return

    # ------------------------------------------
    # INDICATOR CALCULATION
    # ------------------------------------------
    def _calc_indicator_row(self, option_type: str) -> dict:
        """
        Calculate all indicators and return a row dict for signal checking.

        The row dict has the same keys that engine/signals.check_signal() expects.
        """
        row: dict = {}

        latest = self.buffer.get_option_bar(option_type, -1)
        prev_bar = self.buffer.get_option_bar(option_type, -2)
        if latest is None:
            return row

        row["close"] = latest["close"]
        row["close_prev"] = prev_bar["close"] if prev_bar else latest["close"]
        row["high"] = latest["high"]
        row["high_prev"] = prev_bar["high"] if prev_bar else latest["high"]
        row["low"] = latest["low"]
        row["low_prev"] = prev_bar["low"] if prev_bar else latest["low"]
        row["open"] = latest.get("open", latest["close"])
        row["open_prev"] = (prev_bar.get("open", prev_bar["close"])
                            if prev_bar else latest.get("open", latest["close"]))
        row["strike"] = self._atm_strike
        row["option_type"] = option_type
        row["moneyness"] = "ATM"

        spot_series = self.buffer.get_spot_series()
        option_series = self.buffer.get_option_series(option_type)
        option_high = self.buffer.get_option_high_series(option_type)
        option_low = self.buffer.get_option_low_series(option_type)

        for ind, cfg in zip(self.indicators, self.indicator_configs):
            price_source = cfg.get("price_source", "option")
            series = spot_series if price_source == "spot" else option_series

            if len(series) < 2:
                continue

            try:
                # SuperTrend needs high/low for True Range calculation
                if cfg['type'] == 'SUPERTREND' and price_source != "spot":
                    result = ind.calculate(
                        series, high=option_high, low=option_low,
                    )
                else:
                    result = ind.calculate(series)
            except Exception as e:
                logger.warning(f"Indicator {ind.name} calc error: {e}")
                continue

            if isinstance(result, dict):
                for key, sub_series in result.items():
                    col = f"{ind.name}_{key}"
                    row[col] = (sub_series.iloc[-1]
                                if len(sub_series) > 0 else float("nan"))
                    row[f"{col}_prev"] = (sub_series.iloc[-2]
                                          if len(sub_series) > 1
                                          else float("nan"))
            else:
                row[ind.name] = (result.iloc[-1]
                                 if len(result) > 0 else float("nan"))
                row[f"{ind.name}_prev"] = (result.iloc[-2]
                                           if len(result) > 1
                                           else float("nan"))

        return row

    # ------------------------------------------
    # LOGGING HELPERS
    # ------------------------------------------
    def _format_atm_indicators(self, indicator_rows: dict,
                               atm_strike: int) -> str:
        """Build a compact ATM indicator string matching backtest log format."""
        if not indicator_rows:
            return ""

        ind_names = []
        for cfg in self.indicator_configs:
            name = cfg["name"]
            if cfg["type"] == "MACD":
                ind_names.extend([
                    f"{name}_macd", f"{name}_signal", f"{name}_histogram"
                ])
            elif cfg["type"] == "BOLLINGER":
                ind_names.extend([
                    f"{name}_upper", f"{name}_middle", f"{name}_lower"
                ])
            else:
                ind_names.append(name)

        parts = []
        for opt_type in ("CE", "PE"):
            row = indicator_rows.get(opt_type, {})
            if not row:
                continue
            vals = []
            for name in ind_names:
                v = row.get(name)
                if v is not None and not (isinstance(v, float) and v != v):
                    vals.append(f"{name}={v:.2f}")
            if vals:
                parts.append(
                    f"ATM {opt_type} {atm_strike} {' '.join(vals)}"
                )

        return " | ".join(parts)

    def _format_trade_status(self, opt_type: str, trade: Optional[Trade],
                             trade_ltp: Optional[float]) -> str:
        """Build trade status string matching backtest log format."""
        if trade is None:
            return "idle"

        strike = trade.strike
        ltp_str = f"{trade_ltp:.2f}" if trade_ltp is not None else "--"

        if trade.status == "WAITING_ENTRY":
            if self.entry_indicator:
                # Show the cached indicator value as the limit level
                cached_row = self._cached_indicator_rows.get(opt_type, {})
                ind_val = cached_row.get(self.entry_indicator)
                lvl_str = (f"waiting limit={ind_val:.2f}"
                           if ind_val is not None and not math.isnan(ind_val)
                           else "waiting limit=NaN")
            else:
                next_lvl = trade.get_next_unfilled_level()
                lvl_str = (f"waiting L{next_lvl.level_num}={next_lvl.target_price:.2f}"
                           if next_lvl else "waiting")
            return (
                f"observing {strike} {opt_type} | "
                f"close={ltp_str} | {lvl_str}"
            )

        if trade.status in ("PARTIAL_POSITION", "FULL_POSITION"):
            filled = len(trade.parts)
            total = trade.num_levels
            avg = trade.get_avg_entry_price() or 0

            if self.direction == "sell":
                sl = avg * (1 + self.stop_loss_pct / 100)
                tp = avg * (1 - self.target_pct / 100)
            else:
                sl = avg * (1 - self.stop_loss_pct / 100)
                tp = avg * (1 + self.target_pct / 100)

            next_lvl = trade.get_next_unfilled_level()
            next_str = ""
            if next_lvl:
                next_str = (f" | waiting L{next_lvl.level_num}="
                            f"{next_lvl.target_price:.2f}")

            return (
                f"in position {strike} {opt_type} ({filled}/{total}) | "
                f"close={ltp_str} | avg={avg:.2f} "
                f"SL={sl:.2f} TP={tp:.2f}{next_str}"
            )

        return "idle"

    def _get_trade_ltp(self, trade: Optional[Trade],
                       atm_strike: int, atm_ltp: Optional[float],
                       opt_type: str) -> Optional[float]:
        """
        Get the current LTP for a trade's specific contract.

        If the trade's strike == current ATM, use the already-fetched ATM LTP.
        Otherwise, try WebSocket first, then REST API.
        """
        if trade is None or atm_ltp is None:
            return atm_ltp

        if trade.strike == atm_strike:
            return atm_ltp

        ltp = self._get_ws_option_ltp(trade.strike, opt_type)
        if ltp is not None:
            return ltp

        return atm_ltp

    def _format_indicator_summary(self, indicator_rows: dict) -> str:
        """Backward-compatible wrapper."""
        parts = []
        for opt_type in ("CE", "PE"):
            row = indicator_rows.get(opt_type, {})
            if not row:
                continue
            vals = []
            for cfg in self.indicator_configs:
                name = cfg["name"]
                v = row.get(name)
                if v is not None and not (isinstance(v, float) and v != v):
                    vals.append(f"{name}={v:.2f}")
            if vals:
                parts.append(f"{opt_type}: {' '.join(vals)}")

        if not parts:
            return ""
        return " | " + " | ".join(parts)

    # ------------------------------------------
    # FETCH LIVE PRICES
    # ------------------------------------------
    def _fetch_prices(self, now: datetime) -> Optional[dict]:
        """
        Fetch spot + ATM CE/PE prices.

        Tries WebSocket first (instant, no rate limit).
        Falls back to REST API if WebSocket is unavailable.
        """
        if self._ws_feed and self._ws_feed.is_connected:
            result = self._fetch_prices_ws(now)
            if result is not None:
                return result

        return self._fetch_prices_rest(now)

    def _fetch_prices_ws(self, now: datetime) -> Optional[dict]:
        """Fetch prices from WebSocket tick cache."""
        spot_info = self.data_feed.SECURITY_IDS.get(self.instrument)
        if not spot_info:
            return None
        spot = self._ws_feed.get_ltp(spot_info['security_id'])
        if spot is None:
            return None

        atm_strike = self.data_feed.get_atm_strike(spot, self.instrument)

        today = now.date()
        if self._expiry is None or self._current_date != today:
            self._expiry = self.data_feed.get_weekly_expiry(self.instrument)
            self._current_date = today
            self.day_trade_count = 0
            logger.info(
                f"Expiry for {self.instrument}: {self._expiry} | "
                f"Date: {today}"
            )

        ce_ltp = self._get_ws_option_ltp(atm_strike, "CE")
        pe_ltp = self._get_ws_option_ltp(atm_strike, "PE")

        ce_sec_id = self.data_feed.get_option_security_id(
            self.instrument, atm_strike, self._expiry, "CE"
        )
        pe_sec_id = self.data_feed.get_option_security_id(
            self.instrument, atm_strike, self._expiry, "PE"
        )

        self._check_ws_resubscribe(atm_strike)

        return {
            "spot": spot,
            "atm_strike": atm_strike,
            "expiry": self._expiry,
            "ce_ltp": ce_ltp,
            "pe_ltp": pe_ltp,
            "ce_sec_id": ce_sec_id,
            "pe_sec_id": pe_sec_id,
        }

    def _get_ws_option_ltp(self, strike: int,
                           option_type: str) -> Optional[float]:
        """Get option LTP from WebSocket. Falls back to REST if unavailable."""
        sec_id = self.data_feed.get_option_security_id(
            self.instrument, strike, self._expiry, option_type
        )
        if not sec_id:
            return None

        if self._ws_feed and self._ws_feed.is_connected:
            ltp = self._ws_feed.get_ltp(sec_id)
            if ltp is not None:
                return ltp

        try:
            data = self.data_feed.get_option_price(
                self.instrument, strike, self._expiry, option_type
            )
            if data and data.get("ltp"):
                return data["ltp"]
        except Exception as e:
            logger.warning(
                f"REST fallback failed for {strike} {option_type}: {e}"
            )
        return None

    def _fetch_prices_rest(self, now: datetime) -> Optional[dict]:
        """Fetch prices from REST API (fallback when WS is unavailable)."""
        spot = self.data_feed.get_spot_price(self.instrument)
        if spot is None:
            return None

        atm_strike = self.data_feed.get_atm_strike(spot, self.instrument)

        today = now.date()
        if self._expiry is None or self._current_date != today:
            self._expiry = self.data_feed.get_weekly_expiry(self.instrument)
            self._current_date = today
            self.day_trade_count = 0
            logger.info(
                f"Expiry for {self.instrument}: {self._expiry} | "
                f"Date: {today}"
            )

        ce_data = self.data_feed.get_option_price(
            self.instrument, atm_strike, self._expiry, "CE"
        )
        ce_ltp = ce_data["ltp"] if ce_data else None

        pe_data = self.data_feed.get_option_price(
            self.instrument, atm_strike, self._expiry, "PE"
        )
        pe_ltp = pe_data["ltp"] if pe_data else None

        return {
            "spot": spot,
            "atm_strike": atm_strike,
            "expiry": self._expiry,
            "ce_ltp": ce_ltp,
            "pe_ltp": pe_ltp,
            "ce_sec_id": None,
            "pe_sec_id": None,
        }

    # ------------------------------------------
    # RUN ONE MINUTE
    # ------------------------------------------
    def run_one_minute(self) -> List[dict]:
        """
        Execute one minute of the forward test loop.

        Steps:
          1. Fetch live prices (spot + ATM CE + ATM PE)
          2. Append to price buffer
          3. Calculate indicators
          4. Check signals -> create Trade observations
          5. Check staggered entry for active trades
          6. Check SL / TP / EOD exit
        """
        now = datetime.now(IST)
        t_only = now.time()
        events: List[dict] = []

        if t_only < self.entry_time or t_only > self.exit_time:
            return events

        is_exit_time = (t_only >= self.exit_time)

        # --- Fetch live prices ---
        prices = self._fetch_prices(now)
        if prices is None:
            events.append(make_event(
                "error", f"Failed to fetch spot price for {self.instrument}"
            ))
            return events

        spot = prices["spot"]
        atm_strike = prices["atm_strike"]
        self._atm_strike = atm_strike

        expiry = prices["expiry"] or ""
        self.buffer.set_expiry(expiry)

        # Log ATM shift
        new_ce_key = f"{atm_strike}_CE"
        prev_ce_key = self.buffer.get_current_key("CE")
        if prev_ce_key is not None and new_ce_key != prev_ce_key:
            events.append(make_event(
                "info",
                f"ATM shift: {prev_ce_key} -> "
                f"{new_ce_key} | spot={spot:.2f}"
            ))

        # --- Buffer prices (use real OHLC from candle aggregator) ---
        self.buffer.add_spot(now, spot)

        for opt_type, ltp_key, sid_key in [
            ("CE", "ce_ltp", "ce_sec_id"),
            ("PE", "pe_ltp", "pe_sec_id"),
        ]:
            ltp = prices.get(ltp_key)
            if ltp is None:
                continue
            sec_id = prices.get(sid_key)
            candle_high, candle_low, candle_open, candle_close = None, None, None, None
            if sec_id and self._ws_feed:
                candle = self._ws_feed.candles.get_completed_candle(sec_id)
                if candle:
                    candle_high = candle.get("high")
                    candle_low = candle.get("low")
                    candle_open = candle.get("open")
                    candle_close = candle.get("close")
            # Use the completed candle's close for the buffer bar.
            # This is the true last tick of the previous minute (matches
            # TradingView candle close). Fall back to live LTP if no
            # completed candle is available (REST fallback, first minute).
            bar_close = candle_close if candle_close is not None else ltp
            self.buffer.add_option(
                now, atm_strike, opt_type, bar_close,
                high=candle_high, low=candle_low, open_price=candle_open,
            )

        # --- Calculate indicator rows for both option types ---
        indicator_rows = {}
        for opt_type, ltp in [("CE", prices["ce_ltp"]),
                              ("PE", prices["pe_ltp"])]:
            if ltp is not None:
                indicator_rows[opt_type] = self._calc_indicator_row(opt_type)

        # Cache indicator rows for tick-level signal checking.
        # Tick checks reuse these cached values (indicators don't change intra-minute)
        # and overlay the live LTP as the price.
        self._cached_indicator_rows = dict(indicator_rows)

        # --- Resolve trade-specific LTPs ---
        trade_ltps: Dict[str, Optional[float]] = {}
        for opt_type, atm_ltp in [("CE", prices["ce_ltp"]),
                                   ("PE", prices["pe_ltp"])]:
            active = self.active_ce if opt_type == "CE" else self.active_pe
            trade_ltps[opt_type] = self._get_trade_ltp(
                active, atm_strike, atm_ltp, opt_type
            )

        # --- Process each option type (CE and PE independently) ---
        for opt_type, atm_ltp in [("CE", prices["ce_ltp"]),
                                   ("PE", prices["pe_ltp"])]:
            if atm_ltp is None:
                continue

            active = self.active_ce if opt_type == "CE" else self.active_pe
            row = indicator_rows.get(opt_type, {})
            trade_ltp = trade_ltps[opt_type]

            # --- SIGNAL DETECTION ---
            day_limit_hit = (
                self.max_trades_per_day is not None
                and self.day_trade_count >= self.max_trades_per_day
            )

            if not is_exit_time and active is None and not day_limit_hit:
                if row:
                    fired, reason = check_signal(
                        row, self.signal_conditions, self.signal_logic
                    )
                    if fired:
                        trade = Trade(
                            signal_time=now,
                            base_price=atm_ltp,
                            option_type=opt_type,
                            strike=atm_strike,
                            expiry_type="WEEK",
                            expiry_code=1,
                            instrument=self.instrument,
                            direction=self.direction,
                            entry_levels_config=self.entry_levels_config,
                            lot_size=self.lot_size,
                        )

                        if opt_type == "CE":
                            self.active_ce = trade
                        else:
                            self.active_pe = trade

                        self.day_trade_count += 1
                        active = trade
                        trade_ltp = atm_ltp
                        trade_ltps[opt_type] = atm_ltp

                        if self.entry_indicator:
                            ind_val = row.get(self.entry_indicator)
                            entry_info = (
                                f"entry_indicator={self.entry_indicator}"
                                f"({ind_val:.2f})" if ind_val is not None
                                and not math.isnan(ind_val)
                                else f"entry_indicator={self.entry_indicator}(NaN)"
                            )
                        else:
                            entry_info = " ".join(
                                f"L{lvl.level_num}={lvl.target_price:.2f}"
                                for lvl in trade.entry_levels
                            )
                        if self.direction == "sell":
                            sl_ref = atm_ltp * (1 + self.stop_loss_pct / 100)
                            tp_ref = atm_ltp * (1 - self.target_pct / 100)
                        else:
                            sl_ref = atm_ltp * (1 - self.stop_loss_pct / 100)
                            tp_ref = atm_ltp * (1 + self.target_pct / 100)
                        events.append(make_event(
                            "signal",
                            f"{opt_type} SIGNAL: {reason} | "
                            f"{atm_strike} {opt_type} | "
                            f"base={atm_ltp:.2f} | {entry_info} | "
                            f"SL~{sl_ref:.2f} TP~{tp_ref:.2f}",
                            option_type=opt_type,
                        ))

                        if self.telegram:
                            try:
                                self.telegram.send_message(
                                    f"<b>SIGNAL</b> {self.instrument} "
                                    f"{atm_strike} {opt_type}\n"
                                    f"{reason}\nBase: {atm_ltp:.2f}"
                                )
                            except Exception:
                                pass

            # --- ENTRY CHECK (staggered or indicator level) ---
            if (not is_exit_time and active is not None
                    and active.status in ("WAITING_ENTRY", "PARTIAL_POSITION")
                    and trade_ltp is not None):

                if self.entry_indicator:
                    # Indicator Level: get current indicator value from row,
                    # check if candle-level LTP range touched it.
                    ind_val = row.get(self.entry_indicator) if row else None
                    if ind_val is not None and not math.isnan(ind_val):
                        prev_ltp = self._prev_entry_ltp.get(opt_type, trade_ltp)
                        entry_events = check_indicator_entry(
                            active, prev_ltp, trade_ltp,
                            ind_val, now, self.direction,
                        )
                    else:
                        entry_events = []
                    # Always track prev LTP for next tick-level check
                    self._prev_entry_ltp[opt_type] = trade_ltp
                else:
                    entry_events = _check_staggered_entry(
                        active, trade_ltp, now, self.direction
                    )

                for msg in entry_events:
                    events.append(make_event(
                        "entry", f"{opt_type} {msg}", option_type=opt_type
                    ))
                    if self.telegram:
                        try:
                            self.telegram.send_message(
                                f"<b>ENTRY</b> {self.instrument} "
                                f"{opt_type}\n{msg}"
                            )
                        except Exception:
                            pass

            # --- EXIT CHECK (use shared function) ---
            if active is not None and trade_ltp is not None:
                closed, exit_msg = _check_exit(
                    active, trade_ltp, now, is_exit_time,
                    self.direction, self.stop_loss_pct, self.target_pct,
                )
                if closed:
                    self.completed_trades.append(active)
                    events.append(make_event(
                        "exit", f"{opt_type} {exit_msg}",
                        option_type=opt_type,
                    ))
                    if opt_type == "CE":
                        self.active_ce = None
                    else:
                        self.active_pe = None

                    if self.telegram:
                        try:
                            self.telegram.send_message(
                                f"<b>EXIT</b> {self.instrument} "
                                f"{opt_type}\n{exit_msg}"
                            )
                        except Exception:
                            pass

        # --- Info event: backtest-style format ---
        atm_ind_str = self._format_atm_indicators(indicator_rows, atm_strike)

        ce_status = self._format_trade_status(
            "CE", self.active_ce, trade_ltps.get("CE")
        )
        pe_status = self._format_trade_status(
            "PE", self.active_pe, trade_ltps.get("PE")
        )

        ce_price = prices['ce_ltp']
        pe_price = prices['pe_ltp']
        ce_str = f"{ce_price:.2f}" if ce_price else "--"
        pe_str = f"{pe_price:.2f}" if pe_price else "--"

        src = "WS" if (self._ws_feed and self._ws_feed.is_connected) else "REST"
        tick_info = ""
        if self._ws_feed and self._ws_feed.is_connected:
            tick_info = f" ticks={self._ws_feed.tick_count}"

        # OHLC info from candle aggregator
        ohlc_parts = []
        for opt_type, sid_key in [("CE", "ce_sec_id"), ("PE", "pe_sec_id")]:
            sec_id = prices.get(sid_key)
            if sec_id and self._ws_feed:
                candle = self._ws_feed.candles.get_completed_candle(sec_id)
                if candle:
                    ohlc_parts.append(
                        f"{opt_type} O={candle['open']:.1f} "
                        f"H={candle['high']:.1f} "
                        f"L={candle['low']:.1f} "
                        f"C={candle['close']:.1f} "
                        f"({candle['ticks']}t)"
                    )
        ohlc_str = " | ".join(ohlc_parts)

        line_parts = [
            f"[{now.strftime('%H:%M')}] [{src}] "
            f"spot={spot:.2f} ATM={atm_strike} "
            f"CE={ce_str} PE={pe_str}{tick_info}",
        ]
        if atm_ind_str:
            line_parts.append(atm_ind_str)
        if ohlc_str:
            line_parts.append(f"OHLC: {ohlc_str}")
        line_parts.append(f"CE: {ce_status}")
        line_parts.append(f"PE: {pe_status}")

        events.append(make_event("info", " | ".join(line_parts)))

        return events

    # ------------------------------------------
    # TICK-LEVEL SIGNAL CHECKING
    # ------------------------------------------
    def _check_tick_signals(self) -> List[dict]:
        """
        Check signals using live tick LTP + cached indicator values.

        Called every ~1 second between minute boundaries.
        Indicators stay fixed (from last run_one_minute); only price changes.
        This lets price-vs-indicator signals fire mid-candle instead of
        waiting for the next minute boundary.
        """
        events: List[dict] = []
        now = datetime.now(IST)

        # Skip if no cached indicators yet (first minute hasn't run)
        if not self._cached_indicator_rows:
            return events

        # Skip if no WebSocket feed (can't get live ticks)
        if not self._ws_feed or not self._ws_feed.is_connected:
            return events

        # Skip during exit time
        if now.time() >= self.exit_time:
            return events

        # Day trade limit
        day_limit_hit = (
            self.max_trades_per_day is not None
            and self.day_trade_count >= self.max_trades_per_day
        )
        if day_limit_hit:
            return events

        atm_strike = self._atm_strike
        if atm_strike is None:
            return events

        for opt_type in ("CE", "PE"):
            active = self.active_ce if opt_type == "CE" else self.active_pe
            if active is not None:
                continue  # trade already active for this type

            cached_row = self._cached_indicator_rows.get(opt_type)
            if not cached_row:
                continue

            # Get live LTP from WebSocket
            ltp = self._get_ws_option_ltp(atm_strike, opt_type)
            if ltp is None:
                continue

            # Build a signal-check row: live price + cached indicators.
            # Overlay price fields with live tick values.
            row = dict(cached_row)
            prev_ltp = self._prev_tick_ltp.get(opt_type, ltp)
            row["close"] = ltp
            row["close_prev"] = prev_ltp
            # For high/low/open at tick level, use LTP as the "current" value
            # since we don't have intra-second OHLC. Prev values stay from cache.
            row["high"] = ltp
            row["low"] = ltp
            row["open"] = ltp
            row["high_prev"] = prev_ltp
            row["low_prev"] = prev_ltp
            row["open_prev"] = prev_ltp

            # Update prev tick LTP for next check
            self._prev_tick_ltp[opt_type] = ltp

            # Run signal conditions
            fired, reason = check_signal(
                row, self.signal_conditions, self.signal_logic
            )
            if not fired:
                continue

            # Signal fired on tick — create trade
            trade = Trade(
                signal_time=now,
                base_price=ltp,
                option_type=opt_type,
                strike=atm_strike,
                expiry_type="WEEK",
                expiry_code=1,
                instrument=self.instrument,
                direction=self.direction,
                entry_levels_config=self.entry_levels_config,
                lot_size=self.lot_size,
            )

            if opt_type == "CE":
                self.active_ce = trade
            else:
                self.active_pe = trade

            self.day_trade_count += 1

            if self.entry_indicator:
                ind_val = cached_row.get(self.entry_indicator)
                entry_info = (
                    f"entry_indicator={self.entry_indicator}"
                    f"({ind_val:.2f})" if ind_val is not None
                    and not math.isnan(ind_val)
                    else f"entry_indicator={self.entry_indicator}(NaN)"
                )
            else:
                entry_info = " ".join(
                    f"L{lvl.level_num}={lvl.target_price:.2f}"
                    for lvl in trade.entry_levels
                )
            if self.direction == "sell":
                sl_ref = ltp * (1 + self.stop_loss_pct / 100)
                tp_ref = ltp * (1 - self.target_pct / 100)
            else:
                sl_ref = ltp * (1 - self.stop_loss_pct / 100)
                tp_ref = ltp * (1 + self.target_pct / 100)

            events.append(make_event(
                "signal",
                f"[TICK] {opt_type} SIGNAL: {reason} | "
                f"{atm_strike} {opt_type} | "
                f"base={ltp:.2f} | {entry_info} | "
                f"SL~{sl_ref:.2f} TP~{tp_ref:.2f}",
                option_type=opt_type,
            ))

            if self.telegram:
                try:
                    self.telegram.send_message(
                        f"<b>SIGNAL [TICK]</b> {self.instrument} "
                        f"{atm_strike} {opt_type}\n"
                        f"{reason}\nBase: {ltp:.2f}"
                    )
                except Exception:
                    pass

        return events

    def _check_tick_indicator_entry(self, _emit: Callable):
        """
        Tick-level indicator entry check.

        For each active trade in WAITING_ENTRY, check if the live tick LTP
        crossed through the cached indicator value since the last tick.
        Uses: min(prev_ltp, curr_ltp) <= indicator_value <= max(prev_ltp, curr_ltp)
        """
        if not self._ws_feed or not self._ws_feed.is_connected:
            return
        if not self._cached_indicator_rows:
            return

        now = datetime.now(IST)
        if now.time() >= self.exit_time:
            return

        for opt_type in ("CE", "PE"):
            active = self.active_ce if opt_type == "CE" else self.active_pe
            if active is None or active.status != "WAITING_ENTRY":
                continue

            # Get live LTP for the trade's specific contract
            ltp = self._get_ws_option_ltp(active.strike, opt_type)
            if ltp is None:
                continue

            # Get cached indicator value
            cached_row = self._cached_indicator_rows.get(opt_type, {})
            ind_val = cached_row.get(self.entry_indicator)
            if ind_val is None or math.isnan(ind_val):
                self._prev_entry_ltp[opt_type] = ltp
                continue

            prev_ltp = self._prev_entry_ltp.get(opt_type, ltp)

            # Check if price crossed through the indicator level
            entry_events = check_indicator_entry(
                active, prev_ltp, ltp, ind_val, now, self.direction
            )

            # Update prev LTP for next tick
            self._prev_entry_ltp[opt_type] = ltp

            for msg in entry_events:
                _emit(make_event(
                    "entry",
                    f"[TICK] {opt_type} {msg}",
                    option_type=opt_type,
                ))
                if self.telegram:
                    try:
                        self.telegram.send_message(
                            f"<b>ENTRY [TICK]</b> {self.instrument} "
                            f"{opt_type}\n{msg}"
                        )
                    except Exception:
                        pass

    # ------------------------------------------
    # EOD CLOSE (safety net)
    # ------------------------------------------
    def _eod_close_all(self) -> List[dict]:
        """Force-close any open positions at EOD."""
        events = []
        now = datetime.now(IST)

        for opt_type in ("CE", "PE"):
            active = self.active_ce if opt_type == "CE" else self.active_pe
            if active is None:
                continue

            if active.has_position():
                ltp = self._get_ws_option_ltp(active.strike, opt_type)
                if ltp is None:
                    bar = self.buffer.get_option_bar(opt_type, -1)
                    ltp = bar["close"] if bar else active.base_price
                active.close_trade(now, ltp, "EOD")
                self.completed_trades.append(active)
                avg = active.get_avg_entry_price() or 0
                pnl = (avg - ltp if self.direction == "sell"
                       else ltp - avg)
                pnl_pct = (pnl / avg * 100) if avg else 0
                events.append(make_event(
                    "exit",
                    f"{opt_type} EXIT EOD (safety net): "
                    f"LTP={ltp:.2f} | pnl={pnl_pct:+.2f}%",
                    option_type=opt_type,
                ))
            else:
                events.append(make_event(
                    "info",
                    f"{opt_type} EOD: observation expired (no entry)",
                    option_type=opt_type,
                ))

            if opt_type == "CE":
                self.active_ce = None
            else:
                self.active_pe = None

        return events

    # ------------------------------------------
    # MAIN LOOP
    # ------------------------------------------
    def run_loop(self, on_event: Optional[Callable] = None,
                 stop_event: Optional[threading.Event] = None):
        """
        Main loop: run_one_minute() at minute boundaries + tick-level
        checks every second.
        """
        logger.info(
            f"Starting forward test loop for {self.instrument} | "
            f"hours: {self.entry_time} - {self.exit_time}"
        )

        if stop_event is None:
            stop_event = threading.Event()

        def _emit(ev):
            if on_event:
                on_event(ev)

        # Wait until market open (before entry_time)
        # Note: the runner already waits for market hours, but this handles
        # the case where engine is used directly (e.g. from Streamlit UI)
        while not stop_event.is_set():
            now = datetime.now(IST)
            if now.time() >= self.entry_time:
                break
            wait_secs = min(30, (
                datetime.combine(now.date(), self.entry_time)
                - now.replace(tzinfo=None)
            ).total_seconds())
            if wait_secs > 0:
                _emit(make_event(
                    "info",
                    f"Waiting for market open ({self.entry_time}). "
                    f"~{int(wait_secs)}s remaining."
                ))
                stop_event.wait(timeout=max(wait_secs, 1))

        # --- Warmup with retry ---
        MAX_WARMUP_RETRIES = 5
        WARMUP_BASE_BACKOFF = 15

        warmup_ok = False
        for attempt in range(1, MAX_WARMUP_RETRIES + 1):
            if stop_event.is_set():
                break
            try:
                warmup_events = self.warmup()
                for ev in warmup_events:
                    _emit(ev)

                has_error = any(
                    e.get("type") == "error" for e in warmup_events
                )

                if has_error:
                    backoff = min(
                        WARMUP_BASE_BACKOFF * (2 ** (attempt - 1)), 120
                    )
                    _emit(make_event(
                        "error",
                        f"Warmup attempt {attempt}/{MAX_WARMUP_RETRIES} "
                        f"had API errors. Retrying in {backoff}s..."
                    ))
                    stop_event.wait(timeout=backoff)
                    continue

                warmup_ok = True
                break

            except Exception as e:
                backoff = min(
                    WARMUP_BASE_BACKOFF * (2 ** (attempt - 1)), 120
                )
                logger.error(f"Warmup attempt {attempt} exception: {e}",
                             exc_info=True)
                _emit(make_event(
                    "error",
                    f"Warmup attempt {attempt}/{MAX_WARMUP_RETRIES} "
                    f"crashed: {e}. Retrying in {backoff}s..."
                ))
                stop_event.wait(timeout=backoff)

        if not warmup_ok and not stop_event.is_set():
            _emit(make_event(
                "error",
                f"Warmup failed after {MAX_WARMUP_RETRIES} attempts. "
                f"Cannot start — API is unreachable. Aborting."
            ))
            return

        # --- Start WebSocket feed ---
        if not stop_event.is_set():
            ws_events = self._init_websocket()
            for ev in ws_events:
                _emit(ev)

        # Backoff state for error handling
        consecutive_errors = 0
        MAX_CONSECUTIVE_ERRORS = 10
        BASE_BACKOFF = 10

        last_minute_run = -1
        TICK_POLL_INTERVAL = 1.0

        # Main 1-second polling loop
        while not stop_event.is_set():
            now = datetime.now(IST)

            if now.time() > self.exit_time:
                eod_events = self._eod_close_all()
                for ev in eod_events:
                    _emit(ev)
                _emit(make_event("info", "Market closed. Loop ended."))
                break

            current_minute = now.hour * 60 + now.minute

            # --- MINUTE BOUNDARY: full indicator + signal cycle ---
            if current_minute != last_minute_run:
                last_minute_run = current_minute
                try:
                    events = self.run_one_minute()
                    for ev in events:
                        _emit(ev)

                    has_error = any(
                        e.get("type") == "error" for e in events
                    )
                    if has_error:
                        consecutive_errors += 1
                    else:
                        consecutive_errors = 0
                except Exception as e:
                    logger.error(
                        f"Error in run_one_minute: {e}", exc_info=True
                    )
                    _emit(make_event("error", f"Engine error: {e}"))
                    consecutive_errors += 1

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    _emit(make_event(
                        "error",
                        f"Stopping: {MAX_CONSECUTIVE_ERRORS} consecutive "
                        f"API errors. Check credentials and API status."
                    ))
                    break

                if consecutive_errors > 0:
                    backoff = min(
                        BASE_BACKOFF * (2 ** (consecutive_errors - 1)), 120
                    )
                    _emit(make_event(
                        "info",
                        f"API error #{consecutive_errors}. "
                        f"Backing off {backoff}s before retry..."
                    ))
                    stop_event.wait(timeout=backoff)
                    continue

            # --- TICK-LEVEL: signal check + entry/SL/TP check ---
            # 1. Check signals on live ticks (uses cached indicators + live LTP)
            try:
                signal_events = self._check_tick_signals()
                for ev in signal_events:
                    _emit(ev)
            except Exception as e:
                logger.debug(f"Tick signal check error (non-fatal): {e}")

            # 2. Indicator-level entry on live ticks
            if self.entry_indicator:
                try:
                    self._check_tick_indicator_entry(_emit)
                except Exception as e:
                    logger.debug(f"Tick indicator entry error (non-fatal): {e}")

            # 3. Check staggered entry and SL/TP on live ticks
            try:
                tick_events = self._tick_checker.check(
                    self.active_ce, self.active_pe, self._ws_feed,
                )
                # Handle trade closures from tick checker.
                # Trade objects are mutated in-place by _check_exit, but we
                # need to move them to completed_trades and clear the slot.
                for ev in tick_events:
                    _emit(ev)
                    if ev.get("type") == "exit":
                        ot = ev.get("option_type", "")
                        if ot == "CE" and self.active_ce:
                            self.completed_trades.append(self.active_ce)
                            self.active_ce = None
                        elif ot == "PE" and self.active_pe:
                            self.completed_trades.append(self.active_pe)
                            self.active_pe = None
            except Exception as e:
                logger.debug(f"Tick check error (non-fatal): {e}")

            stop_event.wait(timeout=TICK_POLL_INTERVAL)

        # Final EOD safety net
        eod_events = self._eod_close_all()
        for ev in eod_events:
            _emit(ev)

        self._stop_websocket()

        logger.info("Forward test loop finished.")
