"""Chronological model selection, calibration, evaluation, and persistence."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

from app.ml.outcome.artifact import save_artifact
from app.ml.outcome.features import OUTCOME_FEATURE_VERSION
from app.ml.outcome.metrics import score_probabilities
from app.ml.outcome.model import (
    CLASS_ORDER,
    CalibratedOutcomeModel,
    ContextFrequencyBaseline,
    fit_temperature,
    reorder_probabilities,
)
from app.ml.outcome.preprocessing import INPUT_FEATURES, build_preprocessor

CALIBRATION_FRACTION = 0.15


def dataframe_schema_hash(frame: pd.DataFrame) -> str:
    schema = [{"name": name, "dtype": str(frame[name].dtype)} for name in frame.columns]
    payload = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _read_dataset(path: Path) -> pd.DataFrame:
    return pd.read_csv(path) if path.suffix.lower() == ".csv" else pd.read_parquet(path)


def _validate_dataset(frame: pd.DataFrame, manifest: dict[str, Any]) -> None:
    if manifest.get("manifest_version") != 1:
        raise ValueError(
            f"Unsupported dataset manifest version: {manifest.get('manifest_version')}"
        )
    if manifest.get("outcome_feature_version") != OUTCOME_FEATURE_VERSION:
        raise ValueError(
            "Outcome feature version mismatch: "
            f"expected={OUTCOME_FEATURE_VERSION!r}, "
            f"actual={manifest.get('outcome_feature_version')!r}"
        )
    if list(frame.columns) != manifest.get("columns"):
        raise ValueError("Dataset columns do not exactly match the manifest")
    actual_hash = dataframe_schema_hash(frame)
    if actual_hash != manifest.get("schema_sha256"):
        raise ValueError(
            f"Dataset schema mismatch: expected={manifest.get('schema_sha256')}, "
            f"actual={actual_hash}"
        )
    if tuple(manifest.get("label_policy", {}).get("outcomes", ())) != CLASS_ORDER:
        raise ValueError("Manifest class order does not match the outcome model contract")
    if set(frame["dataset_partition"]) != {"development", "holdout"}:
        raise ValueError("Dataset must contain development and holdout partitions")
    source = manifest.get("source", {})
    if source.get("provider") != "histdata":
        raise ValueError("Outcome training accepts HistData manifests only")


def _range_mask(frame: pd.DataFrame, value: dict[str, str]) -> np.ndarray:
    start, end = pd.Timestamp(value["min"]), pd.Timestamp(value["max"])
    return ((frame["ts"] >= start) & (frame["ts"] <= end)).to_numpy()


def _new_estimator(name: str) -> object:
    if name == "logistic":
        return LogisticRegression(max_iter=1000, solver="lbfgs", random_state=0)
    if name == "hist_gradient_boosting":
        return HistGradientBoostingClassifier(
            learning_rate=0.06,
            max_iter=200,
            max_leaf_nodes=31,
            l2_regularization=1.0,
            random_state=0,
        )
    raise ValueError(f"Unknown estimator: {name}")


def _fit_candidate(
    name: str, train: pd.DataFrame, validation: pd.DataFrame
) -> tuple[np.ndarray, object, object]:
    preprocessor = build_preprocessor()
    train_features = train.loc[:, INPUT_FEATURES]
    transformed = preprocessor.fit_transform(train_features)
    estimator = _new_estimator(name)
    estimator.fit(transformed, train["y_outcome"])
    raw = estimator.predict_proba(preprocessor.transform(validation.loc[:, INPUT_FEATURES]))
    return reorder_probabilities(raw, estimator.classes_), preprocessor, estimator


def _development_cv(
    development: pd.DataFrame, manifest: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    names = ("logistic", "hist_gradient_boosting")
    results: dict[str, Any] = {name: {"folds": []} for name in (*names, "context_frequency")}
    weighted: dict[str, list[tuple[int, float, float]]] = {name: [] for name in results}
    for fold in manifest["split_policy"]["folds"]:
        train = development.loc[_range_mask(development, fold["train_range"])]
        validation = development.loc[_range_mask(development, fold["validation_range"])]
        if train.empty or validation.empty:
            raise ValueError(f"Manifest fold {fold['fold']} selects no dataset rows")
        baseline = ContextFrequencyBaseline().fit(
            train.loc[:, INPUT_FEATURES], train["y_outcome"]
        )
        baseline_probabilities = baseline.predict_proba(validation.loc[:, INPUT_FEATURES])
        baseline_metrics = score_probabilities(
            validation, validation["y_outcome"], baseline_probabilities, detailed=False
        )
        results["context_frequency"]["folds"].append(
            {"fold": fold["fold"], **baseline_metrics}
        )
        weighted["context_frequency"].append(
            (
                len(validation),
                baseline_metrics["multiclass_log_loss"],
                baseline_metrics["multiclass_brier"],
            )
        )
        for name in names:
            probabilities, _, _ = _fit_candidate(name, train, validation)
            metrics = score_probabilities(
                validation, validation["y_outcome"], probabilities, detailed=False
            )
            results[name]["folds"].append({"fold": fold["fold"], **metrics})
            weighted[name].append(
                (
                    len(validation),
                    metrics["multiclass_log_loss"],
                    metrics["multiclass_brier"],
                )
            )
    for name, values in weighted.items():
        weights = np.array([value[0] for value in values], dtype=float)
        results[name]["aggregate"] = {
            "rows": int(weights.sum()),
            "multiclass_log_loss": float(
                np.average([value[1] for value in values], weights=weights)
            ),
            "multiclass_brier": float(
                np.average([value[2] for value in values], weights=weights)
            ),
        }
    selected = min(names, key=lambda name: results[name]["aggregate"]["multiclass_log_loss"])
    baseline_loss = results["context_frequency"]["aggregate"]["multiclass_log_loss"]
    for name in names:
        model_loss = results[name]["aggregate"]["multiclass_log_loss"]
        results[name]["lift_vs_context_frequency"] = {
            "absolute_log_loss": baseline_loss - model_loss,
            "relative_log_loss": (baseline_loss - model_loss) / baseline_loss,
        }
    return results, selected


def _calibration_split(
    development: pd.DataFrame, manifest: dict[str, Any]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    timestamps = development["ts"].drop_duplicates().sort_values(ignore_index=True)
    calibration_count = max(1, int(np.ceil(len(timestamps) * CALIBRATION_FRACTION)))
    calibration_start = len(timestamps) - calibration_count
    gap = max(
        int(manifest["split_policy"]["purge_bars"]),
        int(manifest["split_policy"]["embargo_bars"]),
    )
    fit_end = calibration_start - gap
    if fit_end <= 0:
        raise ValueError("Not enough development bars for a dedicated calibration block")
    fit = development[development["ts"].isin(timestamps.iloc[:fit_end])].copy()
    calibration = development[development["ts"].isin(timestamps.iloc[calibration_start:])].copy()
    if set(fit["y_outcome"]) != set(CLASS_ORDER) or set(calibration["y_outcome"]) != set(
        CLASS_ORDER
    ):
        raise ValueError("Training and calibration blocks must each contain all outcome classes")
    return fit, calibration


def _timestamp_range(frame: pd.DataFrame) -> dict[str, str]:
    return {
        "min": pd.Timestamp(frame["ts"].min()).isoformat(),
        "max": pd.Timestamp(frame["ts"].max()).isoformat(),
    }


def _git_state(repo_root: Path) -> dict[str, Any]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    return {"revision": revision, "dirty": dirty}


def train_outcome_model(
    dataset_path: Path, manifest_path: Path, artifact_root: Path, version: str
) -> tuple[Path, dict[str, Any]]:
    destination = artifact_root / version
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite immutable artifact: {destination}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    frame = _read_dataset(dataset_path)
    _validate_dataset(frame, manifest)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    frame = frame.sort_values(["ts", "side"], kind="stable", ignore_index=True)
    development = frame[frame["dataset_partition"] == "development"].copy()
    holdout = frame[frame["dataset_partition"] == "holdout"].copy()

    cv_metrics, selected_name = _development_cv(development, manifest)
    fit, calibration = _calibration_split(development, manifest)
    calibration_raw, preprocessor, estimator = _fit_candidate(
        selected_name, fit, calibration
    )
    temperature = fit_temperature(calibration_raw, calibration["y_outcome"])
    model = CalibratedOutcomeModel(preprocessor, estimator, temperature)
    calibration_probabilities = model.predict_proba(calibration.loc[:, INPUT_FEATURES])

    baseline = ContextFrequencyBaseline().fit(fit.loc[:, INPUT_FEATURES], fit["y_outcome"])
    holdout_probabilities = model.predict_proba(holdout.loc[:, INPUT_FEATURES])
    holdout_baseline_probabilities = baseline.predict_proba(holdout.loc[:, INPUT_FEATURES])
    holdout_metrics = score_probabilities(
        holdout, holdout["y_outcome"], holdout_probabilities
    )
    holdout_baseline_metrics = score_probabilities(
        holdout, holdout["y_outcome"], holdout_baseline_probabilities
    )
    baseline_loss = holdout_baseline_metrics["multiclass_log_loss"]
    model_loss = holdout_metrics["multiclass_log_loss"]
    metrics = {
        "selected_estimator": selected_name,
        "development_cv": cv_metrics,
        "calibration": {
            "temperature": temperature,
            "uncalibrated": score_probabilities(
                calibration, calibration["y_outcome"], calibration_raw, detailed=False
            ),
            "calibrated": score_probabilities(
                calibration,
                calibration["y_outcome"],
                calibration_probabilities,
                detailed=False,
            ),
        },
        "holdout": {
            "model": holdout_metrics,
            "context_frequency_baseline": holdout_baseline_metrics,
            "lift_vs_context_frequency": {
                "absolute_log_loss": baseline_loss - model_loss,
                "relative_log_loss": (baseline_loss - model_loss) / baseline_loss,
            },
        },
    }
    coverage_years = (
        frame["ts"].max() - frame["ts"].min()
    ).total_seconds() / (365.2425 * 86400)
    model_brier = holdout_metrics["multiclass_brier"]
    baseline_brier = holdout_baseline_metrics["multiclass_brier"]
    ece = holdout_metrics.get("reliability", {}).get("ece")
    gates = {
        "histdata_coverage_at_least_five_years": coverage_years >= 5.0,
        "log_loss_beats_empirical_baseline": model_loss < baseline_loss,
        "brier_no_worse_than_empirical_baseline": model_brier <= baseline_brier,
        "ece_at_most_0_05": ece is not None and ece <= 0.05,
        "causal_parity": False,
    }
    metrics["promotion"] = {
        "promoted": False,
        "eligible_after_parity": all(value for key, value in gates.items() if key != "causal_parity"),
        "history_coverage_years": coverage_years,
        "gates": gates,
    }
    repo_root = Path(__file__).resolve().parents[4]
    dependency_versions = {
        name: importlib.metadata.version(name)
        for name in ("numpy", "pandas", "scikit-learn", "joblib")
    }
    metadata = {
        "dataset_contract": manifest["dataset_contract"],
        "schema_sha256": manifest["schema_sha256"],
        "outcome_feature_version": manifest["outcome_feature_version"],
        "class_order": list(CLASS_ORDER),
        "input_features": list(INPUT_FEATURES),
        "selected_estimator": selected_name,
        "thresholds": {
            "minimum_confidence": 0.0,
            "positive_gross_expectancy": {"win_reward_r": 1.5, "loss_risk_r": 1.0},
        },
        "training_source": "histdata",
        "live_source": "capital",
        "promoted": False,
        "promotion_gates": metrics["promotion"],
        "ranges": {
            "training": _timestamp_range(fit),
            "calibration": _timestamp_range(calibration),
            "holdout": _timestamp_range(holdout),
        },
        "dataset": {
            "path": str(dataset_path),
            "manifest_path": str(manifest_path),
            "sources": manifest["source"],
        },
        "dependencies": dependency_versions,
        "git": _git_state(repo_root),
    }
    artifact_path = save_artifact(
        artifact_root,
        version,
        model=model,
        metadata=metadata,
        metrics=metrics,
        dataset_manifest=manifest,
    )
    return artifact_path, metrics
