"""Reading the recorder's files: lines, and the session in force at a time.

Shared by the daily check (which records each day's book mode), the status
page and research's replay.
"""

from __future__ import annotations

import contextlib
import gzip
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["SESSION_PREFIX", "find_session", "read_lines"]

SESSION_PREFIX = '{"stream":"_control@session"'


def read_lines(path: Path) -> Iterator[tuple[int, str]]:
    """``(recv_ns, raw)`` per line; a truncated tail (crash, open hour) just ends."""
    with contextlib.suppress(EOFError), gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            stamp, _, text = line.rstrip("\n").partition(" ")
            if stamp.isdigit() and text:
                yield int(stamp), text


def find_session(record_dir: Path, until: datetime) -> dict[str, Any] | None:
    """The last ``_control@session`` recorded before ``until``, if any.

    The service writes it once at start, possibly days before the replayed
    hour, so every earlier ``_CONTROL`` file is searched (they are tiny).
    """
    limit = f"_CONTROL_{until.strftime('%Y%m%d_%H')}.gz"
    found: dict[str, Any] | None = None
    candidates = sorted(record_dir.glob("_CONTROL/*/_CONTROL_*.gz"), key=lambda p: p.name)
    for path in candidates:
        if path.name > limit:
            break
        for _, text in read_lines(path):
            if text.startswith(SESSION_PREFIX):
                found = json.loads(text)["data"]
    return found
