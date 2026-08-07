#!/usr/bin/env python3
"""Preview causal calendar features without rewriting meta-events or training."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))


def _date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    from app.services.calendar.features import build_feature_preview

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--from", dest="date_from", type=_date, required=True)
    parser.add_argument("--to", dest="date_to", type=_date, required=True)
    args = parser.parse_args()
    report = build_feature_preview(
        args.symbol,
        args.timeframe,
        args.date_from,
        args.date_to,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
