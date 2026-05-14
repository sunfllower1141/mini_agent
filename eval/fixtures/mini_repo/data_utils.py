"""Combined data utilities module.

Contains both loading and transformation functions.
Needs to be split into separate modules.
"""

from typing import Any


# ---- Data loading functions ----
def load_csv(path: str) -> list[dict[str, Any]]:
    """Load data from a CSV file."""
    return []


def load_json(path: str) -> dict[str, Any]:
    """Load data from a JSON file."""
    return {}


def load_text(path: str) -> str:
    """Load text from a file."""
    return ""


# ---- Data transformation functions ----
def transform_normalize(data: list[float]) -> list[float]:
    """Normalize a list of floats to [0, 1] range."""
    if not data:
        return []
    mn, mx = min(data), max(data)
    if mx == mn:
        return [0.0 for _ in data]
    return [(x - mn) / (mx - mn) for x in data]


def transform_filter_positive(data: list[float]) -> list[float]:
    """Keep only positive values."""
    return [x for x in data if x > 0]


def transform_scale(data: list[float], factor: float) -> list[float]:
    """Scale all values by a factor."""
    return [x * factor for x in data]
