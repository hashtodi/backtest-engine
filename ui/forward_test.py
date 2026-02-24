"""
Streamlit tab for forward testing (paper trading).

Provides:
  - Strategy and instrument selector
  - Start / Stop controls
  - Live event log (auto-refreshing every 10s)
  - Current position status
  - Session P&L summary
"""

import os
import threading
import logging
from collections import deque
from datetime import datetime

import streamlit as st
import pytz

import config
from datafeed import DhanDataFeed
from forward.engine import ForwardTestEngine
from forward.paper_trader import PaperTrader
from ui.strategy_store import (
    list_saved_strategies, load_saved_strategy, render_strategy_selector,
)

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Maximum events to keep in the UI log
MAX_UI_EVENTS = 200

# Auto-refresh interval (ms) while engine is running
AUTO_REFRESH_MS = 10_000


# ============================================
# SESSION STATE HELPERS
# ============================================

def _init_ft_state():
    """Initialise forward-test session state keys if missing."""
    defaults = {
        "ft_running": False,
        "ft_events": None,
        "ft_thread": None,
        "ft_stop_event": None,
        "ft_engine": None,
        "ft_paper": None,
        "ft_error": None,
        # Persist which strategy/instrument is running
        "ft_strategy_name": None,
        "ft_instrument_name": None,
        # Flag to show last session events after thread dies
        "ft_show_last_events": False,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def _load_env():
    """Load .env.local into os.environ if present."""
    path = ".env.local"
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
# ENGINE THREAD
# ============================================

def _run_engine_thread(engine: ForwardTestEngine, paper: PaperTrader,
                       stop_event: threading.Event,
                       event_sink: deque):
    """
    Target function for the background engine thread.

    Runs the forward test loop, piping events to both PaperTrader
    and the shared deque (for Streamlit UI).
    """
    def _on_event(ev):
        paper.on_event(ev)
        event_sink.append(ev)

    try:
        engine.run_loop(on_event=_on_event, stop_event=stop_event)
    except Exception as e:
        logger.error(f"Forward test thread error: {e}", exc_info=True)
        event_sink.append({
            "time": datetime.now(IST),
            "type": "error",
            "option_type": "",
            "message": f"Engine crashed: {e}",
        })
    finally:
        paper.sync_trades(engine.completed_trades)


# ============================================
# START / STOP
# ============================================

def _start_forward_test(strategy: dict, instrument: str):
    """Initialise and start the forward test engine in a background thread."""
    _load_env()

    client_id = os.getenv("CLIENT_ID")
    access_token = os.getenv("ACCESS_TOKEN")

    if not client_id or not access_token:
        st.session_state.ft_error = (
            "Missing Dhan API credentials. "
            "Set CLIENT_ID and ACCESS_TOKEN in .env.local."
        )
        return

    lot_size = config.LOT_SIZE.get(instrument, 1)

    # Create data feed
    feed = DhanDataFeed(client_id, access_token)

    # Create engine
    engine = ForwardTestEngine(
        strategy=strategy,
        data_feed=feed,
        instrument=instrument,
        lot_size=lot_size,
    )

    # Create paper trader
    strategy_name = strategy.get("name", "unnamed")
    paper = PaperTrader(instrument, strategy_name, lot_size)

    # Shared event sink for UI
    event_sink = deque(maxlen=MAX_UI_EVENTS)

    # Stop event for graceful shutdown
    stop_event = threading.Event()

    # Launch thread
    thread = threading.Thread(
        target=_run_engine_thread,
        args=(engine, paper, stop_event, event_sink),
        daemon=True,
    )
    thread.start()

    # Store in session state
    st.session_state.ft_running = True
    st.session_state.ft_events = event_sink
    st.session_state.ft_thread = thread
    st.session_state.ft_stop_event = stop_event
    st.session_state.ft_engine = engine
    st.session_state.ft_paper = paper
    st.session_state.ft_error = None
    st.session_state.ft_strategy_name = strategy_name
    st.session_state.ft_instrument_name = instrument
    st.session_state.ft_show_last_events = False

    logger.info(f"Forward test started: {instrument} / {strategy_name}")


def _stop_forward_test():
    """Signal the engine thread to stop and clean up WebSocket."""
    stop_event = st.session_state.get("ft_stop_event")
    if stop_event:
        stop_event.set()

    thread = st.session_state.get("ft_thread")
    if thread and thread.is_alive():
        thread.join(timeout=10)

    # Sync final trades (safe — thread is stopped or timed out)
    engine = st.session_state.get("ft_engine")
    paper = st.session_state.get("ft_paper")
    if engine and paper:
        paper.sync_trades(engine.completed_trades)

    # WebSocket cleanup (engine stops it in run_loop, but double-check)
    if engine and hasattr(engine, '_ws_feed') and engine._ws_feed:
        try:
            engine._stop_websocket()
        except Exception:
            pass

    st.session_state.ft_running = False
    # Keep events visible so user can review after stopping
    st.session_state.ft_show_last_events = True
    logger.info("Forward test stopped.")


def _is_engine_alive() -> bool:
    """Check if the engine thread is still running."""
    thread = st.session_state.get("ft_thread")
    return thread is not None and thread.is_alive()


# ============================================
# RENDER
# ============================================

def render_forward_test():
    """Render the Forward Test tab."""
    _init_ft_state()

    st.header("Forward Test (Paper Trading)")
    st.caption(
        "Run a saved strategy against live market data. "
        "No real orders are placed — trades are simulated at live prices."
    )

    # --- ERROR DISPLAY ---
    if st.session_state.ft_error:
        st.error(st.session_state.ft_error)

    # Detect thread death while ft_running is True
    if st.session_state.ft_running and not _is_engine_alive():
        st.session_state.ft_running = False
        st.session_state.ft_show_last_events = True
        st.warning(
            "Forward test engine stopped (thread ended). "
            "Check the event log below for details."
        )

    # =============================================
    # NOT RUNNING: show config & start button
    # =============================================
    if not st.session_state.ft_running:
        _render_config_panel()

        # Show last session events/summary if available
        _render_last_session()

    # =============================================
    # RUNNING: show controls, live log, status
    # =============================================
    else:
        _render_running_panel()

        # Auto-refresh while running (no manual Refresh needed)
        try:
            from streamlit_autorefresh import st_autorefresh
            st_autorefresh(
                interval=AUTO_REFRESH_MS,
                limit=None,
                key="ft_autorefresh",
            )
        except ImportError:
            # Fallback: manual refresh hint
            st.caption(
                "Install `streamlit-autorefresh` for auto-updating log, "
                "or click Refresh."
            )


# ============================================
# CONFIG PANEL (when not running)
# ============================================

def _render_config_panel():
    """Strategy and instrument selectors + start button."""
    # Use the shared strategy selector (no pre-selection on refresh)
    strategy, slug = render_strategy_selector("ft")
    if strategy is None:
        st.info("Select a strategy above to start forward testing.")
        return

    # Instrument selector depends on the selected strategy
    instruments = strategy.get("instruments", list(config.LOT_SIZE.keys()))
    instrument = st.selectbox(
        "Instrument", instruments, key="ft_instrument_select"
    )

    # Start button
    if st.button("Start Forward Test", type="primary", key="ft_start_btn"):
        _start_forward_test(strategy, instrument)
        st.rerun()


# ============================================
# RUNNING PANEL
# ============================================

def _render_running_panel():
    """Live controls, event log, and status while engine is running."""
    engine = st.session_state.get("ft_engine")
    paper = st.session_state.get("ft_paper")

    # Header: show which strategy/instrument and data feed mode
    strat_name = st.session_state.get("ft_strategy_name", "?")
    instr_name = st.session_state.get("ft_instrument_name", "?")
    ws_status = ""
    if engine and hasattr(engine, '_ws_feed') and engine._ws_feed:
        if engine._ws_feed.is_connected:
            ws_status = " | Feed: WebSocket"
        else:
            ws_status = " | Feed: REST (WS reconnecting)"
    else:
        ws_status = " | Feed: REST"
    st.info(
        f"Running **{strat_name}** on **{instr_name}** | "
        f"ATM: {engine._atm_strike or '--'}{ws_status}"
    )

    # Controls row
    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        if st.button("Stop Forward Test", type="secondary", key="ft_stop_btn"):
            _stop_forward_test()
            st.rerun()
    with col2:
        if st.button("Refresh", key="ft_refresh_btn"):
            st.rerun()
    with col3:
        if engine:
            ce_stat = "active" if engine.active_ce else "idle"
            pe_stat = "active" if engine.active_pe else "idle"
            st.caption(f"CE: {ce_stat} | PE: {pe_stat} | Auto-refresh: 10s")

    st.divider()

    # --- Active positions ---
    _render_active_positions(engine)

    # --- P&L summary (read-only snapshot, avoid race condition) ---
    if paper:
        try:
            completed = list(engine.completed_trades) if engine else []
            paper.sync_trades(completed)
        except Exception:
            pass
        _render_summary(paper)

    st.divider()

    # --- Event log ---
    _render_event_log()


# ============================================
# LAST SESSION (shown after engine stops)
# ============================================

def _render_last_session():
    """Show events and summary from the last forward test session."""
    if not st.session_state.get("ft_show_last_events"):
        return

    events = st.session_state.get("ft_events")
    paper = st.session_state.get("ft_paper")
    strat_name = st.session_state.get("ft_strategy_name", "?")
    instr_name = st.session_state.get("ft_instrument_name", "?")

    if not events and not paper:
        return

    st.divider()
    st.subheader(f"Last Session: {strat_name} / {instr_name}")

    if paper and paper.completed_trades:
        _render_summary(paper)

    if events:
        st.divider()
        _render_event_log()


# ============================================
# SUB-RENDERERS
# ============================================

def _render_event_log():
    """Render the event log from session state."""
    st.subheader("Live Event Log")
    events = st.session_state.get("ft_events")

    if not events:
        st.info("Waiting for events...")
        return

    event_list = list(events)
    # Show newest first, up to 100
    for ev in reversed(event_list[-100:]):
        _render_event_line(ev)


def _render_active_positions(engine):
    """Show current active CE/PE positions."""
    if engine is None:
        return

    st.subheader("Active Positions")
    col1, col2 = st.columns(2)

    for opt_type, trade, col in [
        ("CE", engine.active_ce, col1),
        ("PE", engine.active_pe, col2),
    ]:
        with col:
            if trade is None:
                st.caption(f"{opt_type}: idle")
            else:
                avg = trade.get_avg_entry_price()
                n_filled = len(trade.parts)
                if avg:
                    st.markdown(
                        f"**{opt_type}** {int(trade.strike)} | "
                        f"status: {trade.status} | "
                        f"parts: {n_filled}/{trade.num_levels} | "
                        f"avg: {avg:.2f}"
                    )
                else:
                    st.markdown(
                        f"**{opt_type}** {int(trade.strike)} | "
                        f"status: {trade.status} | waiting entry"
                    )


def _render_summary(paper: PaperTrader):
    """Show P&L summary metrics."""
    summary = paper.get_summary()

    st.subheader("Session P&L")
    cols = st.columns(5)
    cols[0].metric("Trades", summary["total_trades"])
    cols[1].metric("Winners", summary["winning_trades"])
    cols[2].metric("Losers", summary["losing_trades"])
    cols[3].metric("Win Rate", f"{summary['win_rate']:.1f}%")
    cols[4].metric("Total P&L", f"{summary['total_pnl_pct']:+.2f}%")

    cols2 = st.columns(4)
    cols2[0].metric("Signals", summary["signals"])
    cols2[1].metric("Entries", summary["entries"])
    cols2[2].metric("Exits", summary["exits"])
    cols2[3].metric("Money P&L", f"Rs {summary['total_money_pnl']:+.0f}")


def _render_event_line(ev: dict):
    """Render a single event as a styled line."""
    time_obj = ev.get("time", "")
    if hasattr(time_obj, "strftime"):
        time_str = time_obj.strftime("%H:%M:%S")
    else:
        time_str = str(time_obj)

    etype = ev.get("type", "info")
    msg = ev.get("message", "")

    colour_map = {
        "signal": "orange",
        "entry": "blue",
        "exit": "green",
        "error": "red",
        "info": "gray",
    }
    colour = colour_map.get(etype, "gray")

    st.markdown(
        f"<span style='color:{colour};font-family:monospace;font-size:0.85em'>"
        f"[{time_str}] [{etype.upper():6s}] {msg}</span>",
        unsafe_allow_html=True,
    )
