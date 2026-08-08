#!/usr/bin/env python3
"""Check worker freshness and emit one idempotent alert per stale episode."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.config import settings
from app.services.meta_event_notifications import MetaEventNotifier
from app.services.meta_shadow_store import MetaShadowStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-seconds", type=int, default=settings.meta_shadow_stale_seconds)
    args = parser.parse_args()
    if args.max_age_seconds <= 0:
        raise SystemExit("--max-age-seconds must be positive")

    store = MetaShadowStore(settings.meta_shadow_db_path)
    status = store.status()
    raw = status.get("last_run_at")
    last = datetime.fromisoformat(raw) if raw else None
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - last).total_seconds() if last else None
    stale = age is None or age > args.max_age_seconds
    episode = raw or "never"
    notifier = MetaEventNotifier.from_settings(settings)
    notification = None
    if stale:
        notification = notifier.notify_operational(
            subject="lookup-trader meta-shadow worker is stale",
            message=(
                "RESEARCH SHADOW OPERATIONS ALERT\n"
                f"Last completed worker run: {episode}\n"
                f"Age seconds: {age if age is not None else 'unknown'}\n"
                "No orders are enabled. Inspect the worker and Capital/calendar freshness."
            ),
            idempotency_key=f"meta-shadow-stale:{episode}",
        ).status
    result = {
        "status": "stale" if stale else "healthy",
        "last_run_at": raw,
        "age_seconds": age,
        "max_age_seconds": args.max_age_seconds,
        "notification": notification,
        "orders_enabled": False,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
