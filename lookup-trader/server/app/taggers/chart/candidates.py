"""Which bars are worth asking a model about.

Chart patterns are rare. Sampling uniformly across history to build a labelling
set would spend most of the budget on bars where nothing is forming, and would
hand the classifier a training set with almost no positives. This module picks
out the bars where the pivot geometry admits a pattern at all.

It is a **recall** filter, deliberately loose: a candidate is a bar the geometry
cannot rule out, not a bar that has the pattern. Precision is the labeller's job.
Tightening these thresholds to "only real ones" would defeat the purpose — the
whole point of asking a model is that the boundary cases are the hard ones.

Causal by construction: everything here reads the pivot sequence, and a pivot
needs `lookback` bars after it to be confirmed.
"""

from __future__ import annotations

from typing import NamedTuple

from app.taggers.chart.swings import Pivot, alternating

# How close two pivots must be, in ATR, to read as "the same level". Wide on
# purpose — a double bottom whose second low undercuts the first by half an ATR
# is still a double bottom to a human, and that is the call being delegated.
LEVEL_TOLERANCE_ATR = 1.0

# A pattern needs room. Two lows three bars apart are noise, not a formation.
MIN_PIVOT_SEPARATION = 5

# How far back the anchor may sit from the formation's last pivot before the
# pattern stops being about the current bar. Kept short deliberately: a formation
# whose last turn was two days ago is history, and a generous window makes every
# anchor in it re-report the same pattern.
MAX_BARS_SINCE_FORMATION = 10

# Fraction of the opening range the boundaries must close (or open) by before a
# contraction or expansion is worth naming.
MIN_RANGE_CHANGE = 0.35

# A triangle side counts as flat if it moves less than this many ATR across the
# whole formation.
FLAT_SIDE_ATR = 0.5


class Candidate(NamedTuple):
    """A bar whose geometry admits one or more patterns."""

    setup_ids: tuple[str, ...]
    pivots_used: tuple[Pivot, ...]


def _near(a: float, b: float, atr: float) -> bool:
    return abs(a - b) <= LEVEL_TOLERANCE_ATR * atr


def _separated(a: Pivot, b: Pivot) -> bool:
    return abs(b.index - a.index) >= MIN_PIVOT_SEPARATION


def _double(trio: list[Pivot], kind: str, atr: float) -> str | None:
    """low-high-low → double bottom; high-low-high → double top."""
    first, middle, second = trio
    if first.kind != kind or second.kind != kind or middle.kind == kind:
        return None
    if not (_separated(first, middle) and _separated(middle, second)):
        return None
    if not _near(first.price, second.price, atr):
        return None
    # The middle pivot has to actually separate them, or this is one turn.
    if _near(middle.price, first.price, atr):
        return None
    return "double_bottom" if kind == "low" else "double_top"


def _head_shoulders(five: list[Pivot], kind: str, atr: float) -> str | None:
    """Five pivots where the middle extreme overshoots its two neighbours."""
    kinds = [p.kind for p in five]
    if kinds[0] != kind or kinds[2] != kind or kinds[4] != kind:
        return None
    left, head, right = five[0], five[2], five[4]
    if not (_separated(left, head) and _separated(head, right)):
        return None

    if kind == "high":
        overshoots = head.price > left.price and head.price > right.price
    else:
        overshoots = head.price < left.price and head.price < right.price
    if not overshoots:
        return None
    # Shoulders roughly level with each other is what separates this from a
    # plain trend leg.
    if not _near(left.price, right.price, atr):
        return None
    return "head_shoulders" if kind == "high" else "inv_head_shoulders"


def _slope(a: Pivot, b: Pivot) -> float:
    """Price change per bar between two pivots."""
    span = b.index - a.index
    return (b.price - a.price) / span if span else 0.0


def _channel_family(four: list[Pivot], atr: float) -> str | None:
    """Name the boundary geometry of two highs and two lows.

    A shrinking range is not enough — a market that merely quietened down has one
    too. The boundaries themselves have to move: lower highs, higher lows, or a
    flat side with the other closing on it. Requiring that is what separates this
    from "the last few bars were calmer than the ones before".

    Which member of the family it is comes from the two slopes, so the filter can
    name it rather than reporting the whole family and making the labeller guess.
    """
    highs = [p for p in four if p.kind == "high"]
    lows = [p for p in four if p.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None

    first_range = abs(highs[0].price - lows[0].price)
    last_range = abs(highs[-1].price - lows[-1].price)
    if first_range <= 0:
        return None

    high_slope = _slope(highs[0], highs[-1])
    low_slope = _slope(lows[0], lows[-1])
    # A side counts as flat when it has not moved a meaningful fraction of an ATR
    # across the whole formation.
    flat = FLAT_SIDE_ATR * atr / max(four[-1].index - four[0].index, 1)
    high_flat, low_flat = abs(high_slope) < flat, abs(low_slope) < flat

    contraction = (first_range - last_range) / first_range
    expansion = (last_range - first_range) / first_range

    if expansion >= MIN_RANGE_CHANGE and high_slope > 0 and low_slope < 0:
        return "broadening_formation"

    if contraction < MIN_RANGE_CHANGE:
        return None

    if high_flat and low_slope > 0:
        return "triangle_ascending"
    if low_flat and high_slope < 0:
        return "triangle_descending"
    if high_slope < 0 and low_slope > 0:
        return "triangle_symmetrical"
    # Both boundaries leaning the same way while the range closes is a wedge,
    # and the direction of the lean names it.
    if high_slope < 0 and low_slope < 0:
        return "wedge_falling"
    if high_slope > 0 and low_slope > 0:
        return "wedge_rising"
    return None


def candidates(found: list[Pivot], anchor_index: int, atr: float) -> Candidate | None:
    """Patterns the pivot geometry admits at `anchor_index`, or None.

    `found` is the raw pivot sequence for the window ending at the anchor. The
    returned `setup_ids` are seeds for a labelling prompt, not verdicts — several
    may be reported for one bar, and the model is expected to reject most of them.
    """
    if atr <= 0:
        return None

    series = alternating(found)
    if len(series) < 3:
        return None
    if anchor_index - series[-1].index > MAX_BARS_SINCE_FORMATION:
        return None

    hits: list[str] = []
    used: list[Pivot] = []

    trio = series[-3:]
    for kind in ("low", "high"):
        name = _double(trio, kind, atr)
        if name:
            hits.append(name)
            used = trio

    if len(series) >= 5:
        five = series[-5:]
        for kind in ("high", "low"):
            name = _head_shoulders(five, kind, atr)
            if name:
                hits.append(name)
                used = five

    if len(series) >= 4:
        four = series[-4:]
        name = _channel_family(four, atr)
        if name:
            hits.append(name)
            used = used or four

    if not hits:
        return None
    return Candidate(tuple(dict.fromkeys(hits)), tuple(used))
