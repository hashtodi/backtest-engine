"""
Strategy persistence: save, load, list, delete strategies as JSON.

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
from typing import Dict, List, Optional

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
