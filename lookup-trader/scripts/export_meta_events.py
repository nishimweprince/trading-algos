#!/usr/bin/env python3
"""Build the deterministic calendar-enhanced meta-events v2 export."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))


def main() -> int:
    from app.services.meta_events_v2 import (
        build_meta_events_v2,
        event_path_v2,
        export_meta_events_v2,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--yes", action="store_true", help="write v2 artifacts")
    args = parser.parse_args()
    symbol, timeframe = args.symbol.upper(), args.timeframe.upper()
    frame, manifest, report = build_meta_events_v2(symbol, timeframe)
    if not args.yes:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "destination": str(event_path_v2()),
                    "rows": len(frame),
                    "manifest": manifest,
                    "report": report,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return 0
    count = export_meta_events_v2(symbol, timeframe)
    print(json.dumps({"written": count, "path": str(event_path_v2())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
