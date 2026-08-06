from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

from app.ml.outcome.artifact import load_artifact, save_artifact
from app.ml.outcome.metrics import multiclass_log_loss
from app.ml.outcome.model import CalibratedOutcomeModel, fit_temperature
from app.ml.outcome.preprocessing import (
    CATEGORICAL_FEATURES,
    INPUT_FEATURES,
    build_preprocessor,
)


def _features(rows: int = 60) -> pd.DataFrame:
    data = {}
    for name in INPUT_FEATURES:
        if name == "shape_48":
            data[name] = [
                [float((row + offset) % 7) for offset in range(48)] for row in range(rows)
            ]
        elif name in CATEGORICAL_FEATURES:
            data[name] = [str((row // 2) % 3) for row in range(rows)]
        else:
            data[name] = [float(row % 11) for row in range(rows)]
    data["side"] = [-1 if row % 2 else 1 for row in range(rows)]
    data["session"][3] = None
    data["atr_at_signal"][5] = np.nan
    data["shape_48"][7][2] = np.nan
    return pd.DataFrame(data)


def _model() -> tuple[CalibratedOutcomeModel, pd.DataFrame]:
    features = _features()
    target = pd.Series(["win", "loss", "timeout"] * 20)
    preprocessor = build_preprocessor()
    transformed = preprocessor.fit_transform(features)
    estimator = LogisticRegression(max_iter=500, random_state=0).fit(transformed, target)
    return CalibratedOutcomeModel(preprocessor, estimator, 1.0), features


def test_preprocessing_is_reproducible_and_rejects_schema_changes():
    features = _features()
    first = build_preprocessor().fit_transform(features)
    second = build_preprocessor().fit_transform(features.copy())
    np.testing.assert_array_equal(first, second)

    with pytest.raises(ValueError, match="schema mismatch"):
        build_preprocessor().fit(features.drop(columns=["session"]))
    with pytest.raises(ValueError, match="schema mismatch"):
        build_preprocessor().fit(features.assign(fwd24_max_atr=1.0))


def test_artifact_load_checks_version_schema_and_refuses_overwrite(tmp_path):
    model, features = _model()
    root = tmp_path / "models"
    path = save_artifact(
        root,
        "pilot-v1",
        model=model,
        metadata={"schema_sha256": "abc"},
        metrics={"holdout": {}},
        dataset_manifest={"manifest_version": 1},
    )
    loaded, metadata = load_artifact(
        path, expected_version="pilot-v1", expected_schema_hash="abc"
    )
    np.testing.assert_allclose(loaded.predict_proba(features), model.predict_proba(features))
    assert metadata["artifact_version"] == "pilot-v1"

    with pytest.raises(ValueError, match="schema mismatch"):
        load_artifact(path, expected_schema_hash="different")
    with pytest.raises(ValueError, match="version mismatch"):
        load_artifact(path, expected_version="pilot-v2")
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        save_artifact(
            root,
            "pilot-v1",
            model=model,
            metadata={"schema_sha256": "abc"},
            metrics={},
            dataset_manifest={},
        )


def test_calibrated_probabilities_are_normalized_and_reproducible():
    raw = np.array(
        [
            [0.80, 0.10, 0.10],
            [0.15, 0.70, 0.15],
            [0.10, 0.15, 0.75],
            [0.55, 0.25, 0.20],
        ]
    )
    target = pd.Series(["win", "loss", "timeout", "loss"])
    first = fit_temperature(raw, target)
    second = fit_temperature(raw, target.copy())
    assert first == second

    model, features = _model()
    probabilities = model.predict_proba(features)
    np.testing.assert_allclose(probabilities.sum(axis=1), 1.0, atol=1e-12)
    assert np.all((probabilities >= 0) & (probabilities <= 1))


def test_log_loss_respects_persisted_class_order():
    target = pd.Series(["win", "loss", "timeout"])
    assert multiclass_log_loss(target, np.eye(3)) == pytest.approx(0.0)
