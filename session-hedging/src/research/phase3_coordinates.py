"""Frozen §8.0 Phase 3 exploratory coordinates. 104 one-topic cells; no lane crossing."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from models import EngineParams, EntryMode

PHASE3_COORDINATE_COUNT = 104
ENTRY_MODES: tuple[str, ...] = tuple(mode.value for mode in EntryMode)

SHARED_BASE: dict[str, Any] = {
    "sessions": ["tokyo", "london", "new_york"],
    "timeframe": "M15",
    "orb_minutes": 60,
    "entry_delay_minutes": 15,
    "time_exit_mode": "max_age",
    "max_age_hours": 24.0,
    "stop_mode": "bar_range",
    "sl_mult": 2.0,
    "rr": 3.0,
    "tp_mode": "fixed_r",
    "partial_tp_r": 1.0,
    "partial_fraction": 0.5,
    "lock_mode": "absolute",
    "lock_pips": 20.0,
    "lock_r": 0.0,
    "min_stop_pips": 0.0,
    "min_stop_cost_mult": 0.0,
    "qty": 1.0,
    "qty_ref": 1.0,
    "max_concurrent_structures": 3,
    "one_open_per_session": True,
    "firm_profile": "none",
    "filter_d1_ema50": False,
    "filter_nr7": False,
    "filter_orb_atr_min": 0.0,
    "filter_orb_atr_max": 0.0,
    "cost_model": "per_session",
    "spread_pips_per_side": 2.0,
    "slippage_pips_per_side": 0.5,
    "commission_pips_per_side": 0.0,
    "swap_long_pips_per_rollover": 0.0,
    "swap_short_pips_per_rollover": 0.0,
}

STRESS_COST = {
    "spread_pips_per_side": 4.0,
    "slippage_pips_per_side": 1.0,
}


def canonical_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _coordinate(coord_id: str, lane: str, overrides: dict[str, Any]) -> dict[str, Any]:
    params = deepcopy(SHARED_BASE)
    params.update(overrides)
    return {"id": coord_id, "lane": lane, "params": params}


def _build() -> list[dict[str, Any]]:
    coordinates: list[dict[str, Any]] = []
    for mode in ENTRY_MODES:
        coordinates.append(_coordinate(f"incumbent:{mode}", "incumbent", {"entry_mode": mode}))
        for multiple in (2.0, 3.0):
            coordinates.append(
                _coordinate(
                    f"cost_floor:mult{multiple:g}:{mode}",
                    "cost_floor",
                    {"entry_mode": mode, "min_stop_cost_mult": multiple},
                )
            )
        for stop_mode in ("atr14", "orb_atr14_blend"):
            for sl_mult in (1.5, 2.0, 2.5):
                coordinates.append(
                    _coordinate(
                        f"smoothed_stop:{stop_mode}:sl{sl_mult:g}:{mode}",
                        "smoothed_stop",
                        {"entry_mode": mode, "stop_mode": stop_mode, "sl_mult": sl_mult},
                    )
                )
        for rr in (2.0, 3.0, 4.0):
            for age in (8.0, 24.0, 48.0):
                if rr == 3.0 and age == 24.0:
                    continue
                coordinates.append(
                    _coordinate(
                        f"horizon:rr{rr:g}:age{age:g}:{mode}",
                        "horizon",
                        {"entry_mode": mode, "rr": rr, "max_age_hours": age},
                    )
                )
        coordinates.append(
            _coordinate(
                f"partial_trail:{mode}",
                "partial_trail",
                {"entry_mode": mode, "tp_mode": "partial_trail"},
            )
        )
        coordinates.append(
            _coordinate(f"lock:none:{mode}", "lock", {"entry_mode": mode, "lock_mode": "none"})
        )
        coordinates.append(
            _coordinate(
                f"lock:breakeven:{mode}",
                "lock",
                {"entry_mode": mode, "lock_mode": "breakeven"},
            )
        )
        for lock_r in (0.1, 0.2):
            coordinates.append(
                _coordinate(
                    f"lock:r_relative:{lock_r:g}:{mode}",
                    "lock",
                    {"entry_mode": mode, "lock_mode": "r_relative", "lock_r": lock_r},
                )
            )
        coordinates.append(
            _coordinate(
                f"filter:d1_ema50:{mode}",
                "filter",
                {"entry_mode": mode, "filter_d1_ema50": True},
            )
        )
        coordinates.append(
            _coordinate(f"filter:nr7:{mode}", "filter", {"entry_mode": mode, "filter_nr7": True})
        )
        coordinates.append(
            _coordinate(
                f"filter:orb_atr_min:{mode}",
                "filter",
                {"entry_mode": mode, "filter_orb_atr_min": 0.5},
            )
        )
        coordinates.append(
            _coordinate(
                f"filter:orb_atr_max:{mode}",
                "filter",
                {"entry_mode": mode, "filter_orb_atr_max": 2.0},
            )
        )
    return coordinates


def validate_phase3_coordinates(coordinates: list[dict[str, Any]]) -> None:
    if len(coordinates) != PHASE3_COORDINATE_COUNT:
        raise ValueError(
            f"Phase 3 exploratory registry must contain exactly {PHASE3_COORDINATE_COUNT} "
            f"coordinates, not {len(coordinates)}"
        )
    ids = [str(item["id"]) for item in coordinates]
    if len(set(ids)) != len(ids):
        raise ValueError("Phase 3 exploratory registry has duplicate coordinate IDs")
    fingerprints = [canonical_dumps(item["params"]) for item in coordinates]
    if len(set(fingerprints)) != len(fingerprints):
        raise ValueError("Phase 3 exploratory registry has duplicate semantic coordinates")


def phase3_coordinate_sha256(coordinates: list[dict[str, Any]]) -> str:
    blob = "\n".join(
        canonical_dumps(item) for item in sorted(coordinates, key=lambda item: str(item["id"]))
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def apply_phase3_coordinate(base: EngineParams, coordinate: dict[str, Any]) -> EngineParams:
    params = coordinate["params"]
    updates = {
        key: params[key]
        for key in (
            "entry_mode",
            "orb_minutes",
            "entry_delay_minutes",
            "time_exit_mode",
            "max_age_hours",
            "stop_mode",
            "sl_mult",
            "rr",
            "tp_mode",
            "partial_tp_r",
            "partial_fraction",
            "lock_mode",
            "lock_pips",
            "lock_r",
            "min_stop_pips",
            "min_stop_cost_mult",
            "qty",
            "qty_ref",
            "max_concurrent_structures",
            "one_open_per_session",
            "firm_profile",
            "filter_d1_ema50",
            "filter_nr7",
            "filter_orb_atr_min",
            "filter_orb_atr_max",
            "cost_model",
            "spread_pips_per_side",
            "slippage_pips_per_side",
            "commission_pips_per_side",
            "swap_long_pips_per_rollover",
            "swap_short_pips_per_rollover",
        )
    }
    return EngineParams.model_validate(base.model_dump() | updates)


PHASE3_COORDINATES = _build()
validate_phase3_coordinates(PHASE3_COORDINATES)
PHASE3_COORDINATE_SHA256 = phase3_coordinate_sha256(PHASE3_COORDINATES)
