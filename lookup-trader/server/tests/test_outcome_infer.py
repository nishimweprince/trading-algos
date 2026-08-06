from __future__ import annotations

import inspect
import json

import duckdb
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.ml.outcome.artifact import save_artifact
from app.ml.outcome.features import OUTCOME_FEATURE_VERSION
from app.ml.outcome.infer import (
    OutcomeArtifactIncompatible,
    OutcomeArtifactUnavailable,
    build_input_features,
    infer_outcomes,
)
from app.ml.outcome.model import CLASS_ORDER
from app.ml.outcome.preprocessing import INPUT_FEATURES
from app.routers import outcome
from app.services.bar_features import context_half, tags_half
from app.services.export_bar_features import _encode_tags


class FixedFixtureModel:
    def predict_proba(self, frame):
        assert list(frame.columns) == list(INPUT_FEATURES)
        return np.array(
            [[0.61, 0.24, 0.15] if side == 1 else [0.29, 0.51, 0.20] for side in frame["side"]]
        )


def _window(rows: int = 240) -> pd.DataFrame:
    index = np.arange(rows, dtype=float)
    close = 1900 + index * 0.2 + np.sin(index / 7)
    return pd.DataFrame(
        {
            "ts": pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC"),
            "open": close - 0.1,
            "high": close + 0.8,
            "low": close - 0.7,
            "close": close,
            "volume": 100 + index,
        }
    )


def _artifact(tmp_path, version="r2"):
    manifest = {
        "schema_sha256": "fixture-schema",
        "bar_feature_version": settings.bar_feature_version,
        "feature_version": settings.feature_version,
    }
    path = save_artifact(
        tmp_path,
        version,
        model=FixedFixtureModel(),
        metadata={
            "schema_sha256": manifest["schema_sha256"],
            "input_features": list(INPUT_FEATURES),
            "class_order": list(CLASS_ORDER),
            "outcome_feature_version": OUTCOME_FEATURE_VERSION,
            "selected_estimator": "fixed-fixture",
        },
        metrics={},
        dataset_manifest=manifest,
    )
    return path


def test_live_features_equal_training_encoding():
    window = _window()
    live = build_input_features(window, "XAUUSD", "H1", 0.1)
    row = context_half(window, "XAUUSD", "H1", 0.1)
    tags = tags_half(window, float(row["atr_at_bar"] or 0))
    training = _encode_tags(pd.DataFrame([{**row, "bar_tags": tags["bar_tags"]}]))
    expected = pd.concat(
        [training.assign(side=1), training.assign(side=-1)], ignore_index=True
    ).loc[:, INPUT_FEATURES]
    pd.testing.assert_frame_equal(live, expected)


def test_inference_signatures_have_no_forward_input():
    assert "forward" not in inspect.signature(build_input_features).parameters
    assert "forward" not in inspect.signature(infer_outcomes).parameters
    assert "forward" not in inspect.signature(outcome.fetch_closed_bar_window).parameters


def test_fixed_fixture_probabilities_are_deterministic(tmp_path):
    _artifact(tmp_path)
    first = infer_outcomes(
        _window(), "XAUUSD", "H1", 0.1, artifact_root=tmp_path, artifact_version="r2"
    )
    second = infer_outcomes(
        _window(), "XAUUSD", "H1", 0.1, artifact_root=tmp_path, artifact_version="r2"
    )
    assert first == second
    assert (first.long.p_win, first.long.p_loss, first.long.p_timeout) == (0.61, 0.24, 0.15)
    assert (first.short.p_win, first.short.p_loss, first.short.p_timeout) == (0.29, 0.51, 0.20)
    assert first.status == "pilot_shadow"
    assert first.pilot is True and first.promoted is False


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("schema_sha256", "wrong", "schema_sha256"),
        ("bar_feature_version", "0.0.0", "bar_feature_version"),
        ("feature_version", "0.0.0", "feature_version"),
    ],
)
def test_artifact_schema_and_feature_versions_must_match(tmp_path, field, value, match):
    path = _artifact(tmp_path)
    manifest_path = path / "dataset_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(OutcomeArtifactIncompatible, match=match):
        infer_outcomes(
            _window(), "XAUUSD", "H1", 0.1,
            artifact_root=tmp_path, artifact_version="r2"
        )


def test_requested_artifact_version_must_exist(tmp_path):
    _artifact(tmp_path, "r1")
    with pytest.raises(OutcomeArtifactUnavailable, match="not installed"):
        infer_outcomes(
            _window(), "XAUUSD", "H1", 0.1, artifact_root=tmp_path, artifact_version="r2"
        )


def test_artifact_directory_and_metadata_versions_must_match(tmp_path):
    path = _artifact(tmp_path)
    metadata_path = path / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["artifact_version"] = "r1"
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(OutcomeArtifactIncompatible, match="artifact_version"):
        infer_outcomes(
            _window(), "XAUUSD", "H1", 0.1,
            artifact_root=tmp_path, artifact_version="r2"
        )


def test_endpoint_returns_typed_503_when_artifact_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "outcome_artifact_root", tmp_path)
    monkeypatch.setattr(settings, "outcome_artifact_version", "r2")
    window = _window(30)

    def db_override():
        con = duckdb.connect(":memory:")
        con.register("fixture", window)
        con.execute(
            "CREATE TABLE candles AS SELECT 'XAUUSD' symbol, 'H1' timeframe, * FROM fixture"
        )
        try:
            yield con
        finally:
            con.close()

    app.dependency_overrides[outcome.get_db] = db_override
    try:
        response = TestClient(app).get(
            "/outcome-model/shadow",
            params={
                "symbol": "XAUUSD",
                "timeframe": "H1",
                "signal_ts": window.iloc[-1]["ts"].isoformat(),
            },
        )
    finally:
        app.dependency_overrides.pop(outcome.get_db, None)
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "outcome_artifact_absent",
        "message": f"Outcome artifact 'r2' is not installed at {tmp_path / 'r2'}",
        "retryable": False,
    }
