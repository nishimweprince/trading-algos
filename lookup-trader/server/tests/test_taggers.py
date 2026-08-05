"""Rule taggers against hand-built OHLC windows.

The corpus lives in `fixtures/tagging/candlestick.json` rather than inline so the
same cases can later score an algorithmic or LLM tagger against the rules.

Every rule gets near-miss negatives as well as a positive. A tagger that fires
too eagerly is worse than one that fires too little: the tags become a base-rate
population, and a population of near-misses is a population of nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.config import settings
from app.taggers import TAG_LOOKBACK, TagResult, tag_bar
from app.taggers.confidence import CONFIDENCE_FLOOR, RATIO_CAP, graded
from app.taggers.rules import RULES
from app.taggers.thresholds import MIN_RANGE_ATR

FIXTURES = json.loads((Path(__file__).parent / "fixtures/tagging/candlestick.json").read_text())


def _window(bars: list[list[float]]) -> pd.DataFrame:
    """An OHLCV frame shaped like the slice `context_half` receives."""
    frame = pd.DataFrame(bars, columns=["open", "high", "low", "close"])
    frame["ts"] = pd.date_range("2026-01-01", periods=len(frame), freq="h", tz="UTC")
    frame["volume"] = 1000.0
    return frame[["ts", "open", "high", "low", "close", "volume"]]


@pytest.mark.parametrize("case", FIXTURES, ids=[c["name"] for c in FIXTURES])
def test_golden_cases(case: dict) -> None:
    result = tag_bar(_window(case["bars"]), case["atr"])
    got = sorted((t.setup_id, t.side) for t in result.tags)
    want = sorted((e["setup_id"], e["side"]) for e in case["expect"])
    assert got == want


def test_every_tag_is_complete_and_rule_sourced_in_phase_1() -> None:
    for case in FIXTURES:
        for tag in tag_bar(_window(case["bars"]), case["atr"]).tags:
            assert tag.state == "complete"
            assert tag.source == "rule"
            assert tag.model_version is None
            assert CONFIDENCE_FLOOR <= tag.confidence <= 1.0


def test_ambiguous_bar_keeps_both_tags_and_a_deterministic_primary() -> None:
    case = next(c for c in FIXTURES if c["name"].startswith("ambiguous"))
    result = tag_bar(_window(case["bars"]), case["atr"])

    assert len(result.tags) == 2
    # Opposite sides on one bar is not a bug — it is a bar that genuinely reads
    # both ways, and the primary has to be the same one on every run.
    assert {t.side for t in result.tags} == {1, -1}
    primary = result.primary()
    assert primary is not None
    assert primary.confidence == max(t.confidence for t in result.tags)
    assert result.primary_setup_id() == primary.setup_id


def test_no_atr_means_no_tags() -> None:
    # Every size threshold is ATR-normalised, so without one the alternative to
    # returning nothing is tagging on an arbitrary scale.
    case = FIXTURES[0]
    for atr in (None, 0.0, -1.0):
        assert tag_bar(_window(case["bars"]), atr).tags == ()


def test_a_single_bar_window_cannot_be_tagged() -> None:
    assert tag_bar(_window([[100.0, 101.0, 99.0, 100.5]]), 1.0).tags == ()


def test_result_carries_the_feature_version() -> None:
    result = tag_bar(_window(FIXTURES[0]["bars"]), 1.0)
    assert result.version == settings.bar_feature_version
    assert result.to_json()["version"] == settings.bar_feature_version


def test_tagging_is_deterministic() -> None:
    window = _window(FIXTURES[0]["bars"])
    first = json.dumps(tag_bar(window, 1.0).to_json(), sort_keys=False)
    second = json.dumps(tag_bar(window, 1.0).to_json(), sort_keys=False)
    assert first == second


def test_json_round_trip_is_lossless() -> None:
    # Pins the rounding in `graded`: an unrounded float would survive a Python
    # round trip but not necessarily match a rebuild byte-for-byte.
    for case in FIXTURES:
        result = tag_bar(_window(case["bars"]), case["atr"])
        payload = json.dumps(result.to_json(), separators=(",", ":"))
        assert TagResult.from_json(payload).to_json() == result.to_json()


def test_setup_ids_lead_with_the_primary() -> None:
    """`tag_setup_ids` and `tag_primary_setup_id` are written from one ordering.

    Phase 2 filters on the CSV column and the UI reads the primary; if the two
    ever disagree, a bar would be filtered into a population its own label
    contradicts.
    """
    for case in FIXTURES:
        result = tag_bar(_window(case["bars"]), case["atr"])
        if result.primary() is None:
            continue
        assert result.setup_ids()[0] == result.primary_setup_id()


def test_a_bar_that_exactly_meets_every_threshold_scores_the_floor() -> None:
    assert graded(1.0) == CONFIDENCE_FLOOR
    assert graded(1.0, 5.0, 5.0) == CONFIDENCE_FLOOR


def test_confidence_is_monotone_in_the_weakest_dimension() -> None:
    scores = [graded(r, RATIO_CAP) for r in (1.0, 1.25, 1.5, 1.75, 2.0)]
    assert scores == sorted(scores)
    assert scores[-1] == 1.0


def test_no_single_dimension_can_carry_the_score() -> None:
    # A doji drives a headroom ratio towards infinity; the cap is what stops that
    # from reading as a perfect match.
    assert graded(1.0, 1e9) == CONFIDENCE_FLOOR


def test_range_gate_rejects_every_rule() -> None:
    """No rule may tag a bar whose range is noise relative to volatility."""
    for case in FIXTURES:
        bars = case["bars"]
        anchor_range = bars[-1][1] - bars[-1][2]
        # An ATR large enough that the anchor range cannot clear the gate.
        atr = anchor_range / (MIN_RANGE_ATR / 2)
        assert tag_bar(_window(bars), atr).tags == ()


def test_rules_declare_a_stable_order() -> None:
    assert [r.__name__ for r in RULES] == [
        "bull_engulfing",
        "bear_engulfing",
        "pin_bar_long",
        "pin_bar_short",
        "inside_break",
    ]


def test_lookback_covers_the_deepest_rule() -> None:
    # inside_break reads mother, inside and break. If a rule ever needs more, the
    # window slice has to grow with it or that rule silently never fires.
    assert TAG_LOOKBACK == 3
