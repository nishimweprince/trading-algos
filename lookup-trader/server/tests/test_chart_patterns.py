"""Chart patterns detected from the pivot sequence.

Tests come in pairs: a formation the geometry should find, and the near-miss it
must reject. Detection is only half of it — a formation is `forming` until price
leaves it, and that distinction decides whether the tag reaches a base rate.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.db.setups_seed import SEED_SETUPS
from app.taggers.chart.patterns import (
    MAX_BARS_SINCE_FORMATION,
    detect,
)
from app.taggers.chart.swings import Pivot

ATR = 1.0


def hi(index: int, price: float) -> Pivot:
    return Pivot(index, price, "high")


def lo(index: int, price: float) -> Pivot:
    return Pivot(index, price, "low")


def _window(length: int, close: float) -> pd.DataFrame:
    """A frame long enough to place the anchor, carrying only the close that matters."""
    return pd.DataFrame({"close": [close] * length})


def tags(
    series: list[Pivot],
    close: float,
    anchor: int | None = None,
    atr: float = ATR,
) -> dict[str, object]:
    """Detected tags keyed by setup id, for the anchor sitting `anchor` bars along."""
    end = anchor if anchor is not None else series[-1].index + 5
    found = detect(_window(end + 1, close), series, atr)
    return {d.tag.setup_id: d.tag for d in found}


# --------------------------------------------------------------------------
# Double top / bottom
# --------------------------------------------------------------------------

DOUBLE_BOTTOM = [lo(0, 100.0), hi(10, 110.0), lo(20, 100.5)]
DOUBLE_TOP = [hi(0, 110.0), lo(10, 100.0), hi(20, 110.5)]


def test_two_comparable_lows_around_a_high_is_a_double_bottom():
    assert "double_bottom" in tags(DOUBLE_BOTTOM, close=105.0)


def test_two_comparable_highs_around_a_low_is_a_double_top():
    assert "double_top" in tags(DOUBLE_TOP, close=105.0)


def test_lows_at_different_levels_are_not_a_double_bottom():
    assert "double_bottom" not in tags(
        [lo(0, 100.0), hi(10, 110.0), lo(20, 104.0)], close=105.0
    )


def test_pivots_too_close_together_are_noise_not_a_formation():
    assert "double_bottom" not in tags(
        [lo(0, 100.0), hi(3, 110.0), lo(6, 100.2)], close=105.0
    )


def test_a_middle_pivot_that_does_not_separate_is_one_turn_not_two():
    assert "double_bottom" not in tags(
        [lo(0, 100.0), hi(10, 100.4), lo(20, 100.2)], close=100.3
    )


# --------------------------------------------------------------------------
# Completion — the neckline break
# --------------------------------------------------------------------------


def test_a_double_bottom_is_forming_until_price_clears_the_neckline():
    assert tags(DOUBLE_BOTTOM, close=105.0)["double_bottom"].state == "forming"
    assert tags(DOUBLE_BOTTOM, close=111.0)["double_bottom"].state == "complete"


def test_a_double_top_completes_downward():
    assert tags(DOUBLE_TOP, close=105.0)["double_top"].state == "forming"
    assert tags(DOUBLE_TOP, close=99.0)["double_top"].state == "complete"


def test_head_and_shoulders_completes_below_the_later_trough():
    series = [hi(0, 110.0), lo(10, 100.0), hi(20, 120.0), lo(30, 101.0), hi(40, 110.5)]

    assert tags(series, close=105.0)["head_shoulders"].state == "forming"
    assert tags(series, close=100.5)["head_shoulders"].state == "complete"


# --------------------------------------------------------------------------
# Head and shoulders
# --------------------------------------------------------------------------


def test_a_head_overshooting_level_shoulders_is_head_and_shoulders():
    assert "head_shoulders" in tags(
        [hi(0, 110.0), lo(10, 100.0), hi(20, 120.0), lo(30, 101.0), hi(40, 110.5)],
        close=105.0,
    )


def test_the_inverse_is_recognised_too():
    assert "inv_head_shoulders" in tags(
        [lo(0, 100.0), hi(10, 110.0), lo(20, 90.0), hi(30, 109.0), lo(40, 100.5)],
        close=105.0,
    )


def test_uneven_shoulders_are_a_trend_leg_not_a_formation():
    assert "head_shoulders" not in tags(
        [hi(0, 110.0), lo(10, 100.0), hi(20, 120.0), lo(30, 101.0), hi(40, 116.0)],
        close=105.0,
    )


def test_a_head_that_barely_clears_its_shoulders_is_not_a_head():
    """Any higher high used to qualify; it now has to clear by half an ATR."""
    assert "head_shoulders" not in tags(
        [hi(0, 110.0), lo(10, 100.0), hi(20, 110.2), lo(30, 101.0), hi(40, 110.1)],
        close=105.0,
    )


# --------------------------------------------------------------------------
# Triangle / wedge / broadening family
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "series,expected",
    [
        ([lo(0, 100.0), hi(10, 120.0), lo(20, 106.0), hi(30, 112.0)], "triangle_symmetrical"),
        ([lo(0, 100.0), hi(10, 120.0), lo(20, 112.0), hi(30, 120.05)], "triangle_ascending"),
        ([hi(0, 120.0), lo(10, 100.0), hi(20, 108.0), lo(30, 100.05)], "triangle_descending"),
        ([hi(0, 120.0), lo(10, 100.0), hi(20, 106.0), lo(30, 98.0)], "wedge_falling"),
        ([lo(0, 100.0), hi(10, 120.0), lo(20, 112.0), hi(30, 122.0)], "wedge_rising"),
        ([lo(0, 105.0), hi(10, 110.0), lo(20, 95.0), hi(30, 125.0)], "broadening_formation"),
    ],
)
def test_the_boundary_geometry_names_the_family(series, expected):
    assert expected in tags(series, close=110.0)


def test_a_merely_quieter_range_is_not_a_triangle():
    """The regression that motivated naming the family.

    An earlier version accepted any four pivots whose range had shrunk, which
    fired on 40% of all bars — a market that simply calmed down has a smaller
    range without either boundary going anywhere.
    """
    got = tags([lo(0, 100.0), hi(10, 120.0), lo(20, 101.0), hi(30, 118.0)], close=110.0)

    assert not {
        "triangle_symmetrical",
        "triangle_ascending",
        "triangle_descending",
        "wedge_rising",
        "wedge_falling",
    } & set(got)


# --------------------------------------------------------------------------
# Tag shape
# --------------------------------------------------------------------------


def test_every_chart_tag_is_attributed_to_the_algorithm():
    got = tags(DOUBLE_BOTTOM, close=105.0)["double_bottom"]

    assert got.source == "algorithm"
    assert got.model_version is None


def test_side_comes_from_the_seeded_vocabulary():
    """A tag's direction must not disagree with what `GET /setups` reports."""
    seeded = {setup_id: side for setup_id, _, side, _ in SEED_SETUPS}
    series = [hi(0, 110.0), lo(10, 100.0), hi(20, 120.0), lo(30, 101.0), hi(40, 110.5)]

    for setup_id, tag in tags(series, close=105.0).items():
        assert tag.side == seeded[setup_id], setup_id


def test_confidence_stays_on_the_shared_match_quality_scale():
    from app.taggers.confidence import CONFIDENCE_FLOOR

    for tag in tags(DOUBLE_BOTTOM, close=105.0).values():
        assert CONFIDENCE_FLOOR <= tag.confidence <= 1.0


def test_a_cleaner_match_scores_higher_than_a_marginal_one():
    marginal = tags([lo(0, 100.0), hi(10, 102.5), lo(20, 100.95)], close=101.0)
    clean = tags([lo(0, 100.0), hi(10, 115.0), lo(20, 100.02)], close=105.0)

    assert clean["double_bottom"].confidence > marginal["double_bottom"].confidence


def test_several_readings_of_one_bar_are_all_returned():
    """A head and shoulders contains a symmetrical triangle; both are defensible
    and the ranking belongs to TagResult, not here."""
    got = tags(
        [hi(0, 110.0), lo(10, 100.0), hi(20, 120.0), lo(30, 101.0), hi(40, 110.5)],
        close=105.0,
    )

    assert len(got) > 1


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_a_stale_formation_is_no_longer_about_this_bar():
    assert tags(DOUBLE_BOTTOM, close=105.0, anchor=20 + MAX_BARS_SINCE_FORMATION)
    assert not tags(DOUBLE_BOTTOM, close=105.0, anchor=20 + MAX_BARS_SINCE_FORMATION + 1)


def test_too_few_pivots_admit_nothing():
    assert not tags([lo(0, 100.0), hi(10, 110.0)], close=105.0)
    assert detect(_window(50, 105.0), [], ATR) == []


@pytest.mark.parametrize("atr", [0.0, -1.0])
def test_without_a_scale_nothing_can_be_judged(atr):
    """Every tolerance here is in ATR; without one there is no "close enough"."""
    assert not tags(DOUBLE_BOTTOM, close=105.0, atr=atr)


def test_tolerances_scale_with_volatility():
    series = [lo(0, 100.0), hi(10, 110.0), lo(20, 102.5)]

    assert "double_bottom" not in tags(series, close=105.0, atr=0.5)
    assert "double_bottom" in tags(series, close=105.0, atr=4.0)


def test_detected_pivots_are_the_ones_the_match_used():
    found = detect(_window(26, 105.0), DOUBLE_BOTTOM, ATR)
    double = next(d for d in found if d.tag.setup_id == "double_bottom")

    assert [p.index for p in double.pivots] == [0, 10, 20]
