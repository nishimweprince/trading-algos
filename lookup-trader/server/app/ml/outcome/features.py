"""Typed, versioned feature contract for outcome-model datasets.

Only names in ``CAUSAL_FEATURES`` may cross the model boundary.  The categorical
deny-list is checked first so a future store column cannot accidentally be
allowed merely by adding it to a broad prefix or inferred schema.
"""

from __future__ import annotations

import re
from typing import Final, Literal, cast

from app.db.setups_seed import SEED_SETUPS

OUTCOME_FEATURE_VERSION: Final = "1"

FeatureName = Literal[
    "trend_state",
    "atr_bucket",
    "session",
    "rsi_band",
    "day_of_week",
    "atr_at_signal",
    "ema_slope_bucket",
    "atr_change_bucket",
    "htf_trend_state",
    "dist_day_high_atr",
    "dist_day_low_atr",
    "gap_atr",
    "signal_body_pct",
    "signal_upper_wick_pct",
    "signal_lower_wick_pct",
    "signal_range_atr",
    "bars_since_swing_high",
    "bars_since_swing_low",
    "ema_value",
    "rsi_value",
    "atr_pct",
    "dist_ema_atr",
    "ema_slope_atr",
    "atr_change_ratio",
    "warmup_bars_available",
    "context_reliable",
    "close",
    "atr_at_bar",
    "back_bars_available",
    "efficiency_ratio",
    "close_range_pct",
    "realized_vol_atr",
    "round_number_dist_atr",
    "volume_z",
    "htf_atr_bucket",
    "htf_range_pct",
    "dist_swing_high_atr",
    "dist_swing_low_atr",
    "prior_day_high_dist_atr",
    "prior_day_low_dist_atr",
    "prior_week_high_dist_atr",
    "prior_week_low_dist_atr",
    "bar_in_session",
    "session_overlap",
    "shape_48",
]

CAUSAL_FEATURES: Final[tuple[FeatureName, ...]] = (
    "trend_state",
    "atr_bucket",
    "session",
    "rsi_band",
    "day_of_week",
    "atr_at_signal",
    "ema_slope_bucket",
    "atr_change_bucket",
    "htf_trend_state",
    "dist_day_high_atr",
    "dist_day_low_atr",
    "gap_atr",
    "signal_body_pct",
    "signal_upper_wick_pct",
    "signal_lower_wick_pct",
    "signal_range_atr",
    "bars_since_swing_high",
    "bars_since_swing_low",
    "ema_value",
    "rsi_value",
    "atr_pct",
    "dist_ema_atr",
    "ema_slope_atr",
    "atr_change_ratio",
    "warmup_bars_available",
    "context_reliable",
    "close",
    "atr_at_bar",
    "back_bars_available",
    "efficiency_ratio",
    "close_range_pct",
    "realized_vol_atr",
    "round_number_dist_atr",
    "volume_z",
    "htf_atr_bucket",
    "htf_range_pct",
    "dist_swing_high_atr",
    "dist_swing_low_atr",
    "prior_day_high_dist_atr",
    "prior_day_low_dist_atr",
    "prior_week_high_dist_atr",
    "prior_week_low_dist_atr",
    "bar_in_session",
    "session_overlap",
    "shape_48",
)

SETUP_IDS: Final[tuple[str, ...]] = tuple(sorted(row[0] for row in SEED_SETUPS))
TAG_FEATURES: Final[tuple[str, ...]] = tuple(
    name
    for setup_id in SETUP_IDS
    for name in (
        f"tag_complete__{setup_id}",
        f"tag_forming__{setup_id}",
        f"tag_match_quality__{setup_id}",
    )
)
MODEL_FEATURES: Final[tuple[str, ...]] = (*CAUSAL_FEATURES, *TAG_FEATURES)

_EXACT_FORBIDDEN: Final = frozenset({"level_touch", "fwd_shape", "next_open"})
_LABEL_FIELD = re.compile(r"(^|_)(?:y|target)(?:_|$)", re.IGNORECASE)


def is_forbidden_feature(name: str) -> bool:
    """Return whether a field is categorically outcome-derived or label-like."""
    lowered = name.lower()
    return (
        lowered.startswith("fwd")
        or lowered in _EXACT_FORBIDDEN
        or bool(_LABEL_FIELD.search(lowered))
    )


def validate_causal_features(names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate a requested model schema and return it unchanged.

    Unknown fields are rejected too: inference from whatever happens to be in a
    Parquet file is precisely the failure mode this contract is intended to stop.
    """
    forbidden = sorted({name for name in names if is_forbidden_feature(name)})
    if forbidden:
        raise ValueError(f"Outcome-derived/label fields are forbidden: {forbidden}")
    unknown = sorted(set(names) - set(MODEL_FEATURES))
    if unknown:
        raise ValueError(f"Fields are not in the causal allow-list: {unknown}")
    return tuple(names)


def causal_store_features() -> tuple[FeatureName, ...]:
    """The causal fields read directly from the bar feature store."""
    return cast(tuple[FeatureName, ...], validate_causal_features(list(CAUSAL_FEATURES)))
