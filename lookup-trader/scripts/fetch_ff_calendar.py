#!/usr/bin/env python3
"""Fetch or replay Forex Factory weekly pages into validated calendar artifacts."""

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
    from app.services.calendar.store import ingest_range

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date", type=_date, help="Compatibility shorthand for one date"
    )
    parser.add_argument("--from", dest="date_from", type=_date)
    parser.add_argument("--to", dest="date_to", type=_date)
    parser.add_argument(
        "--offline", action="store_true", help="Require cached weekly HTML"
    )
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="refetch requested weeks and preserve timestamped raw snapshots",
    )
    parser.add_argument(
        "--historical-backfill",
        action="store_true",
        help="Explicitly authorize and label a range longer than one year",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    args = parser.parse_args()
    if args.offline and args.refresh:
        parser.error("--offline and --refresh cannot be combined")

    if args.date:
        if args.date_from or args.date_to:
            parser.error("--date cannot be combined with --from or --to")
        date_from = date_to = args.date
    else:
        if not args.date_from or not args.date_to:
            parser.error("provide either --date or both --from and --to")
        date_from, date_to = args.date_from, args.date_to
    if (date_to - date_from).days > 366 and not args.historical_backfill:
        parser.error("ranges longer than one year require --historical-backfill")

    def progress(index: int, total: int, source_week: date, cached: bool) -> None:
        if args.quiet:
            return
        if index == 1 or index % 10 == 0 or index == total:
            source = "cache" if cached else "network"
            print(
                f"calendar week {index}/{total}: {source_week} ({source})",
                file=sys.stderr,
                flush=True,
            )

    report = ingest_range(
        date_from,
        date_to,
        use_network=not args.offline,
        write_report=args.write_report,
        historical_backfill=args.historical_backfill,
        force_refresh=args.refresh,
        progress=progress,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
