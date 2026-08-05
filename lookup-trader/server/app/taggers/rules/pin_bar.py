"""Pin bars: one dominant wick, a small body, and a close away from the wick."""

from __future__ import annotations

from collections.abc import Sequence

from app.taggers.confidence import graded
from app.taggers.thresholds import (
    MIN_RANGE_ATR,
    PIN_BODY_MAX_PCT,
    PIN_CLOSE_POS,
    PIN_OPP_WICK_MAX_PCT,
    PIN_WICK_PCT,
)
from app.taggers.types import Bar, BarTag

LOOKBACK = 1

# Guards the headroom ratios below against a bar with literally no body or no
# opposite wick, where the division would be by zero rather than by something
# small. RATIO_CAP then keeps the resulting large ratio from carrying the score.
_EPS = 1e-6


def _pin_bar(bars: Sequence[Bar], atr: float, side: int) -> BarTag | None:
    if len(bars) < LOOKBACK:
        return None
    anchor = bars[-1]

    if side == 1:
        wick_pct, opp_wick_pct = anchor.lower_pct, anchor.upper_pct
        close_pos = anchor.close_pos
        setup_id = "pin_bar_long"
    else:
        wick_pct, opp_wick_pct = anchor.upper_pct, anchor.lower_pct
        close_pos = 1.0 - anchor.close_pos
        setup_id = "pin_bar_short"

    if wick_pct < PIN_WICK_PCT:
        return None
    if anchor.body_pct > PIN_BODY_MAX_PCT:
        return None
    if opp_wick_pct > PIN_OPP_WICK_MAX_PCT:
        return None
    # The rejection has to hold into the close. A long lower wick that closes in
    # the bottom third is a bar that was sold, not one that was bought back.
    if close_pos < PIN_CLOSE_POS:
        return None

    range_atr = anchor.range / atr
    if range_atr < MIN_RANGE_ATR:
        return None

    return BarTag(
        setup_id=setup_id,
        confidence=graded(
            wick_pct / PIN_WICK_PCT,
            PIN_BODY_MAX_PCT / max(anchor.body_pct, _EPS),
            PIN_OPP_WICK_MAX_PCT / max(opp_wick_pct, _EPS),
            close_pos / PIN_CLOSE_POS,
            range_atr / MIN_RANGE_ATR,
        ),
        side=side,
    )


def pin_bar_long(bars: Sequence[Bar], atr: float) -> BarTag | None:
    return _pin_bar(bars, atr, 1)


def pin_bar_short(bars: Sequence[Bar], atr: float) -> BarTag | None:
    return _pin_bar(bars, atr, -1)
