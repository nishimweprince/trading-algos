#!/usr/bin/env python3
"""Refresh trusted calendar coverage for live meta inference."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))


def main() -> int:
    from app.services.calendar.store import ingest_range

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true", help="fetch and publish refreshed weeks"
    )
    parser.add_argument(
        "--as-of", type=date.fromisoformat, default=datetime.now(UTC).date()
    )
    args = parser.parse_args()
    start = args.as_of - timedelta(days=7)
    end = args.as_of + timedelta(days=14)
    if not args.yes:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "date_from": start.isoformat(),
                    "date_to": end.isoformat(),
                    "force_refresh": True,
                    "orders_enabled": False,
                },
                indent=2,
            )
        )
        return 0
    report = ingest_range(
        start,
        end,
        use_network=True,
        force_refresh=True,
        write_report=True,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
