"""The decision journal: ``<state_dir>/journal/YYYY-MM-DD.jsonl``, by event time.

One record per fact, with a ``type`` field:

- ``decision``: a window-consuming crossing (``enter`` or ``skip``), with both
  venues' quotes, the gap, the leg, the entry price and, for an entry the
  paper venue did not take, why (``blocked``).
- ``fill``: the paper fill (VWAP, shares, fee, levels) next to the naive
  touch fill kpc assumed.
- ``settle``: the payout credited to cash, and the P&L.
- ``resolution``: each venue's outcome for every window, traded or not.
- ``window``: one summary per asset per window at the roll.

``research.readout`` reads only this journal, so the gates can be read on the
host without the raw tape.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["JsonlDaily", "read_journal"]


class JsonlDaily:
    """Append JSON lines to ``<dir>/<YYYY-MM-DD>.jsonl`` by the record's event time."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        self.errors = 0

    def write(self, t_ns: int, record: dict[str, Any]) -> None:
        if self.directory is None:
            return
        day = datetime.fromtimestamp(t_ns / 1e9, UTC).strftime("%Y-%m-%d")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / f"{day}.jsonl").open("a") as handle:
                handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        except OSError:
            self.errors += 1  # a record must never stop the engine


def read_journal(directory: Path) -> Iterator[dict[str, Any]]:
    for path in sorted(directory.glob("*.jsonl")):
        with path.open() as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)
