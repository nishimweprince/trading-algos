"""Build paired v1/v2 research-shadow artifacts without reading the spent audit."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.config import settings
from app.ml.meta import metrics as M
from app.ml.meta.artifact import save_artifact, write_active_shadow
from app.ml.meta.features import META_INPUT_FEATURES, META_INPUT_FEATURES_V2
from app.ml.meta.folds import assert_no_overlap, audit_split, year_folds
from app.ml.meta.training import CatBoostCandidate
from app.services.meta_events import event_manifest_path, event_path
from app.services.meta_events_v2 import event_manifest_path_v2, event_path_v2

FIRST_TEST_YEAR = 2014
LAST_TEST_YEAR = 2024
AUDIT_FROM_YEAR = 2025
# Alert selectivity as a declared decision rather than a frozen number. The
# threshold is re-solved for this share on every build, so `would_take` keeps a
# stable meaning across versions even though the probability distribution
# shifts. Chosen to fill the 25-selection promotion gate in ~29 days at the
# observed 4.4 events/day — inside the binding 250-event gate at ~57 days —
# while still being a real filter.
TARGET_TAKE_RATE = 0.20
FROZEN_CATBOOST_PARAMS = {
    "depth": 4,
    "iterations": 300,
    "learning_rate": 0.0104,
    "l2_leaf_reg": 8.37,
    "subsample": 0.914,
}


@dataclass
class MetaShadowModel:
    candidate: CatBoostCandidate
    feature_columns: tuple[str, ...]
    threshold: float

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        return self.candidate.predict_proba(frame)


def _git_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[4],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _load_pair() -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], dict[str, Any]]:
    paths = (event_path(), event_path_v2(), event_manifest_path(), event_manifest_path_v2())
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
    v1, v2 = pd.read_parquet(event_path()), pd.read_parquet(event_path_v2())
    v1 = v1.sort_values(["signal_ts", "side", "event_id"], kind="stable").reset_index(drop=True)
    v2 = v2.sort_values(["signal_ts", "side", "event_id"], kind="stable").reset_index(drop=True)
    if list(v1["event_id"]) != list(v2["event_id"]):
        raise ValueError("V1 and v2 must contain identical ordered event IDs")
    for name in ("y_meta", "net_r_3", "net_r_5", "net_r_8"):
        if not v1[name].equals(v2[name]):
            raise ValueError(f"V2 changed frozen labels or returns: {name}")
    return (
        v1,
        v2,
        json.loads(event_manifest_path().read_text(encoding="utf-8")),
        json.loads(event_manifest_path_v2().read_text(encoding="utf-8")),
    )


def _oof(
    frame: pd.DataFrame, feature_columns: tuple[str, ...]
) -> tuple[np.ndarray, np.ndarray, list[Any]]:
    development_idx, _ = audit_split(frame, audit_from_year=AUDIT_FROM_YEAR)
    development = frame.iloc[development_idx].reset_index(drop=True)
    folds = year_folds(
        development,
        first_test_year=FIRST_TEST_YEAR,
        last_test_year=LAST_TEST_YEAR,
    )
    assert_no_overlap(development, folds)
    probabilities: list[np.ndarray] = []
    positions: list[np.ndarray] = []
    for fold in folds:
        train, test = development.iloc[fold.train_idx], development.iloc[fold.test_idx]
        candidate = CatBoostCandidate(FROZEN_CATBOOST_PARAMS, feature_columns=feature_columns).fit(
            train, train["y_meta"]
        )
        probabilities.append(candidate.predict_proba(test))
        positions.append(fold.test_idx)
    return np.concatenate(probabilities), np.concatenate(positions), folds


def _evaluate(frame: pd.DataFrame, feature_columns: tuple[str, ...]) -> dict[str, Any]:
    development_idx, _ = audit_split(frame, audit_from_year=AUDIT_FROM_YEAR)
    development = frame.iloc[development_idx].reset_index(drop=True)
    probabilities, positions, folds = _oof(frame, feature_columns)
    scored = development.iloc[positions]
    threshold = M.threshold_for_take_rate(probabilities, TARGET_TAKE_RATE)
    per_fold: dict[str, Any] = {}
    for fold in folds:
        mask = np.isin(positions, fold.test_idx)
        per_fold[str(fold.test_year)] = M.at_threshold(
            development.iloc[positions[mask]], probabilities[mask], threshold
        )
    return {
        "oof_events": len(scored),
        "threshold": threshold,
        "target_take_rate": TARGET_TAKE_RATE,
        **M.probability_scores(scored["y_meta"].to_numpy(), probabilities),
        "at_threshold": M.at_threshold(scored, probabilities, threshold),
        "take_all": M.take_all(scored),
        "per_fold": per_fold,
        "audit_rows_read": 0,
    }


def _fit(frame: pd.DataFrame, feature_columns: tuple[str, ...], threshold: float):
    candidate = CatBoostCandidate(FROZEN_CATBOOST_PARAMS, feature_columns=feature_columns).fit(
        frame, frame["y_meta"]
    )
    return MetaShadowModel(candidate, feature_columns, threshold)


def build_shadow_pair(
    *,
    reference_version: str,
    challenger_version: str,
) -> dict[str, Any]:
    v1, v2, manifest_v1, manifest_v2 = _load_pair()
    metrics_v1 = _evaluate(v1, META_INPUT_FEATURES)
    metrics_v2 = _evaluate(v2, META_INPUT_FEATURES_V2)
    models = {
        reference_version: (
            _fit(v1, META_INPUT_FEATURES, metrics_v1["threshold"]),
            v1,
            manifest_v1,
            metrics_v1,
            1,
        ),
        challenger_version: (
            _fit(v2, META_INPUT_FEATURES_V2, metrics_v2["threshold"]),
            v2,
            manifest_v2,
            metrics_v2,
            2,
        ),
    }
    created_at = datetime.now(UTC).isoformat()
    for version, (model, frame, manifest, metrics, feature_version) in models.items():
        metadata = {
            "model_kind": "binary_meta_catboost",
            "model_version": "meta-shadow-v1",
            "status": "research_shadow",
            "promoted": False,
            "orders_enabled": False,
            "symbol": "XAUUSD",
            "timeframe": "H1",
            "feature_columns": list(model.feature_columns),
            "schema_sha256": manifest["schema_sha256"],
            "bar_feature_version": settings.bar_feature_version,
            "meta_feature_version": feature_version,
            "meta_label_version": settings.meta_label_version,
            "threshold": model.threshold,
            "target_take_rate": TARGET_TAKE_RATE,
            "training_rows": len(frame),
            "training_cutoff": pd.Timestamp(frame["signal_ts"].max()).isoformat(),
            "created_at": created_at,
            "git_revision": _git_revision(),
            "catboost_params": FROZEN_CATBOOST_PARAMS,
        }
        save_artifact(
            settings.meta_artifact_root,
            version,
            model=model,
            metadata=metadata,
            metrics=metrics,
            dataset_manifest=manifest,
        )

    comparison = {
        "report_version": 1,
        "status": "research_shadow_only",
        "reference_version": reference_version,
        "challenger_version": challenger_version,
        "development_period": "2009-2024",
        "spent_audit_evaluated": False,
        "v1": metrics_v1,
        "v2": metrics_v2,
        "orders_enabled": False,
    }
    report_path = settings.data_dir / "reports" / "meta-v1-v2-shadow-comparison.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(comparison, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    write_active_shadow(
        {
            "pointer_version": 1,
            "active_version": reference_version,
            "reference_version": reference_version,
            "challenger_version": challenger_version,
            "status": "research_shadow",
            "orders_enabled": False,
            "activated_at": created_at,
            "previous_active_version": None,
        }
    )
    return comparison
