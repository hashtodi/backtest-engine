"""
Strategy persistence: save, load, list, delete strategies as JSON.

Also provides shared UI components for strategy selection and
description rendering used across all Streamlit tabs.

Strategies are stored in saved_strategies/ as JSON files.
Each file is a complete strategy config dict (same format as _build_strategy()).

Output files for each strategy go to output/{slug}/:
  - results_{INST}.csv
  - detailed_{INST}.log
  - trades.log
  - summary.md
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import streamlit as st

STRATEGIES_DIR = Path("saved_strategies")
OUTPUT_DIR = Path("output")


def _slugify(name: str) -> str:
    """Convert strategy name to a filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)  # replace non-alphanum with _
    slug = slug.strip("_")
    return slug or "unnamed"


def save_strategy(strategy: Dict) -> str:
    """
    Save a strategy config to JSON.

    Returns the slug (filename without .json).
    Overwrites if same name already exists.
    """
    STRATEGIES_DIR.mkdir(exist_ok=True)
    slug = _slugify(strategy.get("name", "unnamed"))
    path = STRATEGIES_DIR / f"{slug}.json"
    with open(path, "w") as f:
        json.dump(strategy, f, indent=2)
    return slug


def load_saved_strategy(slug: str) -> Optional[Dict]:
    """Load a saved strategy by slug. Returns None if not found."""
    path = STRATEGIES_DIR / f"{slug}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def list_saved_strategies() -> List[Dict]:
    """
    List all saved strategies.

    Returns list of {"slug": str, "name": str, "description": str}
    sorted alphabetically by name.
    """
    STRATEGIES_DIR.mkdir(exist_ok=True)
    items = []
    for path in sorted(STRATEGIES_DIR.glob("*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            items.append({
                "slug": path.stem,
                "name": data.get("name", path.stem),
                "description": data.get("description", ""),
            })
        except (json.JSONDecodeError, KeyError):
            continue
    return items


def delete_strategy(slug: str) -> bool:
    """Delete a saved strategy file. Returns True if deleted."""
    path = STRATEGIES_DIR / f"{slug}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def get_output_dir(strategy: Dict) -> Path:
    """
    Get (and create) the output directory for a strategy's backtest files.

    Output goes to output/{slug}/.
    """
    slug = _slugify(strategy.get("name", "unnamed"))
    out = OUTPUT_DIR / slug
    out.mkdir(parents=True, exist_ok=True)
    return out


# ============================================
# SHARED UI COMPONENTS
# ============================================

def render_strategy_selector(key_prefix: str) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Render a strategy selectbox with no pre-selection and a description.

    Args:
        key_prefix: prefix for widget keys (e.g. "dash", "te", "bt", "ft")

    Returns:
        (strategy_dict, slug) if selected, (None, None) otherwise.
    """
    saved = list_saved_strategies()
    if not saved:
        st.info("No saved strategies found. Create one in the Backtest tab.")
        return None, None

    names = [s["name"] for s in saved]
    slugs = [s["slug"] for s in saved]

    # index=None gives the "Select a strategy..." placeholder
    idx = st.selectbox(
        "Select Strategy",
        range(len(names)),
        format_func=lambda i: names[i],
        index=None,
        placeholder="Select a strategy...",
        key=f"{key_prefix}_strategy_select",
    )

    if idx is None:
        return None, None

    slug = slugs[idx]
    strategy = load_saved_strategy(slug)
    if strategy is None:
        st.error(f"Failed to load strategy: {slug}")
        return None, None

    # Show strategy description inline
    render_strategy_description(strategy)
    return strategy, slug


def render_strategy_description(strategy: Dict):
    """
    Render a compact strategy summary inside an expander.

    Shows: direction, indicators, signal conditions,
    entry type, SL/TP, instruments, and trading hours.
    """
    name = strategy.get("name", "Unnamed")
    desc = strategy.get("description", "")

    with st.expander(f"Strategy: {name}", expanded=False):
        if desc:
            st.caption(desc)

        # --- Direction ---
        direction = strategy.get("direction", "sell").upper()
        st.markdown(f"**Direction:** {direction}")

        # --- Indicators ---
        indicators = strategy.get("indicators", [])
        if indicators:
            ind_parts = []
            for ind in indicators:
                src = ind.get("price_source", "option")
                ind_parts.append(f"`{ind.get('name', ind['type'])}` ({src})")
            st.markdown("**Indicators:** " + ", ".join(ind_parts))

        # --- Signal conditions ---
        conditions = strategy.get("signal_conditions", [])
        logic = strategy.get("signal_logic", "AND")
        if conditions:
            cond_parts = []
            for c in conditions:
                compare = c.get("compare", "")
                val = c.get("value", c.get("other", ""))
                cond_parts.append(f"{c.get('indicator', '?')} {compare} {val}")
            st.markdown(
                f"**Signal:** {f' {logic} '.join(cond_parts)}"
            )

        # --- Entry ---
        entry = strategy.get("entry", {})
        etype = entry.get("type", "direct")
        if etype == "indicator_level":
            st.markdown(f"**Entry:** Indicator Level ({entry.get('indicator', '?')})")
        elif etype == "staggered":
            parts = [
                f"+{lv['pct_from_base']}% ({lv['capital_pct']}%)"
                for lv in entry.get("levels", [])
            ]
            st.markdown("**Entry:** Staggered — " + " / ".join(parts))
        else:
            st.markdown("**Entry:** Direct (100%)")

        # --- Risk ---
        sl = strategy.get("stop_loss_pct", 0)
        tp = strategy.get("target_pct", 0)
        sl_str = "Off" if sl >= 9999 else f"{sl}%"
        tp_str = "Off" if tp >= 9999 else f"{tp}%"
        st.markdown(f"**SL:** {sl_str} | **TP:** {tp_str}")

        # --- Instruments & session ---
        instruments = strategy.get("instruments", [])
        ts = strategy.get("trading_start", "09:30")
        te = strategy.get("trading_end", "15:30")
        if instruments:
            st.markdown(
                f"**Instruments:** {', '.join(instruments)} | "
                f"**Hours:** {ts}–{te}"
            )
