"""Inside bar break: contraction inside a mother bar, then a close beyond it.

Tagged on the break bar, not on the inside bar — the inside bar is a condition,
the break is the event, and only the break has a direction.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.taggers.confidence import graded
from app.taggers.thresholds import (
    INSIDE_BREAK_ATR,
    INSIDE_COMPRESSION_SPAN,
    MIN_RANGE_ATR,
)
from app.taggers.types import Bar, BarTag

LOOKBACK = 3


def inside_break(bars: Sequence[Bar], atr: float) -> BarTag | None:
    if len(bars) < LOOKBACK:
        return None
    mother, inside, anchor = bars[-3], bars[-2], bars[-1]

    if not (inside.high < mother.high and inside.low > mother.low):
        return None

    # Judged on the close, not the extreme. A wick past the mother high that
    # closes back inside is a *failed* break, and pooling failures with breaks
    # would make any base rate over this tag meaningless.
    if anchor.close > mother.high:
        side, boundary = 1, mother.high
    elif anchor.close < mother.low:
        side, boundary = -1, mother.low
    else:
        return None

    range_atr = anchor.range / atr
    if range_atr < MIN_RANGE_ATR:
        return None

    # Strict containment is the only gate on compression; a tighter one would
    # drop legitimate barely-inside bars. How tight it actually was feeds the
    # score instead, which is what a graded confidence is for.
    compression = 1.0 - inside.range / mother.range

    return BarTag(
        setup_id="inside_break",
        confidence=graded(
            1.0 + (abs(anchor.close - boundary) / atr) / INSIDE_BREAK_ATR,
            1.0 + compression / INSIDE_COMPRESSION_SPAN,
            range_atr / MIN_RANGE_ATR,
        ),
        side=side,
    )
