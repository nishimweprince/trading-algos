"""The pivot sequence a chart pattern is built out of.

`context.swing_indices` answers "where was the last swing high and low", which is
all the context half needs. A chart pattern needs the whole series: a double
bottom is two comparable lows with a high between them, and head and shoulders is
five pivots in a particular arrangement. This module returns that series.

Confirmation follows `swing_indices` exactly — a pivot must be the extreme of the
`lookback` bars either side of it — so the final `lookback` bars of a window can
never be pivots. That is what keeps the sequence causal: a pivot is only ever
recognised once the bars that confirm it have already closed.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

import numpy as np
import pandas as pd

PivotKind = Literal["high", "low"]


class Pivot(NamedTuple):
    """One confirmed swing, positioned within the window it was found in."""

    index: int
    price: float
    kind: PivotKind


def pivots(window: pd.DataFrame, lookback: int) -> list[Pivot]:
    """Every confirmed swing high and low in `window`, in time order.

    A flat stretch makes every bar in it simultaneously the max and the min of
    its neighbourhood. Those bars are not turning points, so a bar that qualifies
    as both is recorded as neither — otherwise a quiet range would generate a
    dense run of contradictory pivots and every pattern matcher downstream would
    have to filter it out again.
    """
    if len(window) < lookback * 2 + 1:
        return []

    highs = window["high"].to_numpy(dtype=float)
    lows = window["low"].to_numpy(dtype=float)
    if not (np.isfinite(highs).all() and np.isfinite(lows).all()):
        return []

    found: list[Pivot] = []
    end = len(window) - 1
    for i in range(lookback, end - lookback + 1):
        seg = slice(i - lookback, i + lookback + 1)
        is_high = highs[i] == highs[seg].max()
        is_low = lows[i] == lows[seg].min()
        if is_high == is_low:
            continue
        if is_high:
            found.append(Pivot(i, float(highs[i]), "high"))
        else:
            found.append(Pivot(i, float(lows[i]), "low"))
    return found


def alternating(found: list[Pivot]) -> list[Pivot]:
    """Reduce a pivot series to strictly alternating highs and lows.

    A plateau or a stepped advance produces several same-kind pivots in a row.
    Every pattern this feeds is described in terms of turning points, so a run of
    highs is one turning point — the highest of them — not several. Ties keep the
    earliest bar, so the reduction is deterministic and the retained pivot is the
    one that was confirmed first.
    """
    out: list[Pivot] = []
    for pivot in found:
        if not out or out[-1].kind != pivot.kind:
            out.append(pivot)
            continue
        prev = out[-1]
        better = pivot.price > prev.price if pivot.kind == "high" else pivot.price < prev.price
        if better:
            out[-1] = pivot
    return out


def last_pivots(found: list[Pivot], count: int) -> list[Pivot]:
    """The `count` most recent pivots, oldest first. Fewer if there aren't enough."""
    return found[-count:] if count > 0 else []
