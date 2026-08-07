#!/usr/bin/env python3
"""Audit candle coverage and write a non-destructive exclusion manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "server"))


def main() -> int:
    from app.services.candle_audit import build_audit, write_audit

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()
    if args.write_report:
        report_path, exclusion_path, report = write_audit(args.symbol, args.timeframe)
        print(f"Wrote {report_path}")
        print(f"Wrote {exclusion_path}")
    else:
        report, _ = build_audit(args.symbol, args.timeframe)
    print(
        json.dumps(
            {
                "status": report["status"],
                "bars": report["bars"],
                "range": report["range"],
                "excluded_months": report["excluded_months"],
                "expanded_exclusion_intervals": report["expanded_exclusion_intervals"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
