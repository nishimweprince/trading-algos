"""Validate recorded files: ``ofi-gapcheck data/raw/dev/BTCUSDT/20261001``.

For each ``.gz`` file (or every one under a directory) it checks:

- local timestamps never go backwards;
- the depth chain (``pu`` == previous ``u``) holds, and every break is
  followed by a snapshot that a later diff bridges (``U <= lastUpdateId <= u``);
- for partial-depth (snapshot) streams, which cannot break, how many snapshots
  were skipped (``pu`` != previous ``u``): informational, never a failure;
- aggTrade ids are consecutive;
- the longest silence between frames.

A file that ends mid-stream (the hour still being written, or a crash) is
reported with ``truncated: true`` and checked up to the cut; the lines before
it are valid, so that alone is not a failure.

Prints one JSON report per file and exits 1 if any file has a backwards
timestamp or a depth break that was never recovered.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any

__all__ = ["check_file", "run"]

_PARTIAL = re.compile(r"@depth(5|10|20)(@|$)")


def check_file(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "file": str(path),
        "lines": 0,
        "backwards": 0,
        "unparsed": 0,
        "depth_breaks": 0,
        "unrecovered_breaks": 0,
        "snapshots": 0,
        "agg_trade_gaps": 0,
        "partial_snapshots": 0,
        "partial_skipped": 0,
        "max_silence_ms": 0.0,
        "truncated": False,
    }
    report["_awaiting"] = {}
    try:
        _scan(path, report)
    except EOFError:
        report["truncated"] = True
    report["unrecovered_breaks"] = sum(
        1 for snap in report.pop("_awaiting").values() if snap is None
    )
    report["ok"] = report["backwards"] == 0 and report["unrecovered_breaks"] == 0
    return report


def _scan(path: Path, report: dict[str, Any]) -> None:
    last_ns: int | None = None
    last_u: dict[str, int] = {}
    # symbol -> snapshot id, or None (no snapshot yet); read back by check_file.
    awaiting: dict[str, int | None] = report.setdefault("_awaiting", {})
    last_trade: dict[str, int] = {}
    last_partial: dict[str, int] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            report["lines"] += 1
            stamp, _, text = line.rstrip("\n").partition(" ")
            try:
                recv_ns = int(stamp)
                frame = json.loads(text)
            except ValueError:
                report["unparsed"] += 1
                continue
            if last_ns is not None:
                if recv_ns < last_ns:
                    report["backwards"] += 1
                report["max_silence_ms"] = max(report["max_silence_ms"], (recv_ns - last_ns) / 1e6)
            last_ns = recv_ns
            stream = str(frame.get("stream", ""))
            data = frame.get("data", {})
            symbol = stream.split("@", 1)[0].upper()
            if stream.endswith("@depthSnapshot"):
                report["snapshots"] += 1
                awaiting[symbol] = int(data["lastUpdateId"])
                continue
            kind = data.get("e") if isinstance(data, dict) else None
            if kind == "depthUpdate" and _PARTIAL.search(stream):
                report["partial_snapshots"] += 1
                if symbol in last_partial and int(data["pu"]) != last_partial[symbol]:
                    report["partial_skipped"] += 1
                last_partial[symbol] = int(data["u"])
            elif kind == "depthUpdate":
                first, final, prev = int(data["U"]), int(data["u"]), int(data["pu"])
                if symbol in awaiting:
                    snap = awaiting[symbol]
                    if snap is None or final < snap:
                        continue
                    if first <= snap <= final:
                        del awaiting[symbol]
                        last_u[symbol] = final
                        continue
                    continue  # this snapshot was stale; the live process fetched another
                if symbol in last_u and prev != last_u[symbol]:
                    report["depth_breaks"] += 1
                    awaiting[symbol] = None
                    last_u.pop(symbol)
                    continue
                last_u[symbol] = final
            elif kind == "aggTrade":
                trade_id = int(data["a"])
                if symbol in last_trade and trade_id != last_trade[symbol] + 1:
                    report["agg_trade_gaps"] += 1
                last_trade[symbol] = trade_id


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    files: list[Path] = []
    for path in args.paths:
        files += sorted(path.rglob("*.gz")) if path.is_dir() else [path]
    ok = True
    for path in files:
        report = check_file(path)
        ok = ok and report["ok"]
        print(json.dumps(report))
    sys.exit(0 if ok and files else 1)


if __name__ == "__main__":
    run()
