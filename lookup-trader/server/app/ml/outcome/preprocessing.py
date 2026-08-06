"""Deterministic preprocessing for outcome-model features."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from app.ml.outcome.features import MODEL_FEATURES

CATEGORICAL_FEATURES = (
    "side",
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
SHAPE_COLUMNS = tuple(f"shape_48__{index:02d}" for index in range(48))
INPUT_FEATURES = ("side", *MODEL_FEATURES)
NUMERIC_FEATURES = tuple(
    name for name in INPUT_FEATURES if name not in CATEGORICAL_FEATURES and name != "shape_48"
)


def _shape_values(value: Any) -> list[float]:
    if isinstance(value, str):
        value = json.loads(value)
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return [np.nan] * 48
    values = np.asarray(value, dtype=float).reshape(-1)
    if len(values) != 48:
        raise ValueError(f"shape_48 must contain exactly 48 values, found {len(values)}")
    return values.tolist()


class FeatureFrameTransformer(BaseEstimator, TransformerMixin):
    """Validate the feature contract and expand the fixed-width shape vector."""

    def fit(self, X: pd.DataFrame, y: object = None) -> FeatureFrameTransformer:
        self._validate(X)
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        self._validate(X)
        frame = X.loc[:, INPUT_FEATURES].copy()
        shapes = pd.DataFrame(
            [_shape_values(value) for value in frame.pop("shape_48")],
            columns=SHAPE_COLUMNS,
            index=frame.index,
        )
        for name in CATEGORICAL_FEATURES:
            frame[name] = (
                frame[name].astype("string").fillna("__missing__").astype(object)
            )
        for name in NUMERIC_FEATURES:
            frame[name] = pd.to_numeric(frame[name], errors="coerce")
        return pd.concat([frame, shapes], axis=1)

    @staticmethod
    def _validate(X: pd.DataFrame) -> None:
        if not isinstance(X, pd.DataFrame):
            raise TypeError("Outcome preprocessing requires a pandas DataFrame")
        missing = sorted(set(INPUT_FEATURES) - set(X.columns))
        unexpected = sorted(set(X.columns) - set(INPUT_FEATURES))
        if missing or unexpected:
            raise ValueError(
                f"Outcome feature schema mismatch; missing={missing}, unexpected={unexpected}"
            )


def build_preprocessor() -> Pipeline:
    """Return a dense, deterministic transformer shared by both estimators."""
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
    columns = ColumnTransformer(
        [
            ("categorical", categorical, list(CATEGORICAL_FEATURES)),
            ("numeric", numeric, [*NUMERIC_FEATURES, *SHAPE_COLUMNS]),
        ],
        remainder="drop",
        sparse_threshold=0.0,
    )
    return Pipeline([("features", FeatureFrameTransformer()), ("columns", columns)])
