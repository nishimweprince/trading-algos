"""The gate read-out from a decision journal (live or replayed).

    python -m research.readout --journal data/state/dev/journal

Reads one results epoch (``--epoch``, default the latest in the journal) and
prints ``poly_kalshi_service.gates.evaluate`` with the settlement QA: windows
per asset per day (96 expected for a full day) and how often the two venues
resolved the same window differently. Also:

- ``shadow``: per variant (``live`` first) the windows, crossings, settled
  entries, win rate, average price and net ¢/share at the touch with its s.e.;
- ``markouts``: the cheap leg 1, 5 and 30 s after fills and after no-fills;
- ``capacity``: median shares offered at the limit per asset (``depth_at_limit``).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from poly_kalshi_service.gates import evaluate
from poly_kalshi_service.results import paper_results
from poly_kalshi_service.shadow import shadow_table
from poly_kalshi_service.trades import in_epoch, read_journal


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.readout")
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--cap", type=float, default=0.60)
    parser.add_argument(
        "--epoch", help="results epoch to read (default: the latest in the journal)"
    )
    args = parser.parse_args()
    records, excluded = in_epoch(list(read_journal(args.journal)), args.epoch)
    result = evaluate(records, args.cap)
    result["epoch"] = args.epoch or next(
        (r["epoch"] for r in reversed(records) if r.get("epoch")), None
    )
    result["excluded_records"] = excluded
    short = {k: n for k, n in result["windows_per_asset_day"].items() if n != 96}
    result["windows_not_96"] = short
    result["shadow"] = shadow_table(records)
    result["markouts"] = paper_results(records)["execution"]["markouts"]
    depth: dict[str, list[float]] = {}
    for r in records:
        if r.get("type") == "decision" and r.get("depth_at_limit") is not None:
            depth.setdefault(r["asset"], []).append(r["depth_at_limit"] * (r["entry_px"] or 0))
    result["capacity_usd_at_limit_median"] = {
        a: sorted(v)[len(v) // 2] for a, v in sorted(depth.items())
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    _main()
