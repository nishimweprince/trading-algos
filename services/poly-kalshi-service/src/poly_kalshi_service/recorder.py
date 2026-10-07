"""The raw feed recorder: every frame from both venues, as received, in one file.

Lines are ``<recv_ns> <venue> <raw frame>``, gzip-compressed, **one file per
UTC hour for both venues**::

    <record_dir>/<YYYYMMDD>/pk_<YYYYMMDD>_<HH>_<session>.gz
    <record_dir>/<YYYYMMDD>/pk_<YYYYMMDD>_<HH>_<session>.gz.json  (manifest)

``session`` is the process start in ns, zero-padded: a restart within the hour
starts a new file instead of appending. A crash leaves a gzip stream without
its end marker, and a member appended after that would be unreadable; this way
a crash costs at most the unflushed tail of one file, which the reader keeps
up to the break.

``venue`` is ``k`` (Kalshi), ``p`` (Polymarket) or ``c`` (this process's own
control lines: window identity, reconnects, resolutions). One writer stamps
every line from one monotonic clock, so file order *is* arrival order and a
replay never has to merge (kpc trap 7: recorded file order turned out not to
be timestamp order, and a naive merge returned an empty join).

Compression runs in a ``gzip -1`` child process per open file, fed through a
pipe by a writer thread; the event loop only enqueues. Compressing in a thread
of this process held the GIL against the feed readers (PLAN 2.2); the child
compresses on another core. The file format is unchanged (gzip), so
``recordings.read_lines`` reads it as before. If this process dies, the child
sees EOF on its stdin and finishes the file with its gzip trailer. Without a
``gzip`` binary the recorder falls back to compressing on the writer thread.
The recorder never raises into its caller: a write error is counted, logged,
and reported on /v1/status.

**Disk guard.** Every ``DISK_CHECK_SECONDS`` the writer checks free space on the
recording volume. Below ``min_free_bytes`` it closes its file (manifest
written), stops writing and counts what it skips; it resumes once free space is
back above the floor plus ``RESUME_HEADROOM``.
"""

from __future__ import annotations

import gzip
import json
import logging
import queue
import shutil
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from ta_core.logging_config import log_event

__all__ = ["Recorder", "hour_path"]

FLUSH_SECONDS = 10.0
DISK_CHECK_SECONDS = 10.0
# Lines drained from the queue per batch and written as one string: one small
# gzip write per frame held the GIL ~2,000 times a second, and level 1 costs a
# fraction of level 6 for somewhat larger files.
BATCH = 4096
COMPRESS_LEVEL = 1
RESUME_HEADROOM = 1.2  # resume at 120% of the floor, so it does not flap
_STOP = object()
_FILE = "pk"  # the one open file's key


def hour_path(root: Path, recv_ns: int, session_ns: int = 0) -> tuple[Path, str]:
    stamp = datetime.fromtimestamp(recv_ns / 1e9, UTC)
    day = stamp.strftime("%Y%m%d")
    hour = stamp.strftime("%H")
    return root / day / f"pk_{day}_{hour}_{session_ns:020d}.gz", f"{day}T{hour}"


class _GzipPipe:
    """A text sink that a ``gzip -1`` child compresses into ``path`` (appending a member)."""

    def __init__(self, gzip_bin: str, path: Path, level: int) -> None:
        self._file = path.open("ab")
        self._proc = subprocess.Popen(
            [gzip_bin, f"-{level}", "-c"],
            stdin=subprocess.PIPE,
            stdout=self._file,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # a Ctrl-C to the service must not kill it mid-file
        )
        assert self._proc.stdin is not None
        self._stdin = self._proc.stdin

    def write(self, text: str) -> None:
        self._stdin.write(text.encode("utf-8"))

    def flush(self) -> None:
        self._stdin.flush()

    def close(self) -> None:
        try:
            self._stdin.close()
            self._proc.wait(timeout=30)
        finally:
            self._file.close()


@dataclass
class _Open:
    path: Path
    hour: str
    handle: IO[str] | _GzipPipe
    lines: int = 0
    streams: Counter[str] = field(default_factory=Counter)
    first_ns: int | None = None
    last_ns: int | None = None
    gaps: int = 0
    pending: list[str] = field(default_factory=list)

    def flush_pending(self) -> None:
        if self.pending:
            self.handle.write("".join(self.pending))
            self.pending.clear()


class Recorder:
    def __init__(
        self,
        root: Path,
        *,
        host_tag: str,
        queue_limit: int = 500_000,
        min_free_bytes: int = 0,
        disk_usage: Any = shutil.disk_usage,
        gzip_bin: str | None = "auto",
    ) -> None:
        self.root = root
        # "auto": the system gzip if there is one; None: compress on the writer thread.
        self.gzip_bin = shutil.which("gzip") if gzip_bin == "auto" else gzip_bin
        self.host_tag = host_tag
        self.session_ns = time.time_ns()
        self.min_free_bytes = min_free_bytes
        self._disk_usage = disk_usage
        self.disk_free_bytes: int | None = None
        self.disk_paused = False
        self.skipped_for_disk = 0
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_limit)
        self._open: dict[str, _Open] = {}
        self._thread: threading.Thread | None = None
        self.lines_written = 0
        self.dropped = 0
        self.errors = 0
        self.last_error: str | None = None
        self.files_closed = 0

    # --- event-loop side --------------------------------------------------------

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="pk-recorder", daemon=True)
        self._thread.start()

    def write(self, venue: str, recv_ns: int, text: str) -> None:
        """The hub's sink. Never blocks, never raises."""
        try:
            self._queue.put_nowait(("line", recv_ns, venue, text))
        except queue.Full:
            self.dropped += 1

    def note_gap(self) -> None:
        try:
            self._queue.put_nowait(("gap", _FILE))
        except queue.Full:
            self.dropped += 1

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._queue.put(_STOP)
        self._thread.join(timeout)
        self._thread = None

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "root": str(self.root),
            "lines_written": self.lines_written,
            "queue_depth": self._queue.qsize(),
            "dropped": self.dropped,
            "errors": self.errors,
            "last_error": self.last_error,
            "files_closed": self.files_closed,
            "disk_free_gb": None
            if self.disk_free_bytes is None
            else round(self.disk_free_bytes / 1e9, 2),
            "min_free_gb": round(self.min_free_bytes / 1e9, 2),
            "disk_paused": self.disk_paused,
            "skipped_for_disk": self.skipped_for_disk,
            "open_files": sorted(str(item.path) for item in self._open.values()),
            "compressor": "gzip-child" if self.gzip_bin else "thread",
        }

    # --- writer thread ----------------------------------------------------------

    def check_disk(self) -> None:
        """Pause or resume on free space. Runs on the writer thread (and in tests)."""
        try:
            free = int(self._disk_usage(self.root).free)
        except OSError as exc:
            self.last_error = f"disk_usage: {exc}"[:200]
            return
        self.disk_free_bytes = free
        if not self.disk_paused and free < self.min_free_bytes:
            self.disk_paused = True
            for symbol in list(self._open):
                self._close(symbol)
            log_event(
                "recorder_disk_paused",
                level=logging.ERROR,
                free_gb=round(free / 1e9, 2),
                min_free_gb=round(self.min_free_bytes / 1e9, 2),
            )
        elif self.disk_paused and free >= self.min_free_bytes * RESUME_HEADROOM:
            self.disk_paused = False
            log_event("recorder_disk_resumed", free_gb=round(free / 1e9, 2))

    def _run(self) -> None:
        last_flush = time.monotonic()
        last_disk = 0.0
        while True:
            try:
                batch = [self._queue.get(timeout=1.0)]
            except queue.Empty:
                batch = []
            while batch and len(batch) < BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            stop = _STOP in batch
            try:
                for item in batch:
                    if item is _STOP:
                        break
                    if item[0] == "line":
                        self._write_line(item[1], item[2], item[3])
                    elif item[0] == "gap" and item[1] in self._open:
                        self._open[item[1]].gaps += 1
                for current in self._open.values():
                    current.flush_pending()
                if time.monotonic() - last_disk >= DISK_CHECK_SECONDS:
                    self.check_disk()
                    last_disk = time.monotonic()
                if time.monotonic() - last_flush >= FLUSH_SECONDS:
                    for current in self._open.values():
                        current.handle.flush()
                    last_flush = time.monotonic()
            except Exception as exc:  # noqa: BLE001 - the recorder must not die
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log_event("recorder_error", level=logging.ERROR, error=self.last_error)
            if stop:
                break
        for symbol in list(self._open):
            self._close(symbol)

    def _write_line(self, recv_ns: int, venue: str, text: str) -> None:
        if self.disk_paused:
            self.skipped_for_disk += 1
            return
        symbol, stream = _FILE, venue
        path, hour = hour_path(self.root, recv_ns, self.session_ns)
        current = self._open.get(symbol)
        if current is not None and current.hour != hour:
            self._close(symbol)
            current = None
        if current is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Append: the same session re-opens its own file after a disk pause, as
            # a second gzip member, which the reader reads straight through.
            handle: IO[str] | _GzipPipe
            if self.gzip_bin:
                handle = _GzipPipe(self.gzip_bin, path, COMPRESS_LEVEL)
            else:
                handle = gzip.open(path, "at", encoding="utf-8", compresslevel=COMPRESS_LEVEL)
            current = _Open(path, hour, handle)
            self._open[symbol] = current
        current.pending.append(f"{recv_ns} {venue} {text}\n")
        current.lines += 1
        current.streams[stream] += 1
        current.first_ns = current.first_ns if current.first_ns is not None else recv_ns
        current.last_ns = recv_ns
        self.lines_written += 1

    def _close(self, symbol: str) -> None:
        current = self._open.pop(symbol)
        current.flush_pending()
        current.handle.close()
        manifest = current.path.with_name(current.path.name + ".json")
        previous: dict[str, Any] = {}
        if manifest.exists():
            previous = json.loads(manifest.read_text())
        streams = Counter(previous.get("streams", {})) + current.streams
        body = {
            "hour": current.hour,
            "host": self.host_tag,
            "lines": previous.get("lines", 0) + current.lines,
            "streams": dict(streams),
            "first_recv_ns": previous.get("first_recv_ns", current.first_ns),
            "last_recv_ns": current.last_ns,
            "gaps": previous.get("gaps", 0) + current.gaps,
            "sessions": previous.get("sessions", 0) + 1,
        }
        manifest.write_text(json.dumps(body, indent=2))
        self.files_closed += 1
