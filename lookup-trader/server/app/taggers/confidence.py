"""How good an example of the pattern this bar is.

`confidence` is how far past the bar of qualification an instance sits, on
[0.6, 1.0]. It is a match-quality score. It is not a probability of anything, and
specifically not a probability that the trade works — that is what `/base-rate`
is for, and the two numbers will end up rendered close enough together that the
distinction has to be stated rather than assumed.

The predicate firing is binary and certain; what is uncertain is whether this
instance is a good example, and that is a continuum.
"""

from __future__ import annotations

CONFIDENCE_FLOOR = 0.60
"""Score of a bar that exactly meets every threshold and exceeds none."""

CONFIDENCE_SPAN = 1.00
"""Ratio surplus above 1.0 that reaches a full score."""

RATIO_CAP = 2.00
"""Ceiling on any single ratio, so one runaway dimension cannot carry the score."""


def graded(*ratios: float) -> float:
    """Score from per-dimension ratios, each >= 1.0 iff its predicate holds.

    The weakest dimension decides. A bar cannot buy a high score with one
    spectacular measurement while barely scraping another — which is exactly the
    bar a reader would call a marginal example.

    The cap matters for degenerate inputs: a doji drives `threshold / body_pct`
    towards infinity, and without it a bar with no body at all would score 1.0.
    """
    weakest = min(min(r, RATIO_CAP) for r in ratios)
    scaled = min(1.0, max(0.0, (weakest - 1.0) / CONFIDENCE_SPAN))
    # Rounded so the serialized JSON is byte-stable and the stored tags can be
    # compared against a live recomputation as strings.
    return round(CONFIDENCE_FLOOR + (1.0 - CONFIDENCE_FLOOR) * scaled, 3)
