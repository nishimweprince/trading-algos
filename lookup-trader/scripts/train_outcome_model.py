#!/usr/bin/env python3
"""Train and persist an immutable calibrated outcome-model artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings
from app.ml.outcome.training import train_outcome_model


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train logistic and histogram-gradient-boosting outcome models"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=settings.data_dir / "exports" / "bar_features_training.parquet",
    )
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=settings.data_dir / "models" / "outcome",
    )
    parser.add_argument("--version", required=True, help="Immutable artifact version")
    args = parser.parse_args()
    manifest = args.manifest or args.dataset.with_suffix(".manifest.json")
    artifact, metrics = train_outcome_model(
        args.dataset, manifest, args.artifact_root, args.version
    )
    holdout = metrics["holdout"]
    summary = {
        "artifact": str(artifact),
        "selected_estimator": metrics["selected_estimator"],
        "holdout": {
            "model": {
                key: holdout["model"][key]
                for key in (
                    "rows",
                    "multiclass_log_loss",
                    "multiclass_brier",
                    "timeout_coverage",
                    "net_expectancy",
                )
            },
            "context_frequency_baseline": {
                key: holdout["context_frequency_baseline"][key]
                for key in ("multiclass_log_loss", "multiclass_brier")
            },
            "lift_vs_context_frequency": holdout["lift_vs_context_frequency"],
        },
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
