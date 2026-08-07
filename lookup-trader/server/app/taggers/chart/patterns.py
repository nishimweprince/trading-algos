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

Every matcher grades the *same four axes* — level agreement, clearance, time
symmetry, proportion — because `graded()` takes the weakest and a matcher
passing fewer axes therefore scores higher for reasons that have nothing to do
with match quality. An earlier version passed three axes for a double and two
for a triangle, and the triangle family won `TagResult.primary()` accordingly.

Causal by construction. Every input is a confirmed pivot (which needs `lookback`
bars after it to exist) or bars at or before the anchor.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

import pandas as pd

from app.db.setups_seed import SEED_SETUPS
from app.taggers.chart.swings import Pivot, alternating
from app.taggers.confidence import graded
from app.taggers.thresholds import (
    FLAT_SIDE_ATR,
    LEVEL_TOLERANCE_ATR,
    MAX_BARS_SINCE_FORMATION,
    MAX_BARS_SINCE_FORMATION_CAP,
    MAX_PIVOT_OFFSET,
    MIN_HEAD_OVERSHOOT_ATR,
    MIN_PIVOT_SEPARATION,
    MIN_RANGE_CHANGE,
    MIN_TIME_SYMMETRY,
    NECKLINE_TILT_ATR,
    POLE_MAX_BARS,
    POLE_MAX_RETRACE,
    POLE_MIN_ATR,
    STALE_SPAN_FRACTION,
    STEADY_RANGE_CHANGE,
)
from app.taggers.types import BarTag, TagState

_EPS = 1e-9

# Direction comes from the seeded vocabulary rather than being restated here, so
# a tag's side can never disagree with what `GET /setups` reports for it.
_SIDE: dict[str, int | None] = {setup_id: side for setup_id, _, side, _ in SEED_SETUPS}


class Detection(NamedTuple):
    """A tag plus the pivots it was built from, for rendering and audit."""

    tag: BarTag
    pivots: tuple[Pivot, ...]


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _ratio(actual: float, threshold: float) -> float:
    """How many times over the threshold a measurement clears. 1.0 = exactly met."""
    return actual / threshold if threshold > 0 else 1.0


def _agreement(actual: float, allowance: float) -> float:
    """Quality of a measurement that ought to be zero. 1.0 at the allowance, 2.0 at zero.

    The counterpart to `_ratio` for "smaller is better" dimensions, and linear
    where `_ratio(allowance, actual)` was hyperbolic. That matters: the
    hyperbolic form reached `RATIO_CAP` at half the allowance, so every
    comfortably-formed pattern pinned at a confidence of exactly 1.000 and the
    alphabetical `setup_id` tie-break decided which one `primary()` reported.
    """
    if allowance <= 0:
        return 1.0
    return 2.0 - min(actual / allowance, 2.0)


def _legs(formation: Sequence[Pivot]) -> list[int]:
    """Bars between each consecutive pair of pivots."""
    return [b.index - a.index for a, b in zip(formation, formation[1:], strict=False)]


def _symmetry(legs: Sequence[int]) -> float:
    """Shortest leg over longest, on (0, 1]. 1.0 is a perfectly even formation."""
    longest = max(legs)
    return min(legs) / longest if longest > 0 else 0.0


def _quality(deviation: float, allowance: float, clearance: float, legs: Sequence[int]) -> float:
    """The four axes every chart pattern is scored on.

    `deviation` is however far this formation departs from its own ideal — the
    gap between two levels that should match, the tilt of a side that should be
    flat — and `clearance` is how far it clears the threshold that made it this
    pattern rather than a smaller one.
    """
    return graded(
        _agreement(deviation, allowance),
        clearance,
        _ratio(_symmetry(legs), MIN_TIME_SYMMETRY),
        _ratio(min(legs), MIN_PIVOT_SEPARATION),
    )


def _tag(setup_id: str, confidence: float, state: TagState) -> BarTag:
    return BarTag(
        setup_id=setup_id,
        state=state,
        confidence=confidence,
        source="algorithm",
        side=_SIDE.get(setup_id),
    )


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def _slope(a: Pivot, b: Pivot) -> float:
    span = b.index - a.index
    return (b.price - a.price) / span if span else 0.0


def _level_at(a: Pivot, b: Pivot, index: int, allowance: float) -> float:
    """The line through two pivots evaluated at `index`, clamped near its own fit.

    A neckline or a trendline is a line, not the level of whichever pivot came
    last — on a sloping boundary the flat-level shortcut confirms a break early
    on one side and late on the other. The clamp bounds extrapolation: a slope
    fitted over ten bars says little about where the line sits sixty bars on,
    and unclamped it runs away from anything price actually traded.
    """
    low, high = min(a.price, b.price), max(a.price, b.price)
    return min(max(_level_at_raw(a, b, index), low - allowance), high + allowance)


def _level_at_raw(a: Pivot, b: Pivot, index: int) -> float:
    return a.price + _slope(a, b) * (index - a.index)


def _still_current(anchor_index: int, formation: Sequence[Pivot]) -> bool:
    """Whether the formation is still about this bar.

    The budget scales with the formation's own span. One flat allowance treated
    a sixty-bar head and shoulders and a twelve-bar double bottom alike, so the
    long patterns went stale while their neckline break was still unfolding —
    and the break is the half of a reversal's life that matters.

    Age is deliberately counted from the formation's own last pivot rather than
    from the newest pivot in the series. Aging from the newest sounds fairer to
    an offset match, but a new pivot prints every ten to twenty bars, so the
    gate would pass almost always: it put a double bottom on 12% of all bars.
    """
    span = formation[-1].index - formation[0].index
    budget = min(
        max(MAX_BARS_SINCE_FORMATION, round(span * STALE_SPAN_FRACTION)),
        MAX_BARS_SINCE_FORMATION_CAP,
    )
    return anchor_index - formation[-1].index <= budget


def _well_formed(formation: Sequence[Pivot], anchor_index: int) -> list[int] | None:
    """The formation's legs, or None if it fails the checks every pattern shares."""
    if not _still_current(anchor_index, formation):
        return None
    legs = _legs(formation)
    if min(legs) < MIN_PIVOT_SEPARATION:
        return None
    if _symmetry(legs) < MIN_TIME_SYMMETRY:
        return None
    return legs


def _reversal_state(
    kind: str, close: float, neckline: float, base: float, allowance: float
) -> TagState:
    """Where price sits relative to a reversal's neckline and the level it stands on.

    Completion is the neckline break: the formation is only a reversal once
    price has actually left it, and until then it is a shape that may still
    fail. `base` is the level the structure rests on — the pair of lows in a
    double bottom, the head in a head and shoulders. Price beyond *that* has
    destroyed the formation rather than merely declined to confirm it.
    """
    if kind == "low":
        if close > neckline:
            return "complete"
        if close < base - allowance:
            return "invalidated"
    else:
        if close < neckline:
            return "complete"
        if close > base + allowance:
            return "invalidated"
    return "forming"


# --------------------------------------------------------------------------
# Reversals
# --------------------------------------------------------------------------


def _double(
    trio: Sequence[Pivot], kind: str, anchor_index: int, atr: float, close: float
) -> Detection | None:
    """low-high-low → double bottom; high-low-high → double top."""
    first, middle, second = trio
    if first.kind != kind or second.kind != kind:
        return None
    legs = _well_formed(trio, anchor_index)
    if legs is None:
        return None

    allowance = LEVEL_TOLERANCE_ATR * atr
    gap = abs(first.price - second.price)
    if gap > allowance:
        return None
    depth = abs(middle.price - first.price)
    if depth <= allowance:
        return None

    base = min(first.price, second.price) if kind == "low" else max(first.price, second.price)
    return Detection(
        _tag(
            "double_bottom" if kind == "low" else "double_top",
            _quality(gap, allowance, _ratio(depth, allowance), legs),
            _reversal_state(kind, close, middle.price, base, allowance),
        ),
        tuple(trio),
    )


def _triple(
    five: Sequence[Pivot], kind: str, anchor_index: int, atr: float, close: float
) -> Detection | None:
    """Three touches of one level with two pullbacks between them.

    The same shape as a head and shoulders with no head: what separates them is
    whether the middle touch clears the outer two, so the ceiling here is the
    floor there and a formation can never read as both.
    """
    if [p.kind for p in five][::2] != [kind, kind, kind]:
        return None
    legs = _well_formed(five, anchor_index)
    if legs is None:
        return None

    touches = [five[0], five[2], five[4]]
    prices = [p.price for p in touches]
    allowance = LEVEL_TOLERANCE_ATR * atr
    spread = max(prices) - min(prices)
    if spread > allowance:
        return None

    middle, outer = five[2].price, (five[0].price + five[4].price) / 2
    overshoot = middle - outer if kind == "high" else outer - middle
    if overshoot >= MIN_HEAD_OVERSHOOT_ATR * atr:
        return None

    pull_a, pull_b = five[1], five[3]
    depth = min(abs(pull_a.price - prices[0]), abs(pull_b.price - prices[1]))
    if depth <= allowance:
        return None
    if abs(pull_a.price - pull_b.price) > NECKLINE_TILT_ATR * atr:
        return None

    neckline = _level_at(pull_a, pull_b, anchor_index, allowance)
    base = min(prices) if kind == "low" else max(prices)
    return Detection(
        _tag(
            "triple_bottom" if kind == "low" else "triple_top",
            _quality(spread, allowance, _ratio(depth, allowance), legs),
            _reversal_state(kind, close, neckline, base, allowance),
        ),
        tuple(five),
    )


def _head_shoulders(
    five: Sequence[Pivot], kind: str, anchor_index: int, atr: float, close: float
) -> Detection | None:
    """Five pivots whose middle extreme clears two roughly level shoulders."""
    if [p.kind for p in five][::2] != [kind, kind, kind]:
        return None
    left, neck_a, head, neck_b, right = five
    legs = _well_formed(five, anchor_index)
    if legs is None:
        return None

    overshoot = (
        min(head.price - left.price, head.price - right.price)
        if kind == "high"
        else min(left.price - head.price, right.price - head.price)
    )
    if overshoot < MIN_HEAD_OVERSHOOT_ATR * atr:
        return None

    allowance = LEVEL_TOLERANCE_ATR * atr
    shoulder_gap = abs(left.price - right.price)
    if shoulder_gap > allowance:
        return None
    # Two troughs at wildly different levels are two troughs, not a neckline.
    # Looser than the shoulders, and a guard rather than a graded axis, because
    # a neckline is entitled to slope where a pair of shoulders is not.
    if abs(neck_a.price - neck_b.price) > NECKLINE_TILT_ATR * atr:
        return None

    neckline = _level_at(neck_a, neck_b, anchor_index, allowance)
    return Detection(
        _tag(
            "head_shoulders" if kind == "high" else "inv_head_shoulders",
            _quality(
                shoulder_gap,
                allowance,
                _ratio(overshoot, MIN_HEAD_OVERSHOOT_ATR * atr),
                legs,
            ),
            _reversal_state(kind, close, neckline, head.price, allowance),
        ),
        tuple(five),
    )


# --------------------------------------------------------------------------
# Boundary geometry
# --------------------------------------------------------------------------


class _Family(NamedTuple):
    """A named boundary geometry, with the two numbers it is scored on."""

    setup_id: str
    change: float
    # How far this shape departs from its own ideal — the tilt of a side that
    # should be flat, or the imbalance between two sides that should move
    # together. In price for the flat/parallel families, and as a scale-free
    # fraction for the converging ones, which is why each carries its own
    # allowance rather than sharing one.
    deviation: float
    allowance: float


def _imbalance(high_slope: float, low_slope: float) -> float:
    """How unevenly two boundaries move, on [0, 1]. 0 is a perfectly even shape."""
    strongest = max(abs(high_slope), abs(low_slope))
    if strongest <= _EPS:
        return 0.0
    return 1.0 - min(abs(high_slope), abs(low_slope)) / strongest


def _name_family(
    high_slope: float,
    low_slope: float,
    contraction: float,
    flat: float,
    span: int,
) -> _Family | None:
    """Which boundary geometry this is, if any.

    A shrinking range is not enough — a market that merely quietened down has
    one too. The boundaries themselves have to move: lower highs, higher lows,
    or a flat side with the other closing on it. Requiring that is what took
    this from firing on 40% of bars to firing on 6%.
    """
    high_flat, low_flat = abs(high_slope) < flat, abs(low_slope) < flat
    flat_allowance = flat * span
    even = _imbalance(high_slope, low_slope)

    if -contraction >= MIN_RANGE_CHANGE and high_slope > 0 and low_slope < 0:
        return _Family("broadening_formation", -contraction, even, 1.0)

    if contraction >= MIN_RANGE_CHANGE:
        if high_flat and low_slope > 0:
            return _Family(
                "triangle_ascending", contraction, abs(high_slope) * span, flat_allowance
            )
        if low_flat and high_slope < 0:
            return _Family(
                "triangle_descending", contraction, abs(low_slope) * span, flat_allowance
            )
        if high_slope < 0 and low_slope > 0:
            return _Family("triangle_symmetrical", contraction, even, 1.0)
        if high_slope < 0 and low_slope < 0:
            return _Family("wedge_falling", contraction, even, 1.0)
        if high_slope > 0 and low_slope > 0:
            return _Family("wedge_rising", contraction, even, 1.0)
        return None

    # Neither converging nor diverging: the range held. That is a rectangle if
    # both boundaries stood still and a channel if they travelled together.
    if abs(contraction) < STEADY_RANGE_CHANGE:
        steady = _agreement(abs(contraction), STEADY_RANGE_CHANGE)
        if high_flat and low_flat:
            return _Family(
                "rectangle",
                steady,
                max(abs(high_slope), abs(low_slope)) * span,
                flat_allowance,
            )
        parallel = abs(high_slope - low_slope) * span
        if not high_flat and not low_flat and parallel < flat_allowance:
            if high_slope > 0 and low_slope > 0:
                return _Family("channel_up", steady, parallel, flat_allowance)
            if high_slope < 0 and low_slope < 0:
                return _Family("channel_down", steady, parallel, flat_allowance)
    return None


def _channel_family(
    four: Sequence[Pivot], anchor_index: int, atr: float, close: float
) -> Detection | None:
    """Name the boundary geometry of two highs and two lows."""
    highs = [p for p in four if p.kind == "high"]
    lows = [p for p in four if p.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    legs = _well_formed(four, anchor_index)
    if legs is None:
        return None

    first_range = abs(highs[0].price - lows[0].price)
    if first_range <= 0:
        return None
    last_range = abs(highs[-1].price - lows[-1].price)
    contraction = (first_range - last_range) / first_range

    span = max(four[-1].index - four[0].index, 1)
    high_slope, low_slope = _slope(highs[0], highs[-1]), _slope(lows[0], lows[-1])
    family = _name_family(high_slope, low_slope, contraction, FLAT_SIDE_ATR * atr / span, span)
    if family is None:
        return None

    # The boundary at the anchor, not at whichever pivot came last — on a
    # converging shape the last pivot sits well outside the line by now, which
    # is why breakouts used to register several bars late.
    allowance = LEVEL_TOLERANCE_ATR * atr
    upper = _level_at(highs[0], highs[-1], anchor_index, allowance)
    lower = _level_at(lows[0], lows[-1], anchor_index, allowance)
    broken = close > upper or close < lower

    is_steady = family.setup_id in ("rectangle", "channel_up", "channel_down")
    clearance = family.change if is_steady else _ratio(family.change, MIN_RANGE_CHANGE)
    return Detection(
        _tag(
            family.setup_id,
            _quality(family.deviation, family.allowance, clearance, legs),
            "complete" if broken else "forming",
        ),
        tuple(four),
    )


# --------------------------------------------------------------------------
# Continuation
# --------------------------------------------------------------------------


def _pole(window: pd.DataFrame, start: int, atr: float) -> float:
    """Signed displacement in ATR of the run into `start`, over at most POLE_MAX_BARS."""
    origin = max(start - POLE_MAX_BARS, 0)
    if origin >= start:
        return 0.0
    closes = window["close"]
    return (float(closes.iloc[start]) - float(closes.iloc[origin])) / atr


def _pole_consolidation(
    window: pd.DataFrame, four: Sequence[Pivot], anchor_index: int, atr: float, close: float
) -> Detection | None:
    """A tight pause after a fast move — a flag if parallel, a pennant if converging.

    The pole is the load-bearing half. Without it these are just small ranges,
    and small ranges are everywhere; requiring several ATR of travel in under
    twenty bars is what keeps this from firing constantly.
    """
    highs = [p for p in four if p.kind == "high"]
    lows = [p for p in four if p.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    legs = _well_formed(four, anchor_index)
    if legs is None:
        return None

    pole = _pole(window, four[0].index, atr)
    if abs(pole) < POLE_MIN_ATR:
        return None
    up = pole > 0

    first_range = abs(highs[0].price - lows[0].price)
    if first_range <= 0:
        return None
    contraction = (first_range - abs(highs[-1].price - lows[-1].price)) / first_range

    span = max(four[-1].index - four[0].index, 1)
    flat = FLAT_SIDE_ATR * atr / span
    high_slope, low_slope = _slope(highs[0], highs[-1]), _slope(lows[0], lows[-1])

    # A pause drifts against the pole or holds level; one that runs *with* it is
    # the move continuing, not consolidating.
    drift = (high_slope + low_slope) / 2
    if up and drift > flat:
        return None
    if not up and drift < -flat:
        return None

    # And it has to stay a pause. Past roughly two thirds of the pole given
    # back, the move is being reversed rather than digested.
    origin = float(window["close"].iloc[max(four[0].index - POLE_MAX_BARS, 0)])
    launch = float(window["close"].iloc[four[0].index])
    held = min(lows, key=lambda p: p.price) if up else max(highs, key=lambda p: p.price)
    if abs(launch - held.price) > POLE_MAX_RETRACE * abs(launch - origin):
        return None

    parallel = abs(high_slope - low_slope) * span
    if contraction >= MIN_RANGE_CHANGE and high_slope < 0 < low_slope:
        setup_id = "pennant_bullish" if up else "pennant_bearish"
        deviation, allowance = _imbalance(high_slope, low_slope), 1.0
    elif abs(contraction) < STEADY_RANGE_CHANGE and parallel < flat * span:
        setup_id = "flag_bullish" if up else "flag_bearish"
        deviation, allowance = parallel, flat * span
    else:
        return None

    upper = _level_at(highs[0], highs[-1], anchor_index, LEVEL_TOLERANCE_ATR * atr)
    lower = _level_at(lows[0], lows[-1], anchor_index, LEVEL_TOLERANCE_ATR * atr)
    if up:
        state: TagState = "complete" if close > upper else "forming"
    else:
        state = "complete" if close < lower else "forming"

    return Detection(
        _tag(
            setup_id,
            _quality(deviation, allowance, _ratio(abs(pole), POLE_MIN_ATR), legs),
            state,
        ),
        tuple(four),
    )


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def _reversals(
    series: Sequence[Pivot], anchor_index: int, atr: float, close: float
) -> list[Detection]:
    """Reversals whose last pivot is the last of `series`."""
    out: list[Detection | None] = []
    for kind in ("low", "high"):
        out.append(_double(series[-3:], kind, anchor_index, atr, close))
    if len(series) >= 5:
        for kind in ("high", "low"):
            out.append(_head_shoulders(series[-5:], kind, anchor_index, atr, close))
            out.append(_triple(series[-5:], kind, anchor_index, atr, close))
    return [hit for hit in out if hit is not None]


def _boundaries(
    window: pd.DataFrame,
    series: Sequence[Pivot],
    anchor_index: int,
    atr: float,
    close: float,
) -> list[Detection]:
    """Whatever the four most recent pivots bound — a triangle, a channel, a flag."""
    if len(series) < 4:
        return []
    four = series[-4:]
    # A bull flag *is* a small falling channel — same four pivots, same slopes,
    # opposite `side`. Letting both stand would put a +1 and a -1 tag on one bar
    # and leave the ranking to decide which the operator sees, so the reading
    # that knows about the pole supersedes the one that does not.
    hit = _pole_consolidation(window, four, anchor_index, atr, close) or _channel_family(
        four, anchor_index, atr, close
    )
    return [hit] if hit is not None else []


def _keep(best: dict[str, Detection], hit: Detection) -> None:
    """One reading per setup: the best-formed, and among equals the most recent."""
    prior = best.get(hit.tag.setup_id)
    rank = (hit.tag.confidence, hit.pivots[-1].index)
    if prior is None or rank > (prior.tag.confidence, prior.pivots[-1].index):
        best[hit.tag.setup_id] = hit


def detect(window: pd.DataFrame, found: list[Pivot], atr: float) -> list[Detection]:
    """Every chart pattern present at the last bar of `window`.

    Reversals are searched back a bounded number of pivots rather than only at
    the tail. One carves a fresh pivot the moment it breaks its neckline and
    another when it pulls back to it, so a tail-only search lost the pattern
    exactly as it completed — and then named the pullback as a wedge pointing
    the other way.

    Boundary geometry gets no such offset. A triangle is defined by the lines
    its most recent pivots sit on, so a match set back a few pivots is not a
    triangle completing, only one that ended a while ago — which is precisely
    the staleness `_still_current` exists to exclude.

    Several patterns may fire at once — a head and shoulders contains a double
    top, and both readings are defensible. They are returned together rather
    than ranked here; `TagResult` orders them by confidence.
    """
    if atr <= 0 or window.empty:
        return []

    series = alternating(found)
    if len(series) < 3:
        return []

    anchor_index = len(window) - 1
    close = float(window["close"].iloc[-1])

    best: dict[str, Detection] = {}
    for back in range(MAX_PIVOT_OFFSET + 1):
        end = len(series) - back
        if end < 3:
            break
        for hit in _reversals(series[:end], anchor_index, atr, close):
            _keep(best, hit)
    for hit in _boundaries(window, series, anchor_index, atr, close):
        _keep(best, hit)

    return [best[setup_id] for setup_id in sorted(best)]
