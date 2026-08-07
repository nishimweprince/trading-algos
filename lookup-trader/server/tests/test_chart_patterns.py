"""Chart patterns detected from the pivot sequence.

Tests come in pairs: a formation the geometry should find, and the near-miss it
must reject. Detection is only half of it — a formation is `forming` until price
leaves it, and that distinction decides whether the tag reaches a base rate.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.db.setups_seed import SEED_SETUPS
from app.taggers.chart.patterns import (
    MAX_BARS_SINCE_FORMATION,
    detect,
)
from app.taggers.chart.swings import Pivot
from app.taggers.pipeline import CHART_WINDOW, _chart_tags
from app.taggers.thresholds import SWING_LOOKBACK
from app.taggers.types import TagResult

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


# --------------------------------------------------------------------------
# The pipeline seam
#
# `detect` is exercised above on hand-built pivots and `pivots()` is exercised
# in test_swings on hand-built bars, but the join between them — the window
# slicing and the length guard in `_chart_tags` — was covered by neither.
# --------------------------------------------------------------------------


def _ohlc(turns: list[tuple[int, float]], length: int) -> pd.DataFrame:
    """Bars tracing a path through `turns`, wide enough for pivots to confirm."""
    xs = [i for i, _ in turns]
    ys = [p for _, p in turns]
    close = np.interp(np.arange(length), xs, ys)
    return pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 0.5, "close": close}
    )


def test_the_pipeline_finds_a_formation_in_real_bars():
    """A W traced in OHLC, with no pivots handed in."""
    bars = _ohlc([(0, 120.0), (20, 100.0), (40, 110.0), (60, 100.3), (72, 106.0)], 73)

    found = _chart_tags(bars, atr=1.0)

    assert "double_bottom" in {d.tag.setup_id for d in found}


def test_the_pipeline_reads_only_the_trailing_window():
    """A formation older than `CHART_WINDOW` is not present at this bar."""
    bars = _ohlc([(0, 120.0), (20, 100.0), (40, 110.0), (60, 100.3), (72, 106.0)], 73)
    padded = pd.concat([bars, _ohlc([(0, 106.0), (CHART_WINDOW, 106.0)], CHART_WINDOW)])

    assert not _chart_tags(padded.reset_index(drop=True), atr=1.0)


def test_a_window_too_short_to_confirm_a_pivot_admits_nothing():
    short = SWING_LOOKBACK * 2
    bars = _ohlc([(0, 120.0), (short // 2, 100.0), (short - 1, 110.0)], short)

    assert not _chart_tags(bars, atr=1.0)


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
    """A compact formation gets the floor: ten bars and it has stopped being news."""
    compact = [lo(0, 100.0), hi(5, 110.0), lo(10, 100.5)]

    assert tags(compact, close=105.0, anchor=10 + MAX_BARS_SINCE_FORMATION)
    assert not tags(compact, close=105.0, anchor=10 + MAX_BARS_SINCE_FORMATION + 1)


def test_a_bigger_structure_stays_current_for_longer():
    """The budget scales with the formation's own span.

    One flat allowance treated a sixty-bar head and shoulders like a twelve-bar
    double bottom, so the long patterns went stale while their neckline break
    was still unfolding — which is the half of their life that matters.
    """
    wide = [lo(0, 100.0), hi(30, 110.0), lo(60, 100.5)]
    budget = int((60 - 0) * 0.75)

    assert tags(wide, close=105.0, anchor=60 + budget)
    assert not tags(wide, close=105.0, anchor=60 + budget + 1)


HEAD_SHOULDERS = [hi(0, 110.0), lo(10, 100.0), hi(20, 120.0), lo(30, 101.0), hi(40, 110.5)]
# The right shoulder confirms at 40; the break carves a low fifteen bars later
# and the pullback into the neckline a high seven bars after that. Both are
# pivots in their own right, and both used to hide the formation that made them.
BROKE_DOWN = [*HEAD_SHOULDERS, lo(55, 95.0)]
PULLED_BACK = [*BROKE_DOWN, hi(62, 100.8)]


def _primary(series: list[Pivot], close: float, anchor: int) -> str | None:
    """What `TagResult` would report as this bar's reading — the thing operators see."""
    end = anchor
    found = detect(_window(end + 1, close), series, ATR)
    top = TagResult.of([d.tag for d in found], "test").primary()
    return top.setup_id if top else None


def test_a_formation_survives_its_own_completion():
    """The regression this search was restructured for.

    A reversal carves a fresh pivot the moment it breaks its neckline and
    another when it pulls back to it. Reading only the most recent pivots lost
    the pattern at exactly the point it became tradeable.
    """
    assert _primary(HEAD_SHOULDERS, close=100.5, anchor=45) == "head_shoulders"
    assert _primary(BROKE_DOWN, close=96.0, anchor=60) == "head_shoulders"
    assert _primary(PULLED_BACK, close=99.0, anchor=68) == "head_shoulders"


def test_completion_never_flips_the_reported_direction():
    """The worst available failure, and what the old tail-only search actually did.

    Once the break had carved its own pivots the head and shoulders was gone and
    the pullback read as a falling wedge — a bullish tag, on a completed bearish
    reversal. `side` feeds straight through to the API, so nothing downstream
    would have caught it.
    """
    for series, close, anchor in (
        (BROKE_DOWN, 96.0, 60),
        (PULLED_BACK, 99.0, 68),
    ):
        found = detect(_window(anchor + 1, close), series, ATR)
        strongest = max(found, key=lambda d: d.tag.confidence).tag

        assert strongest.side == -1, {d.tag.setup_id: d.tag.side for d in found}


def test_price_beyond_the_head_invalidates_rather_than_vanishing():
    got = tags(HEAD_SHOULDERS, close=122.0)["head_shoulders"]

    assert got.state == "invalidated"


def test_a_lopsided_head_and_shoulders_is_a_trend_leg():
    """Four bars up, fifty bars down. Two turns, not two shoulders."""
    assert "head_shoulders" not in tags(
        [hi(0, 110.0), lo(4, 100.0), hi(20, 120.0), lo(24, 101.0), hi(70, 110.5)],
        close=105.0,
    )


def test_a_formation_outranks_the_triangle_it_happens_to_contain():
    """Cross-pattern comparability, which is what the shared four axes buy.

    Every matcher used to pass a different number of ratios to `graded()`, and
    since `graded()` takes the weakest, the ones passing fewer scored higher for
    reasons unrelated to match quality. A lopsided head and shoulders lost to
    the symmetrical triangle sitting inside its own five pivots.
    """
    got = tags(HEAD_SHOULDERS, close=100.5)

    assert got["head_shoulders"].confidence > got["triangle_symmetrical"].confidence
    assert _primary(HEAD_SHOULDERS, close=100.5, anchor=45) == "head_shoulders"


def test_a_sloping_neckline_confirms_on_the_line_not_the_last_trough():
    """The neckline is a line. Reading it as the later trough's level confirms
    early on one slope and late on the other."""
    series = [hi(0, 112.0), lo(10, 104.0), hi(20, 120.0), lo(30, 102.2), hi(40, 112.4)]
    # The troughs fall away, so by the anchor the line sits below the later one.
    # A close between the two readings is the whole point: the trough says the
    # break happened, the line says price has not got there yet.
    assert tags(series, close=101.8)["head_shoulders"].state == "forming"
    assert tags(series, close=101.0)["head_shoulders"].state == "complete"


def test_confidence_spreads_instead_of_pinning_at_the_ceiling():
    """`_ratio(tolerance, gap)` hit the cap at half the tolerance, so every
    comfortably-formed pattern scored exactly 1.0 and the alphabetical
    `setup_id` tie-break decided which one `primary()` reported."""
    scores = [
        tags([lo(0, 100.0), hi(10, 115.0), lo(20, 100.0 + gap)], close=105.0)[
            "double_bottom"
        ].confidence
        for gap in (0.1, 0.3, 0.5, 0.7, 0.9)
    ]

    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == len(scores)
    assert max(scores) < 1.0


# --------------------------------------------------------------------------
# Triple top / bottom
# --------------------------------------------------------------------------

TRIPLE_TOP = [hi(0, 110.0), lo(10, 100.0), hi(20, 110.3), lo(30, 100.5), hi(40, 110.1)]


def test_three_touches_of_one_level_is_a_triple():
    assert "triple_top" in tags(TRIPLE_TOP, close=105.0)


def test_a_triple_completes_through_its_neckline():
    assert tags(TRIPLE_TOP, close=105.0)["triple_top"].state == "forming"
    assert tags(TRIPLE_TOP, close=99.0)["triple_top"].state == "complete"


def test_a_middle_touch_that_clears_the_others_is_a_head_not_a_third_touch():
    """The two readings are made mutually exclusive on purpose: the ceiling on a
    triple's middle touch is the floor on a head."""
    got = tags(HEAD_SHOULDERS, close=105.0)

    assert "head_shoulders" in got
    assert "triple_top" not in got


def test_the_inverse_triple_is_recognised_too():
    assert "triple_bottom" in tags(
        [lo(0, 100.0), hi(10, 110.0), lo(20, 99.7), hi(30, 109.5), lo(40, 99.9)],
        close=105.0,
    )


# --------------------------------------------------------------------------
# Rectangle and channel — the geometry that used to fall through unnamed
# --------------------------------------------------------------------------


def test_a_range_that_held_its_boundaries_is_a_rectangle():
    assert "rectangle" in tags(
        [hi(0, 110.0), lo(10, 100.0), hi(20, 110.05), lo(30, 100.05)], close=105.0
    )


def test_boundaries_travelling_together_are_a_channel():
    assert "channel_up" in tags(
        [lo(0, 100.0), hi(10, 110.0), lo(20, 106.0), hi(30, 116.0)], close=112.0
    )
    assert "channel_down" in tags(
        [hi(0, 116.0), lo(10, 106.0), hi(20, 110.0), lo(30, 100.0)], close=104.0
    )


def test_boundaries_that_diverge_are_not_a_channel():
    """Same rising lows, but the highs pull away — the range did not hold."""
    got = tags([lo(0, 100.0), hi(10, 110.0), lo(20, 106.0), hi(30, 120.0)], close=112.0)

    assert not {"channel_up", "channel_down", "rectangle"} & set(got)


# --------------------------------------------------------------------------
# Flags and pennants — the pole is the load-bearing half
# --------------------------------------------------------------------------

FLAG = [lo(50, 107.0), hi(60, 109.5), lo(70, 106.5), hi(80, 109.0)]


def _poled(close: float, rise: float = 7.0, length: int = 86) -> pd.DataFrame:
    """A frame whose closes run `rise` between bars 30 and 50 — a pole, then a pause."""
    closes = [
        100.0 if i <= 30 else 100.0 + rise * min((i - 30) / 20.0, 1.0) for i in range(length)
    ]
    closes[-1] = close
    return pd.DataFrame({"close": closes})


def _poled_tags(close: float, rise: float = 7.0) -> dict[str, object]:
    found = detect(_poled(close, rise), FLAG, ATR)
    return {d.tag.setup_id: d.tag for d in found}


def test_a_tight_pause_after_a_fast_move_is_a_flag():
    assert "flag_bullish" in _poled_tags(close=108.0)


def test_a_flag_completes_out_of_its_upper_boundary():
    assert _poled_tags(close=108.0)["flag_bullish"].state == "forming"
    assert _poled_tags(close=110.0)["flag_bullish"].state == "complete"


def test_the_same_shape_without_a_pole_is_only_a_channel():
    """Small ranges are everywhere; what makes this one a continuation is what
    came before it. Drop the pole and the continuation reading has to go."""
    got = _poled_tags(close=108.0, rise=0.5)

    assert "flag_bullish" not in got
    assert "channel_down" in got


def test_a_flag_never_ships_alongside_the_channel_it_looks_like():
    """A bull flag is a falling channel with a pole in front of it. Both readings
    on one bar would mean a +1 and a -1 tag on the same four pivots."""
    got = _poled_tags(close=108.0)

    assert "channel_down" not in got
    assert got["flag_bullish"].side == 1


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
