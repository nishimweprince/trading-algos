#!/usr/bin/env python3
"""Run the Saturday research-shadow retraining/promotion evaluator once."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
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
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="run at most once per UTC ISO week after the evaluator's UTC gate",
    )
    args = parser.parse_args()
    store = MetaShadowStore(settings.meta_shadow_db_path)
    now = datetime.now(UTC)
    schedule_key = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    if args.scheduled and store.state("last_scheduled_meta_evaluation") == schedule_key:
        print(json.dumps({"status": "already_evaluated", "utc_week": schedule_key}, indent=2))
        return 0
    try:
        with pipeline_lock(settings.data_dir / ".meta-training.lock"):
            result = evaluate_weekly_shadow(store, now=now, force=args.force)
    except PipelineLockedError as exc:
        raise SystemExit("Another meta training process is running") from exc
    if args.scheduled and result.get("status") != "not_due":
        store.set_state("last_scheduled_meta_evaluation", schedule_key)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
