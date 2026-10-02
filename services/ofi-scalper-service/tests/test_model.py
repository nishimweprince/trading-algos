"""Pinned models: hashes, features, gates, calibration, scoring."""

from __future__ import annotations

import json

import pytest

from ofi_scalper_service.model import Calibrator, ModelError, engine_overrides, load_model
from ofi_scalper_service.policy import Scores
from ofi_scalper_service.state_engine import EngineConfig, feature_names
from tests.model_fixture import FEATURES, Dial, make_model_dir


def sample(**overrides) -> dict:
    values = {"symbol": "BTCUSDT", "t_ns": 1, **dict.fromkeys(FEATURES, 0.5)}
    values.update(overrides)
    return values


def test_loads_scores_and_reports(tmp_path) -> None:
    dial = Dial()
    make_model_dir(tmp_path)
    model = load_model(tmp_path, "v1", backend=dial.backend)
    dial.set(up=0.7, down=0.1)
    scores = model.scorer.score(sample(imbalance=0.25))
    assert scores == Scores(up=pytest.approx(0.7), down=pytest.approx(0.1), none=pytest.approx(0.2))
    assert dial.rows == [[0.5, 0.25, 0.5, 0.5]]  # manifest feature order
    assert model.policy.barrier_bp == 8 and model.gates.passed and not model.gates.binding
    summary = model.summary()
    assert summary["version"] == "v1" and summary["scoring_latency"]["n"] == 1


def test_missing_feature_means_no_score(tmp_path) -> None:
    make_model_dir(tmp_path)
    model = load_model(tmp_path, "v1", backend=Dial().backend)
    assert model.scorer.score(sample(spread_bp=None)) is None


def test_any_hash_mismatch_refuses(tmp_path) -> None:
    path = make_model_dir(tmp_path)
    (path / "strategy.md").write_text("edited after the gates ran\n")
    with pytest.raises(ModelError, match="strategy.md sha256"):
        load_model(tmp_path, "v1", backend=Dial().backend)


def test_a_listed_file_that_is_missing_refuses(tmp_path) -> None:
    path = make_model_dir(tmp_path)
    (path / "calibrator.json").unlink()
    with pytest.raises(ModelError, match="missing"):
        load_model(tmp_path, "v1", backend=Dial().backend)


def test_features_the_engine_does_not_produce_refuse(tmp_path) -> None:
    make_model_dir(tmp_path, features=[*FEATURES, "future_return_bp"])
    with pytest.raises(ModelError, match="future_return_bp"):
        load_model(tmp_path, "v1", backend=Dial().backend)


def test_a_model_that_failed_its_gates_refuses_unless_asked(tmp_path) -> None:
    make_model_dir(tmp_path, passed=False)
    with pytest.raises(ModelError, match="did not pass"):
        load_model(tmp_path, "v1", backend=Dial().backend)
    model = load_model(tmp_path, "v1", backend=Dial().backend, require_pass=False)
    assert not model.gates.passed


def test_gates_for_another_version_refuse(tmp_path) -> None:
    path = make_model_dir(tmp_path)
    gates = json.loads((path / "gates.json").read_text())
    gates["model_version"] = "v0"
    (path / "gates.json").write_text(json.dumps(gates))
    manifest = json.loads((path / "manifest.json").read_text())
    from ofi_scalper_service.model import _sha256

    manifest["files"]["gates.json"] = _sha256(path / "gates.json")
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ModelError, match="gates.json is for 'v0'"):
        load_model(tmp_path, "v1", backend=Dial().backend)


def test_bad_policy_and_missing_dir_refuse(tmp_path) -> None:
    make_model_dir(tmp_path, policy={"horizon_s": 5, "threshold": 2, "barrier_bp": 8})
    with pytest.raises(ModelError, match="threshold"):
        load_model(tmp_path, "v1", backend=Dial().backend)
    with pytest.raises(ModelError, match="no manifest"):
        load_model(tmp_path, "v9", backend=Dial().backend)


def test_calibrator_interpolates_clamps_and_renormalises() -> None:
    calibrator = Calibrator(
        {
            "up": {"x": [0.0, 0.5, 1.0], "y": [0.0, 0.2, 0.6]},
            "down": {"x": [0.0, 1.0], "y": [0.0, 1.0]},
            "none": {"x": [0.0, 1.0], "y": [0.0, 1.0]},
        }
    )
    scores = calibrator({"up": 0.75, "down": 0.1, "none": 0.3})
    # up -> 0.4, down -> 0.1, none -> 0.3; sum 0.8
    assert scores.up == pytest.approx(0.5) and scores.down == pytest.approx(0.125)
    with pytest.raises(ModelError, match="sorted"):
        Calibrator({"up": {"x": [1, 0], "y": [0, 1]}, "down": {}, "none": {}})


def test_engine_json_feeds_the_feature_engine() -> None:
    overrides = engine_overrides(
        {"pca_weights": [0.5] * 10, "microprice_table": [[3, 1, 0.25]], "spread_cap": 4}
    )
    config = EngineConfig(tick_sizes={"BTCUSDT": 0.1}, **overrides)
    assert config.pca_weights == (0.5,) * 10
    assert config.microprice_table == {(3, 1): 0.25} and config.spread_cap == 4
    with pytest.raises(ModelError, match="10 entries"):
        engine_overrides({"pca_weights": [1.0]})


def test_a_real_catboost_model_round_trips(tmp_path) -> None:
    catboost = pytest.importorskip("catboost")
    import random

    rng = random.Random(7)
    features = feature_names(cross=False)[:6]
    rows, labels = [], []
    for _ in range(300):
        row = [rng.uniform(-1, 1) for _ in features]
        rows.append(row)
        labels.append("up" if row[0] > 0.3 else "down" if row[0] < -0.3 else "none")
    model = catboost.CatBoostClassifier(
        iterations=30, depth=3, verbose=False, random_seed=7, allow_writing_files=False
    )
    model.fit(rows, labels)
    source = tmp_path / "trained.cbm"
    model.save_model(str(source))
    make_model_dir(tmp_path, features=features, model_bytes=source.read_bytes())
    loaded = load_model(tmp_path, "v1")
    up = loaded.scorer.score(dict(zip(features, [0.9, 0, 0, 0, 0, 0], strict=True)))
    down = loaded.scorer.score(dict(zip(features, [-0.9, 0, 0, 0, 0, 0], strict=True)))
    assert up is not None and down is not None
    assert up.up > up.down and down.down > down.up
    assert loaded.scorer.latency()["p50_us"] < 5_000
