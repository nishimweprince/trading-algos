"""Frozen 104-coordinate Phase 3 exploratory registry."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy

import pytest

from models import EngineParams
from research.phase3_coordinates import (
    PHASE3_COORDINATE_COUNT,
    PHASE3_COORDINATE_SHA256,
    PHASE3_COORDINATES,
    SHARED_BASE,
    apply_phase3_coordinate,
    canonical_dumps,
    phase3_coordinate_sha256,
    validate_phase3_coordinates,
)


def test_registry_has_exactly_104_distinct_coordinates() -> None:
    validate_phase3_coordinates(PHASE3_COORDINATES)
    assert len(PHASE3_COORDINATES) == PHASE3_COORDINATE_COUNT == 104
    assert len({item["id"] for item in PHASE3_COORDINATES}) == 104
    assert len({canonical_dumps(item["params"]) for item in PHASE3_COORDINATES}) == 104


def test_lane_counts_match_the_frozen_family() -> None:
    counts = Counter(item["lane"] for item in PHASE3_COORDINATES)
    assert counts == {
        "incumbent": 4,
        "cost_floor": 8,
        "smoothed_stop": 24,
        "horizon": 32,
        "partial_trail": 4,
        "lock": 16,
        "filter": 16,
    }


def test_horizon_lane_excludes_the_duplicate_base_rr3_age24() -> None:
    horizons = [item for item in PHASE3_COORDINATES if item["lane"] == "horizon"]
    assert all(
        not (item["params"]["rr"] == 3.0 and item["params"]["max_age_hours"] == 24.0)
        for item in horizons
    )
    incumbents = [item for item in PHASE3_COORDINATES if item["lane"] == "incumbent"]
    assert all(
        item["params"]["rr"] == 3.0 and item["params"]["max_age_hours"] == 24.0
        for item in incumbents
    )


def test_incumbent_hedge_pair_is_the_shared_base() -> None:
    hedge = next(item for item in PHASE3_COORDINATES if item["id"] == "incumbent:hedge_pair")
    expected = dict(SHARED_BASE)
    expected["entry_mode"] = "hedge_pair"
    assert hedge["params"] == expected


def test_canonical_hash_is_stable() -> None:
    assert PHASE3_COORDINATE_SHA256 == phase3_coordinate_sha256(PHASE3_COORDINATES)
    assert PHASE3_COORDINATE_SHA256 == (
        "eb2c04f5edd92e86a8e87ec7d903c6f342655e94949dc4d4ee9298097bf47146"
    )


def test_duplicate_and_extra_coordinates_are_rejected() -> None:
    dup = deepcopy(PHASE3_COORDINATES)
    dup[1]["id"] = "extra:duplicate-params"
    dup[1]["params"] = deepcopy(dup[0]["params"])
    with pytest.raises(ValueError, match="duplicate semantic"):
        validate_phase3_coordinates(dup)
    extra = deepcopy(PHASE3_COORDINATES)
    extra.append(
        {
            "id": "extra:105",
            "lane": "incumbent",
            "params": dict(SHARED_BASE) | {"entry_mode": "hedge_pair", "rr": 9.0},
        }
    )
    with pytest.raises(ValueError, match="exactly 104"):
        validate_phase3_coordinates(extra)


def test_apply_coordinate_reaches_engine_params() -> None:
    coordinate = next(
        item for item in PHASE3_COORDINATES if item["id"] == "lock:r_relative:0.2:oco_bracket"
    )
    params = apply_phase3_coordinate(EngineParams(), coordinate)
    assert params.entry_mode == "oco_bracket"
    assert params.lock_mode == "r_relative"
    assert params.lock_r == pytest.approx(0.2)
    assert params.spread_pips_per_side == pytest.approx(2.0)
    assert params.slippage_pips_per_side == pytest.approx(0.5)
    trail = next(item for item in PHASE3_COORDINATES if item["id"] == "partial_trail:hedge_pair")
    trail_params = apply_phase3_coordinate(EngineParams(), trail)
    assert trail_params.tp_mode == "partial_trail"
    assert trail_params.partial_fraction == pytest.approx(0.5)
    leaked = apply_phase3_coordinate(
        EngineParams(session_cost_overrides={"tokyo": {"spread_pips_per_side": 9.0}}),
        coordinate,
    )
    assert leaked.session_cost_overrides == {}
