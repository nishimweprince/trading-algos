"""Run every tagger against the anchor bar.

Two stages, both causal. Candlestick rules read the last few bars directly; chart
patterns read the confirmed pivot sequence over a longer window. Neither can see
past the anchor — the rules because they are handed a fixed-length tail, the
patterns because a pivot needs bars after it to be confirmed and therefore can
never sit at the anchor.

The signature is the whole safety argument: `tag_bar` takes a window that ends at
the anchor and nothing else. There is no forward frame and no (frame, index)
pair, so a tagger cannot reach a bar it should not see — the same structural
guarantee `context_half` gives, rather than a test that has to catch a mistake
after it is made.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.config import settings
from app.taggers.chart.patterns import detect
from app.taggers.chart.swings import pivots
from app.taggers.rules import RULES
from app.taggers.thresholds import SWING_LOOKBACK
from app.taggers.types import Bar, TagResult

TAG_LOOKBACK = 3
"""Bars the deepest rule needs: `inside_break` reads mother, inside and break."""

CHART_WINDOW = 300
"""Bars the pivot scan runs over — about a fortnight of H1, or two months of H4.

Wide enough that a five-pivot formation can still be found once several pivots
have printed since it completed, which is what `MAX_PIVOT_OFFSET` searches back
through; at 180 the earliest pivot of an offset match fell off the front."""

_OHLC = ["open", "high", "low", "close"]


def tag_bar(window: pd.DataFrame, atr_at_bar: float | None) -> TagResult:
    """Every tag on the LAST bar of `window` — candlestick rules and chart patterns.

    `atr_at_bar` normalises every size threshold. Without it a tagger would
    compare a gold range against a euro range, so a missing ATR means no tags
    rather than tags on an arbitrary scale.
    """
    version = settings.bar_feature_version
    if not atr_at_bar or atr_at_bar <= 0:
        return TagResult.empty(version)
    if len(window) < 2:
        return TagResult.empty(version)

    values = window.iloc[-TAG_LOOKBACK:][_OHLC].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        return TagResult.empty(version)
    bars = [Bar(*row) for row in values]

    atr = float(atr_at_bar)
    tags = [tag for tag in (rule(bars, atr) for rule in RULES) if tag is not None]
    tags += [d.tag for d in _chart_tags(window, atr)]
    return TagResult.of(tags, version)


def _chart_tags(window: pd.DataFrame, atr: float):
    """Chart patterns over the trailing window, or nothing if it is too short."""
    chart_window = window.iloc[-CHART_WINDOW:]
    if len(chart_window) < SWING_LOOKBACK * 2 + 1:
        return []
    return detect(chart_window, pivots(chart_window, SWING_LOOKBACK), atr)
