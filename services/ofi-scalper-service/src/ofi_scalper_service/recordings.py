"""Reading the recorder's files: lines, and the session in force at a time.

Shared by the daily check (which records each day's book mode), the status
page and research's replay.

A restart within the hour appends a new gzip member to the hour's file. After
a clean stop that is still one valid file; after a SIGKILL (or a power loss)
the killed process's member is left unfinished, and a plain gzip reader stops
there with ``zlib.error``, losing the rest of the hour. ``recorded_lines``
instead skips to the next member and goes on: only the lines around the kill
are lost (at most one flush interval).
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

__all__ = ["SESSION_PREFIX", "ReadState", "find_session", "read_lines", "recorded_lines"]

GZIP_MAGIC = b"\x1f\x8b\x08"
SYNC_MARK = b"\x00\x00\xff\xff"  # what a zlib sync flush leaves in the stream
READ_CHUNK = 1 << 20

SESSION_PREFIX = '{"stream":"_control@session"'


@dataclass
class ReadState:
    """What ``recorded_lines`` met: an unfinished tail, and damaged spots skipped."""

    truncated: bool = False
    corrupt: int = 0


def recorded_lines(path: Path, state: ReadState | None = None) -> Iterator[str]:
    """Every complete line of a recorder file, without its newline.

    A file that just ends (the open hour, or a crash at the end) sets
    ``state.truncated``; each damaged member that is skipped counts in
    ``state.corrupt``. The partial line at either kind of cut is dropped.
    """
    state = ReadState() if state is None else state
    with path.open("rb") as handle:
        offset: int | None = 0
        while offset is not None:
            produced = [False]
            resume = yield from _members(handle, offset, state, produced)
            if resume is None:
                return
            if produced[0]:
                state.corrupt += 1
            offset = _next_member(handle, resume)


def _members(
    handle: IO[bytes], offset: int, state: ReadState, produced: list[bool]
) -> Iterator[str]:
    """Decode consecutive members from ``offset``; on damage, return where to search on.

    Output is held until a flush point (the recorder's periodic sync flush leaves
    ``00 00 ff ff`` in the stream) or the member's end confirms it. Inflating
    damaged bytes can emit text copied from earlier in the stream before zlib
    notices, so on an error everything since the last confirmed point is dropped.
    Returns None at the end of the file. ``produced`` reports whether any data
    came out, so a false gzip signature found while resyncing is not counted.
    """
    handle.seek(offset)
    inflater = zlib.decompressobj(31)
    member_start = consumed = offset
    held = b""  # decompressed, not yet confirmed
    while chunk := handle.read(READ_CHUNK):
        pending = chunk
        while pending:
            mark = pending.find(SYNC_MARK)
            cut = len(pending) if mark < 0 else mark + len(SYNC_MARK)
            piece, pending = pending[:cut], pending[cut:]
            try:
                held += inflater.decompress(piece)
            except zlib.error:
                return member_start + 1
            if inflater.eof:
                consumed += len(piece) - len(inflater.unused_data)
                member_start = consumed
                pending = inflater.unused_data + pending
                inflater = zlib.decompressobj(31)
            else:
                consumed += len(piece)
                if mark < 0:
                    continue  # not confirmed yet
            if held:
                produced[0] = True
                *lines, held = held.split(b"\n")
                for line in lines:
                    yield line.decode("utf-8", errors="replace")
    # The file just ends: nothing follows to be mistaken for data, so keep it.
    if held:
        produced[0] = True
        for line in held.split(b"\n")[:-1]:
            yield line.decode("utf-8", errors="replace")
    state.truncated = consumed > member_start
    return None


def _next_member(handle: IO[bytes], offset: int) -> int | None:
    """The offset of the next gzip signature at or after ``offset``, if any."""
    handle.seek(offset)
    carry = b""
    base = offset
    while chunk := handle.read(READ_CHUNK):
        window = carry + chunk
        found = window.find(GZIP_MAGIC)
        if found >= 0:
            return base - len(carry) + found
        carry = window[-(len(GZIP_MAGIC) - 1) :]
        base += len(chunk)
    return None


def read_lines(path: Path) -> Iterator[tuple[int, str]]:
    """``(recv_ns, raw)`` per line; a truncated tail or a damaged member is skipped."""
    for line in recorded_lines(path):
        stamp, _, text = line.partition(" ")
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
