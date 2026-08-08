#!/usr/bin/env python3
"""Build paired immutable meta v1/v2 research-shadow artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))


def main() -> int:
    from app.ml.meta.shadow_artifacts import build_shadow_pair
    from app.services.meta_events import event_path
    from app.services.meta_events_v2 import event_path_v2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-version", required=True)
    parser.add_argument("--challenger-version", required=True)
    parser.add_argument("--yes", action="store_true", help="fit and write artifacts")
    parser.add_argument(
        "--activate",
        action="store_true",
        help="atomically activate the pair after building and validating it",
    )
    args = parser.parse_args()
    if not args.yes:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "reference_dataset": str(event_path()),
                    "challenger_dataset": str(event_path_v2()),
                    "reference_version": args.reference_version,
                    "challenger_version": args.challenger_version,
                    "audit_policy": "2009-2024 OOF only; 2025-2026 training-only",
                    "activate": args.activate,
                },
                indent=2,
            )
        )
        return 0
    result = build_shadow_pair(
        reference_version=args.reference_version,
        challenger_version=args.challenger_version,
        activate=args.activate,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
