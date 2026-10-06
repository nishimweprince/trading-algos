"""The gate read-out from a decision journal (live or replayed).

    python -m research.readout --journal data/state/dev/journal

Prints ``poly_kalshi_service.gates.evaluate`` and the settlement QA: windows
per asset per day (96 expected for a full day) and how often the two venues
resolved the same window differently.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from poly_kalshi_service.gates import evaluate
from poly_kalshi_service.trades import read_journal


def _main() -> None:
    parser = argparse.ArgumentParser(prog="research.readout")
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--cap", type=float, default=0.60)
    args = parser.parse_args()
    result = evaluate(read_journal(args.journal), args.cap)
    short = {k: n for k, n in result["windows_per_asset_day"].items() if n != 96}
    result["windows_not_96"] = short
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    _main()
