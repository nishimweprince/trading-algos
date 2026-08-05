"""The rule set, in declaration order.

Order is the tie-break when two rules produce the same confidence on one bar, so
it is part of the tagger's output and not just a listing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Optional

from app.taggers.rules.engulfing import bear_engulfing, bull_engulfing
from app.taggers.rules.inside_bar import inside_break
from app.taggers.rules.pin_bar import pin_bar_long, pin_bar_short
from app.taggers.types import Bar, BarTag

# Optional[...] rather than `| None`: this is a runtime expression, not an
# annotation, so the future import does not defer it.
Rule = Callable[[Sequence[Bar], float], Optional[BarTag]]

RULES: tuple[Rule, ...] = (
    bull_engulfing,
    bear_engulfing,
    pin_bar_long,
    pin_bar_short,
    inside_break,
)

__all__ = [
    "RULES",
    "Rule",
    "bear_engulfing",
    "bull_engulfing",
    "inside_break",
    "pin_bar_long",
    "pin_bar_short",
]
