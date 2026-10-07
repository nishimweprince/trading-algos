"""The gate read-out from a decision journal (live or replayed).

    python -m research.readout --journal data/state/dev/journal

Reads one results epoch (``--epoch``, default the latest in the journal) and
prints ``poly_kalshi_service.gates.evaluate`` with the settlement QA: windows
per asset per day (96 expected for a full day) and how often the two venues
resolved the same window differently. Also:

- ``shadow``: per variant (``live`` first) the windows, crossings, settled
  entries, win rate, average price and net ¢/share at the touch with its s.e.;
- ``markouts``: the cheap leg 1, 5 and 30 s after fills and after no-fills;
- ``capacity``: median shares offered at the limit per asset (``depth_at_limit``);
- ``exits``: net ¢/share per exit rule against holding to settlement, on the
  same orders (PREREG part 3, P4). An exit sells at the held leg's best bid,
  taker fee on both legs. Where there was no bid at the horizon, or the gap
  never closed, the order is held, so every rule has the same n.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from poly_kalshi_service.engine import EXIT_S
from poly_kalshi_service.fees import taker_fee
from poly_kalshi_service.gates import evaluate
from poly_kalshi_service.results import paper_results
from poly_kalshi_service.shadow import shadow_table
from poly_kalshi_service.trades import in_epoch, read_journal


def _sell_net(bid: float | None, ref: float) -> float | None:
    """¢/share from buying at ``ref`` and selling at ``bid``, taker fee on both legs."""
    if bid is None or not 0 < bid < 1:
        return None
    return 100 * (bid - ref - taker_fee(ref, 1.0) - taker_fee(bid, 1.0))


def exit_table(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per group (``fills``, ``no_fills``, ``all``) and per rule (``hold``,
    ``exit_<h>s`` for each of ``EXIT_S``, ``exit_gap``): n, mean net ¢/share and
    how many orders actually exited under the rule."""
    outcome = {
        (r["asset"], r["open_unix"]): r["outcome"]
        for r in records
        if r.get("type") == "resolution" and r.get("venue") == "poly"
    }
    rules = ["hold", *(f"exit_{h}s" for h in EXIT_S), "exit_gap"]
    groups: dict[str, dict[str, list[tuple[float, bool]]]] = {
        g: {rule: [] for rule in rules} for g in ("fills", "no_fills", "all")
    }
    for r in records:
        if r.get("type") != "markout":
            continue
        out = outcome.get((r["asset"], r["open_unix"]))
        if out is None:
            continue
        ref = r["ref_px"]
        hold = 100 * ((r["side"] == out) - ref - taker_fee(ref, 1.0))
        nets = {"hold": (hold, False)}
        for h in EXIT_S:
            x = _sell_net(r.get(f"bid_{h}s"), ref)
            nets[f"exit_{h}s"] = (hold, False) if x is None else (x, True)
        x = _sell_net(r.get("exit_gap_bid"), ref) if r.get("exit_gap_s") is not None else None
        nets["exit_gap"] = (hold, False) if x is None else (x, True)
        for g in ("fills" if r.get("filled") else "no_fills", "all"):
            for rule, v in nets.items():
                groups[g][rule].append(v)
    return {
        g: {
            rule: {
                "n": len(v),
                "exited": sum(1 for _x, e in v if e),
                "net_cents": None if not v else round(sum(x for x, _e in v) / len(v), 3),
            }
            for rule, v in by_rule.items()
        }
        for g, by_rule in groups.items()
    }


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
    result["exits"] = exit_table(records)
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
