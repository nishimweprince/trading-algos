#!/usr/bin/env python3
"""Run the Saturday research-shadow retraining/promotion evaluator once."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings
from app.services.meta_retraining import evaluate_weekly_shadow
from app.services.meta_shadow_store import MetaShadowStore
from app.services.pipeline_lock import PipelineLockedError, pipeline_lock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="ignore Saturday schedule")
    args = parser.parse_args()
    try:
        with pipeline_lock(settings.data_dir / ".meta-training.lock"):
            result = evaluate_weekly_shadow(
                MetaShadowStore(settings.meta_shadow_db_path), force=args.force
            )
    except PipelineLockedError as exc:
        raise SystemExit("Another meta training process is running") from exc
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
