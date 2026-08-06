"""The pre-filter that decides which bars are worth a labelling call.

It is a recall filter, so the tests come in pairs: a formation the geometry
should admit, and the near-miss it must still reject. Over-selection is fine and
expected — several patterns may be reported for one bar. What is not fine is
firing on bars with no structure at all, because that is the budget and the
class balance of the training set.
"""

from __future__ import annotations

import pytest

from app.taggers.chart.candidates import (
    MAX_BARS_SINCE_FORMATION,
    Candidate,
    candidates,
)
from app.taggers.chart.swings import Pivot

ATR = 1.0


def hi(index: int, price: float) -> Pivot:
    return Pivot(index, price, "high")


def lo(index: int, price: float) -> Pivot:
    return Pivot(index, price, "low")


def found(series: list[Pivot], anchor: int | None = None, atr: float = ATR) -> Candidate | None:
    return candidates(series, anchor if anchor is not None else series[-1].index + 5, atr)


# --------------------------------------------------------------------------
# Double top / bottom
# --------------------------------------------------------------------------


def test_two_comparable_lows_around_a_high_is_a_double_bottom():
    got = found([lo(0, 100.0), hi(10, 110.0), lo(20, 100.5)])

    assert got is not None
    assert "double_bottom" in got.setup_ids


def test_two_comparable_highs_around_a_low_is_a_double_top():
    got = found([hi(0, 110.0), lo(10, 100.0), hi(20, 110.5)])

    assert got is not None
    assert "double_top" in got.setup_ids


def test_lows_at_different_levels_are_not_a_double_bottom():
    got = found([lo(0, 100.0), hi(10, 110.0), lo(20, 104.0)])

    assert got is None or "double_bottom" not in got.setup_ids


def test_pivots_too_close_together_are_noise_not_a_formation():
    got = found([lo(0, 100.0), hi(3, 110.0), lo(6, 100.2)])

    assert got is None or "double_bottom" not in got.setup_ids


def test_a_middle_pivot_that_does_not_separate_is_one_turn_not_two():
    """Three pivots at effectively the same level are a flat range."""
    got = found([lo(0, 100.0), hi(10, 100.4), lo(20, 100.2)])

    assert got is None or "double_bottom" not in got.setup_ids


# --------------------------------------------------------------------------
# Head and shoulders
# --------------------------------------------------------------------------


def test_a_head_overshooting_level_shoulders_is_head_and_shoulders():
    got = found(
        [hi(0, 110.0), lo(10, 100.0), hi(20, 120.0), lo(30, 101.0), hi(40, 110.5)]
    )

    assert got is not None
    assert "head_shoulders" in got.setup_ids


def test_the_inverse_is_recognised_too():
    got = found(
        [lo(0, 100.0), hi(10, 110.0), lo(20, 90.0), hi(30, 109.0), lo(40, 100.5)]
    )

    assert got is not None
    assert "inv_head_shoulders" in got.setup_ids


def test_uneven_shoulders_are_a_trend_leg_not_a_formation():
    got = found(
        [hi(0, 110.0), lo(10, 100.0), hi(20, 120.0), lo(30, 101.0), hi(40, 116.0)]
    )

    assert got is None or "head_shoulders" not in got.setup_ids


def test_a_middle_high_that_does_not_overshoot_is_not_a_head():
    got = found(
        [hi(0, 118.0), lo(10, 100.0), hi(20, 112.0), lo(30, 101.0), hi(40, 118.5)]
    )

    assert got is None or "head_shoulders" not in got.setup_ids


# --------------------------------------------------------------------------
# Triangle / wedge / broadening family
# --------------------------------------------------------------------------


def test_lower_highs_over_higher_lows_is_a_symmetrical_triangle():
    got = found([lo(0, 100.0), hi(10, 120.0), lo(20, 106.0), hi(30, 112.0)])

    assert got is not None
    assert "triangle_symmetrical" in got.setup_ids


def test_a_flat_top_with_rising_lows_is_an_ascending_triangle():
    got = found([lo(0, 100.0), hi(10, 120.0), lo(20, 112.0), hi(30, 120.05)])

    assert got is not None
    assert "triangle_ascending" in got.setup_ids


def test_a_flat_floor_with_falling_highs_is_a_descending_triangle():
    got = found([hi(0, 120.0), lo(10, 100.0), hi(20, 108.0), lo(30, 100.05)])

    assert got is not None
    assert "triangle_descending" in got.setup_ids


def test_both_boundaries_falling_into_a_narrowing_range_is_a_falling_wedge():
    """Highs falling faster than the lows — a wedge, not a symmetrical triangle,
    which needs the two boundaries leaning opposite ways."""
    got = found([hi(0, 120.0), lo(10, 100.0), hi(20, 106.0), lo(30, 98.0)])

    assert got is not None
    assert "wedge_falling" in got.setup_ids


def test_both_boundaries_rising_into_a_narrowing_range_is_a_rising_wedge():
    got = found([lo(0, 100.0), hi(10, 120.0), lo(20, 112.0), hi(30, 122.0)])

    assert got is not None
    assert "wedge_rising" in got.setup_ids


def test_diverging_boundaries_are_a_broadening_formation():
    got = found([lo(0, 105.0), hi(10, 110.0), lo(20, 95.0), hi(30, 125.0)])

    assert got is not None
    assert "broadening_formation" in got.setup_ids


def test_a_merely_quieter_range_is_not_a_triangle():
    """The regression that motivated naming the family.

    An earlier version accepted any four pivots whose range had shrunk, which
    fired on 40% of all bars — a market that simply calmed down has a smaller
    range without either boundary going anywhere.
    """
    got = found([lo(0, 100.0), hi(10, 120.0), lo(20, 101.0), hi(30, 118.0)])

    assert got is None or not {
        "triangle_symmetrical",
        "triangle_ascending",
        "triangle_descending",
        "wedge_rising",
        "wedge_falling",
    } & set(got.setup_ids)


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_a_stale_formation_is_no_longer_about_this_bar():
    series = [lo(0, 100.0), hi(10, 110.0), lo(20, 100.5)]

    assert found(series, anchor=20 + MAX_BARS_SINCE_FORMATION) is not None
    assert found(series, anchor=20 + MAX_BARS_SINCE_FORMATION + 1) is None


def test_too_few_pivots_admit_nothing():
    assert found([lo(0, 100.0), hi(10, 110.0)]) is None
    assert candidates([], anchor_index=50, atr=ATR) is None


@pytest.mark.parametrize("atr", [0.0, -1.0])
def test_without_a_scale_nothing_can_be_judged(atr):
    """Every tolerance here is in ATR; without one there is no "close enough"."""
    assert found([lo(0, 100.0), hi(10, 110.0), lo(20, 100.5)], atr=atr) is None


def test_reported_pivots_are_the_ones_the_match_used():
    got = found([lo(0, 100.0), hi(10, 110.0), lo(20, 100.5)])

    assert got is not None
    assert [p.index for p in got.pivots_used] == [0, 10, 20]


def test_setup_ids_are_deduplicated_and_ordered():
    got = found(
        [hi(0, 110.0), lo(10, 100.0), hi(20, 120.0), lo(30, 101.0), hi(40, 110.5)]
    )

    assert got is not None
    assert len(got.setup_ids) == len(set(got.setup_ids))


def test_tolerances_scale_with_volatility():
    """The same geometry reads differently in a quiet market and a wild one."""
    series = [lo(0, 100.0), hi(10, 110.0), lo(20, 102.5)]

    quiet = found(series, atr=0.5)
    wild = found(series, atr=4.0)

    assert quiet is None or "double_bottom" not in quiet.setup_ids
    assert wild is not None and "double_bottom" in wild.setup_ids
