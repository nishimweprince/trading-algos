#!/usr/bin/env python3
"""Validate and atomically activate an already-built research-shadow pair."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.ml.meta.shadow_artifacts import activate_shadow_pair


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-version", required=True)
    parser.add_argument("--challenger-version", required=True)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if not args.yes:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "reference_version": args.reference_version,
                    "challenger_version": args.challenger_version,
                    "orders_enabled": False,
                },
                indent=2,
            )
        )
        return 0
    print(
        json.dumps(
            activate_shadow_pair(args.reference_version, args.challenger_version),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
