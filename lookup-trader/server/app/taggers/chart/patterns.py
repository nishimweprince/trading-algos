"""Chart patterns from the pivot sequence.

This began as a recall filter — a cheap way to pick bars worth spending a
labelling call on. Spot-checking its output against the real store showed the
geometry was finding genuine formations, so it is promoted here to a detector
that emits tags directly. No model is involved, which means no API cost, no
prompt version to track, and no non-determinism.

Confidence is the same quantity the rule taggers report: **match quality** on
[0.6, 1.0], via the same `graded()` ramp over per-dimension ratios. A pattern
that exactly meets its tolerances scores the floor; one that clears them
comfortably scores higher. It is not a probability, and nothing here should be
read as one.

Causal by construction. Every input is a confirmed pivot (which needs `lookback`
bars after it to exist) or the anchor bar's own close.
"""

from __future__ import annotations

from typing import NamedTuple

import pandas as pd

from app.db.setups_seed import SEED_SETUPS
from app.taggers.chart.swings import Pivot, alternating
from app.taggers.confidence import graded
from app.taggers.types import BarTag, TagState

# How close two pivots must be, in ATR, to read as "the same level". Wide on
# purpose — a double bottom whose second low undercuts the first by half an ATR
# is still a double bottom, and the graded score records that it was the looser
# of the two rather than discarding it.
LEVEL_TOLERANCE_ATR = 1.0

# A pattern needs room. Two lows three bars apart are noise, not a formation.
MIN_PIVOT_SEPARATION = 5

# How far back the anchor may sit from the formation's last pivot before the
# pattern stops being about the current bar.
MAX_BARS_SINCE_FORMATION = 10

# Fraction of the opening range the boundaries must close (or open) by.
MIN_RANGE_CHANGE = 0.35

# A triangle side counts as flat if it moves less than this many ATR across the
# whole formation.
FLAT_SIDE_ATR = 0.5

# How far the head must clear its shoulders before the middle peak is a head
# rather than a slightly higher high.
MIN_HEAD_OVERSHOOT_ATR = 0.5

_EPS = 1e-9

# Direction comes from the seeded vocabulary rather than being restated here, so
# a tag's side can never disagree with what `GET /setups` reports for it.
_SIDE: dict[str, int | None] = {setup_id: side for setup_id, _, side, _ in SEED_SETUPS}


class Detection(NamedTuple):
    """A tag plus the pivots it was built from, for rendering and audit."""

    tag: BarTag
    pivots: tuple[Pivot, ...]


def _ratio(actual: float, threshold: float) -> float:
    """How many times over the threshold a measurement clears. 1.0 = exactly met."""
    return actual / threshold if threshold > 0 else 1.0


def _tag(setup_id: str, confidence: float, state: TagState) -> BarTag:
    return BarTag(
        setup_id=setup_id,
        state=state,
        confidence=confidence,
        source="algorithm",
        side=_SIDE.get(setup_id),
    )


def _double(trio: list[Pivot], kind: str, atr: float, close: float) -> Detection | None:
    """low-high-low → double bottom; high-low-high → double top.

    Completion is the neckline break: the formation is only a reversal once price
    has actually left it, and until then it is a shape that may still fail.
    """
    first, middle, second = trio
    if first.kind != kind or second.kind != kind or middle.kind == kind:
        return None

    sep = min(middle.index - first.index, second.index - middle.index)
    if sep < MIN_PIVOT_SEPARATION:
        return None

    gap = abs(first.price - second.price)
    if gap > LEVEL_TOLERANCE_ATR * atr:
        return None

    depth = abs(middle.price - first.price)
    if depth <= LEVEL_TOLERANCE_ATR * atr:
        return None

    setup_id = "double_bottom" if kind == "low" else "double_top"
    broken = close > middle.price if kind == "low" else close < middle.price
    confidence = graded(
        _ratio(LEVEL_TOLERANCE_ATR * atr, max(gap, _EPS)),
        _ratio(sep, MIN_PIVOT_SEPARATION),
        _ratio(depth, LEVEL_TOLERANCE_ATR * atr),
    )
    return Detection(
        _tag(setup_id, confidence, "complete" if broken else "forming"), tuple(trio)
    )


def _head_shoulders(five: list[Pivot], kind: str, atr: float, close: float) -> Detection | None:
    """Five pivots whose middle extreme clears two roughly level shoulders."""
    if [p.kind for p in five][::2] != [kind, kind, kind]:
        return None
    left, neck_a, head, neck_b, right = five

    sep = min(head.index - left.index, right.index - head.index)
    if sep < MIN_PIVOT_SEPARATION:
        return None

    overshoot = (
        min(head.price - left.price, head.price - right.price)
        if kind == "high"
        else min(left.price - head.price, right.price - head.price)
    )
    if overshoot < MIN_HEAD_OVERSHOOT_ATR * atr:
        return None

    shoulder_gap = abs(left.price - right.price)
    if shoulder_gap > LEVEL_TOLERANCE_ATR * atr:
        return None

    setup_id = "head_shoulders" if kind == "high" else "inv_head_shoulders"
    # The neckline runs through the two pivots between the shoulders and the
    # head; the later one is the level price has to clear to confirm.
    neckline = neck_b.price if neck_b.index > neck_a.index else neck_a.price
    broken = close < neckline if kind == "high" else close > neckline
    confidence = graded(
        _ratio(overshoot, MIN_HEAD_OVERSHOOT_ATR * atr),
        _ratio(LEVEL_TOLERANCE_ATR * atr, max(shoulder_gap, _EPS)),
        _ratio(sep, MIN_PIVOT_SEPARATION),
    )
    return Detection(
        _tag(setup_id, confidence, "complete" if broken else "forming"), tuple(five)
    )


def _slope(a: Pivot, b: Pivot) -> float:
    span = b.index - a.index
    return (b.price - a.price) / span if span else 0.0


def _channel_family(four: list[Pivot], atr: float, close: float) -> Detection | None:
    """Name the boundary geometry of two highs and two lows.

    A shrinking range is not enough — a market that merely quietened down has one
    too. The boundaries themselves have to move: lower highs, higher lows, or a
    flat side with the other closing on it. Requiring that is what took this from
    firing on 40% of bars to firing on 6%.
    """
    highs = [p for p in four if p.kind == "high"]
    lows = [p for p in four if p.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None

    first_range = abs(highs[0].price - lows[0].price)
    if first_range <= 0:
        return None
    last_range = abs(highs[-1].price - lows[-1].price)

    high_slope = _slope(highs[0], highs[-1])
    low_slope = _slope(lows[0], lows[-1])
    flat_per_bar = FLAT_SIDE_ATR * atr / max(four[-1].index - four[0].index, 1)
    high_flat, low_flat = abs(high_slope) < flat_per_bar, abs(low_slope) < flat_per_bar

    contraction = (first_range - last_range) / first_range
    expansion = -contraction

    setup_id: str | None = None
    change = contraction
    if expansion >= MIN_RANGE_CHANGE and high_slope > 0 and low_slope < 0:
        setup_id, change = "broadening_formation", expansion
    elif contraction >= MIN_RANGE_CHANGE:
        if high_flat and low_slope > 0:
            setup_id = "triangle_ascending"
        elif low_flat and high_slope < 0:
            setup_id = "triangle_descending"
        elif high_slope < 0 and low_slope > 0:
            setup_id = "triangle_symmetrical"
        elif high_slope < 0 and low_slope < 0:
            setup_id = "wedge_falling"
        elif high_slope > 0 and low_slope > 0:
            setup_id = "wedge_rising"
    if setup_id is None:
        return None

    broken = close > highs[-1].price or close < lows[-1].price
    confidence = graded(
        _ratio(change, MIN_RANGE_CHANGE),
        _ratio(four[-1].index - four[0].index, MIN_PIVOT_SEPARATION * 2),
    )
    return Detection(
        _tag(setup_id, confidence, "complete" if broken else "forming"), tuple(four)
    )


def detect(window: pd.DataFrame, found: list[Pivot], atr: float) -> list[Detection]:
    """Every chart pattern present at the last bar of `window`.

    Several may fire at once — a head and shoulders contains a double top, and
    both readings are defensible. They are returned together rather than ranked
    here; `TagResult` orders them by confidence.
    """
    if atr <= 0 or window.empty:
        return []

    series = alternating(found)
    if len(series) < 3:
        return []

    anchor_index = len(window) - 1
    if anchor_index - series[-1].index > MAX_BARS_SINCE_FORMATION:
        return []

    close = float(window["close"].iloc[-1])
    out: list[Detection] = []

    for kind in ("low", "high"):
        hit = _double(series[-3:], kind, atr, close)
        if hit:
            out.append(hit)

    if len(series) >= 5:
        for kind in ("high", "low"):
            hit = _head_shoulders(series[-5:], kind, atr, close)
            if hit:
                out.append(hit)

    if len(series) >= 4:
        hit = _channel_family(series[-4:], atr, close)
        if hit:
            out.append(hit)

    return out
