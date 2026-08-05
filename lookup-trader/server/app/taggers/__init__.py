"""Per-bar pattern tagging.

Deterministic rules over the candles at or before a bar, producing the setup ids
the operator already labels with. The tags are the universe; `occurrences` are
the operator-confirmed subset of it.
"""

from __future__ import annotations

from app.taggers.pipeline import TAG_LOOKBACK, tag_bar
from app.taggers.types import Bar, BarTag, TagResult, TagSource, TagState

__all__ = [
    "TAG_LOOKBACK",
    "Bar",
    "BarTag",
    "TagResult",
    "TagSource",
    "TagState",
    "tag_bar",
]
