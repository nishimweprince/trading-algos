#!/usr/bin/env python3
"""Explicitly promote a proven active meta artifact for market execution."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.services.execution_promotion import promote_execution_artifact  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-version", required=True)
    parser.add_argument("--new-version", required=True)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Create an immutable order-enabled copy and activate it; omit for dry-run.",
    )
    args = parser.parse_args()
    result = promote_execution_artifact(
        source_version=args.source_version,
        new_version=args.new_version,
        confirmed=args.yes,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
