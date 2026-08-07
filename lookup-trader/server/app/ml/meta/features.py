"""The estimator input boundary for the meta-model.

`meta_events` already decides which columns are causal and side-canonicalised;
this module only splits that list by dtype so the preprocessor can handle each
group, and states in one place which columns may never cross the boundary.

The deny-list is the important half. `outcome/features.py::is_forbidden_feature`
catches `y_meta` through its label regex but returns False for `net_r_3` — that
column is excluded today only because it is absent from an allow-list, which is
a guarantee that disappears the moment someone builds a feature frame by
subtraction rather than by selection.
"""

from __future__ import annotations

import re

from app.services.meta_events import META_MODEL_FEATURES
from app.services.meta_events_v2 import META_MODEL_FEATURES_V2

# Categorical after side-canonicalisation. `trend_state` and `htf_trend_state`
# carry aligned/opposed here, not up/down — the raw direction is gone by design.
META_CATEGORICAL_FEATURES: tuple[str, ...] = (
    "trend_state",
    "atr_bucket",
    "session",
    "rsi_band",
    "day_of_week",
    "ema_slope_bucket",
    "atr_change_bucket",
    "htf_trend_state",
    "htf_atr_bucket",
)

SHAPE_FEATURE = "shape_48"
META_SHAPE_COLUMNS: tuple[str, ...] = tuple(f"shape_48__{index:02d}" for index in range(48))

# `session_overlap` is a bool and `bar_in_session` a small-range int; both are
# left numeric on purpose. Scaling a 0/1 flag is harmless, and the session
# counter is genuinely ordinal.
META_NUMERIC_FEATURES: tuple[str, ...] = tuple(
    name
    for name in META_MODEL_FEATURES
    if name not in META_CATEGORICAL_FEATURES and name != SHAPE_FEATURE
)

META_INPUT_FEATURES: tuple[str, ...] = tuple(META_MODEL_FEATURES)
META_INPUT_FEATURES_V2: tuple[str, ...] = tuple(META_MODEL_FEATURES_V2)

# Anything describing what happened after the signal. Kept as an explicit
# pattern rather than a name list so a future `net_r_12` or `exit_reason` is
# refused by default instead of silently admitted.
_OUTCOME_PATTERN = re.compile(
    r"^(y_meta|net_r|gross_r|cost_r|outcome|exit_|entry_price|stop_price|target_price"
    r"|bars_to_resolution|ambiguous_bar|fwd\d+)",
    re.IGNORECASE,
)


def is_outcome_column(name: str) -> bool:
    """Whether a column describes the future and must stay out of the estimator."""
    return bool(_OUTCOME_PATTERN.match(name))


def assert_causal(names: tuple[str, ...] | list[str]) -> None:
    """Raise if any proposed feature describes the outcome it is meant to predict."""
    leaked = sorted(name for name in names if is_outcome_column(name))
    if leaked:
        raise ValueError(f"Outcome columns cannot be estimator inputs: {leaked}")
    unknown = sorted(set(names) - set(META_INPUT_FEATURES_V2))
    if unknown:
        raise ValueError(f"Not declared in META_MODEL_FEATURES: {unknown}")
