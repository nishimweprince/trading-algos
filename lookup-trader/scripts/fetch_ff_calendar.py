#!/usr/bin/env python3
"""Fetch Forex Factory calendar HTML and update events.parquet."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "server"))

from app.services.calendar.store import ingest_day  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch FF calendar for one day")
    parser.add_argument("--date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), required=True)
    parser.add_argument("--offline", action="store_true", help="Use cached HTML only")
    args = parser.parse_args()

    count = ingest_day(args.date, use_network=not args.offline)
    print(f"Ingested {count} events for {args.date}")


if __name__ == "__main__":
    main()
