"""Reader for the recorder's tape: ``(recv_ns, venue, text)`` in file order.

File order is arrival order (one writer, one clock), so no merge is needed.
A file that ends without its gzip end marker (a crash, or the hour the live
process is still writing) is read up to the break and counted in
``stats["truncated"]``.
The reader still checks it: a line older than its predecessor is counted in
``stats["out_of_order"]``, never silently re-sorted (kpc trap 7). An empty
result from a directory that has files is reported, not returned quietly.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

__all__ = ["iter_files", "read_lines"]


def iter_files(root: Path, start: str | None = None, end: str | None = None) -> list[Path]:
    """Hour files under ``root`` in time order; ``start``/``end`` as ``YYYYMMDD[_HH]``.

    Names are ``pk_<YYYYMMDD>_<HH>_<session>.gz``: sorting by name orders by hour,
    then by session within an hour.
    """
    files = sorted(root.rglob("pk_*.gz"), key=lambda p: p.name)
    if start:
        files = [p for p in files if p.name[3 : 3 + len(start)] >= start]
    if end:
        files = [p for p in files if p.name[3 : 3 + len(end)] <= end]
    return files


def read_lines(
    paths: list[Path], stats: dict[str, int] | None = None
) -> Iterator[tuple[int, str, str]]:
    stats = stats if stats is not None else {}
    stats.setdefault("lines", 0)
    stats.setdefault("bad", 0)
    stats.setdefault("out_of_order", 0)
    stats.setdefault("truncated", 0)
    last = 0
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            while True:
                try:
                    raw = handle.readline()
                except (EOFError, gzip.BadGzipFile, OSError):
                    # A crash, or the file the live process is still writing: keep
                    # what is there, and say so.
                    stats["truncated"] += 1
                    break
                if not raw:
                    break
                if not raw.endswith("\n"):
                    stats["bad"] += 1  # a half-flushed last line
                    continue
                parts = raw.rstrip("\n").split(" ", 2)
                if len(parts) != 3 or not parts[0].isdigit():
                    stats["bad"] += 1
                    continue
                recv_ns = int(parts[0])
                if recv_ns < last:
                    stats["out_of_order"] += 1
                last = max(last, recv_ns)
                stats["lines"] += 1
                yield recv_ns, parts[1], parts[2]
    if paths and stats["lines"] == 0:
        raise ValueError(f"{len(paths)} recording files and no readable line: a reader bug?")
