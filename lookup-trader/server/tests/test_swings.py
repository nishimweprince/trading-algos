"""The pivot sequence chart patterns are built from.

The causal property is the one that matters: a pivot needs `lookback` bars after
it to be confirmed, so the tail of a window can never contain one. A matcher that
saw a pivot on the anchor bar would be reading bars that have not closed.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services.context import swing_indices
from app.taggers.chart.swings import Pivot, alternating, last_pivots, pivots

LOOKBACK = 2


def _window(highs: list[float], lows: list[float] | None = None) -> pd.DataFrame:
    lows = lows if lows is not None else [h - 1.0 for h in highs]
    return pd.DataFrame({"high": highs, "low": lows})


def _zigzag(peaks: list[float], troughs: list[float], spacing: int = 5) -> pd.DataFrame:
    """A clean alternating series: trough, peak, trough, peak, … at fixed spacing."""
    highs: list[float] = []
    lows: list[float] = []
    for i in range(len(peaks) + len(troughs)):
        extreme = troughs[i // 2] if i % 2 == 0 else peaks[i // 2]
        for j in range(spacing):
            # Ramp into and out of each extreme so it is a strict local turn.
            offset = abs(j - spacing // 2)
            if i % 2 == 0:
                lows.append(extreme + offset)
                highs.append(extreme + offset + 1)
            else:
                highs.append(extreme - offset)
                lows.append(extreme - offset - 1)
    return pd.DataFrame({"high": highs, "low": lows})


def test_a_clean_zigzag_yields_alternating_pivots():
    found = alternating(pivots(_zigzag([20, 21], [10, 11]), LOOKBACK))

    assert len(found) >= 3
    assert all(a.kind != b.kind for a, b in zip(found, found[1:]))
    assert [p.index for p in found] == sorted(p.index for p in found)


def test_the_tail_can_never_hold_a_pivot():
    """The causal guarantee: the last `lookback` bars are unconfirmable."""
    frame = _zigzag([20, 21, 22], [10, 11, 12])
    found = pivots(frame, LOOKBACK)

    assert found, "fixture produced no pivots"
    last_confirmable = len(frame) - 1 - LOOKBACK
    assert max(p.index for p in found) <= last_confirmable


def test_appending_bars_never_rewrites_a_confirmed_pivot():
    """A pivot already confirmed is settled — later bars only add to the series.

    This is the property that lets a pattern matcher run once per bar without
    re-deriving history, and it is why the tail exclusion above has to hold.
    """
    frame = _zigzag([20, 21, 22], [10, 11, 12])
    prefix = frame.iloc[: len(frame) - 8]

    earlier = pivots(prefix, LOOKBACK)
    later = pivots(frame, LOOKBACK)

    assert earlier == later[: len(earlier)]


def test_the_last_high_and_low_agree_with_swing_indices():
    """Same confirmation rule as the context half — the two must not drift.

    They agree wherever a bar is unambiguously a high or a low. The one deliberate
    divergence is a bar that is both (a flat stretch): `swing_indices` records it
    as each, this module records it as neither. A zigzag has no such bar.
    """
    frame = _zigzag([20, 21, 22], [10, 11, 12])
    found = pivots(frame, LOOKBACK)

    highs = [p.index for p in found if p.kind == "high"]
    lows = [p.index for p in found if p.kind == "low"]
    expected_high, expected_low = swing_indices(frame, LOOKBACK)

    assert (highs[-1] if highs else None) == expected_high
    assert (lows[-1] if lows else None) == expected_low


def test_a_flat_stretch_produces_no_pivots():
    """Every bar in a flat range is both the max and the min of its neighbours;
    none of them is a turning point."""
    assert pivots(_window([10.0] * 20, [9.0] * 20), LOOKBACK) == []


def test_a_window_shorter_than_the_confirmation_span_has_no_pivots():
    assert pivots(_window([1.0, 2.0, 3.0]), LOOKBACK) == []


def test_non_finite_prices_are_refused_rather_than_ranked():
    frame = _zigzag([20, 21], [10, 11])
    frame.loc[3, "high"] = float("nan")

    assert pivots(frame, LOOKBACK) == []


def test_alternating_keeps_the_extreme_of_a_same_kind_run():
    run = [
        Pivot(2, 10.0, "high"),
        Pivot(5, 12.0, "high"),
        Pivot(8, 11.0, "high"),
        Pivot(11, 3.0, "low"),
    ]

    assert alternating(run) == [Pivot(5, 12.0, "high"), Pivot(11, 3.0, "low")]


def test_alternating_keeps_the_deepest_of_a_low_run():
    run = [Pivot(2, 5.0, "low"), Pivot(5, 3.0, "low"), Pivot(8, 4.0, "low")]

    assert alternating(run) == [Pivot(5, 3.0, "low")]


def test_alternating_breaks_ties_toward_the_earlier_pivot():
    """Deterministic reduction: the pivot confirmed first is the one retained."""
    run = [Pivot(2, 10.0, "high"), Pivot(5, 10.0, "high")]

    assert alternating(run) == [Pivot(2, 10.0, "high")]


def test_alternating_is_idempotent():
    found = pivots(_zigzag([20, 21, 22], [10, 11, 12]), LOOKBACK)
    once = alternating(found)

    assert alternating(once) == once


def test_last_pivots_returns_the_tail_oldest_first():
    run = [Pivot(i, float(i), "high" if i % 2 else "low") for i in range(6)]

    assert last_pivots(run, 3) == run[3:]
    assert last_pivots(run, 99) == run
    assert last_pivots(run, 0) == []


@pytest.mark.parametrize("lookback", [1, 2, 3, 5])
def test_the_tail_exclusion_scales_with_lookback(lookback):
    frame = _zigzag([20, 21, 22, 23], [10, 11, 12, 13], spacing=9)
    found = pivots(frame, lookback)

    assert found, f"no pivots at lookback={lookback}"
    assert max(p.index for p in found) <= len(frame) - 1 - lookback
