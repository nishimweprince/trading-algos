"""Run every rule against the anchor bar.

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
from app.taggers.rules import RULES
from app.taggers.types import Bar, TagResult

TAG_LOOKBACK = 3
"""Bars the deepest rule needs: `inside_break` reads mother, inside and break."""

_OHLC = ["open", "high", "low", "close"]


def tag_bar(window: pd.DataFrame, atr_at_bar: float | None) -> TagResult:
    """Rule tags for the LAST bar of `window`.

    `atr_at_bar` normalises every size threshold. Without it a rule would compare
    a gold range against a euro range, so a missing ATR means no tags rather than
    tags on an arbitrary scale.
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
    return TagResult.of(tags, version)
