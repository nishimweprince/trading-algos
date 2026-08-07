"""Baselines the meta-model has to beat, and the shared preprocessor.

`ContextFrequencyBaseline` in the outcome package groups on
`side × session × trend_state × atr_bucket` over every bar. That population is
wrong here twice over: these events are sparse rather than per-bar, and `side`
carries no information once `_canonical_features` has reflected everything
relative to it. The smoothing arithmetic is worth keeping, so it is reproduced
against the grouping the roadmap asks for.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from app.ml.meta.features import (
    META_CATEGORICAL_FEATURES,
    META_INPUT_FEATURES,
    META_SHAPE_COLUMNS,
    SHAPE_FEATURE,
    assert_causal,
)
from app.ml.outcome.preprocessing import _shape_values

# Grouping for the event-frequency baseline, per the roadmap. `side` is included
# even though features are canonicalised, because the base strategy's own long
# and short populations differ in size and composition.
EVENT_CONTEXT = ("primary_setup_id", "side", "session", "trend_state", "atr_bucket")
SMOOTHING = 10.0


def expand_features(
    frame: pd.DataFrame, feature_columns: tuple[str, ...] = META_INPUT_FEATURES
) -> pd.DataFrame:
    """Select the declared inputs and unpack the 48-bar shape vector."""
    assert_causal(feature_columns)
    missing = sorted(set(feature_columns) - set(frame.columns))
    if missing:
        raise ValueError(f"Meta feature schema mismatch; missing={missing}")

    out = frame.loc[:, list(feature_columns)].copy()
    shapes = pd.DataFrame(
        [_shape_values(value) for value in out.pop(SHAPE_FEATURE)],
        columns=list(META_SHAPE_COLUMNS),
        index=out.index,
    )
    for name in META_CATEGORICAL_FEATURES:
        out[name] = out[name].astype("string").fillna("__missing__").astype(object)
    numeric_features = tuple(
        name
        for name in feature_columns
        if name not in META_CATEGORICAL_FEATURES and name != SHAPE_FEATURE
    )
    for name in numeric_features:
        out[name] = pd.to_numeric(out[name], errors="coerce")
    return pd.concat([out, shapes], axis=1)


def build_preprocessor(
    feature_columns: tuple[str, ...] = META_INPUT_FEATURES,
) -> ColumnTransformer:
    """Same shape as the outcome preprocessor, over the 39 meta features.

    Dense and deterministic: constant-impute then one-hot for categoricals,
    median-impute then standardise for numerics, unknown categories ignored so
    an unseen session in a later fold cannot fail at transform time.
    """
    categorical = Pipeline(
        [
            ("impute", SimpleImputer(strategy="constant", fill_value="__missing__")),
            (
                "encode",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False, dtype=np.float64),
            ),
        ]
    )
    numeric = Pipeline(
        [
            ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("scale", StandardScaler()),
        ]
    )
    numeric_features = tuple(
        name
        for name in feature_columns
        if name not in META_CATEGORICAL_FEATURES and name != SHAPE_FEATURE
    )
    return ColumnTransformer(
        [
            ("categorical", categorical, list(META_CATEGORICAL_FEATURES)),
            ("numeric", numeric, [*numeric_features, *META_SHAPE_COLUMNS]),
        ],
        remainder="drop",
        sparse_threshold=0.0,
    )


class TakeAll:
    """Enter every event the base strategy produced. The floor, not a model."""

    name = "take_all"

    def fit(self, frame: pd.DataFrame, y: pd.Series) -> TakeAll:
        self.rate_ = float(np.mean(y))
        return self

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        return np.full(len(frame), self.rate_, dtype=float)


class EventFrequency:
    """Smoothed historical hit rate per `setup × side × session × trend × ATR`.

    Cells shrink toward the global rate in proportion to how little evidence
    they carry, so a setup seen four times does not claim a 75% win rate.
    """

    name = "event_frequency"

    def __init__(self, smoothing: float = SMOOTHING) -> None:
        self.smoothing = smoothing

    def fit(self, frame: pd.DataFrame, y: pd.Series) -> EventFrequency:
        data = frame.loc[:, list(EVENT_CONTEXT)].copy().fillna("__missing__")
        data["_y"] = np.asarray(y, dtype=float)
        self.global_ = float(data["_y"].mean())
        self.cells_: dict[tuple, float] = {}
        for key, group in data.groupby(list(EVENT_CONTEXT), dropna=False, sort=True):
            hits, n = float(group["_y"].sum()), float(len(group))
            self.cells_[tuple(str(v) for v in key)] = (hits + self.smoothing * self.global_) / (
                n + self.smoothing
            )
        return self

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        context = frame.loc[:, list(EVENT_CONTEXT)].fillna("__missing__")
        return np.array(
            [
                self.cells_.get(tuple(str(v) for v in row), self.global_)
                for row in context.itertuples(index=False, name=None)
            ],
            dtype=float,
        )
