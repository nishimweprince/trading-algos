"""The raw feed recorder: every frame, as received, with its local timestamp.

Lines are ``<recv_ns> <raw combined-stream JSON>``, gzip-compressed, one file
per symbol per UTC hour::

    <record_dir>/<SYMBOL>/<YYYYMMDD>/<SYMBOL>_<YYYYMMDD>_<HH>.gz
    <record_dir>/<SYMBOL>/<YYYYMMDD>/<SYMBOL>_<YYYYMMDD>_<HH>.gz.json  (manifest)

The line layout is modelled on hftbacktest's collector so its Binance-futures
converter can be pointed at these files in Stage 2 (compatibility to be
confirmed then). Depth snapshots are recorded as
``{"stream": "<s>@depthSnapshot", "data": <REST body>}`` so a replay can rebuild
the book exactly as the live process did.

Compression and disk writes happen on a writer thread; the event loop only
enqueues. The recorder never raises into its caller: a write error is counted,
logged, and reported on /v1/status.
"""

from __future__ import annotations

import gzip
import json
import logging
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from ta_core.logging_config import log_event

__all__ = ["Recorder", "hour_path", "symbol_and_stream"]

FLUSH_SECONDS = 10.0
_STOP = object()


def symbol_and_stream(text: str) -> tuple[str, str]:
    """``("BTCUSDT", "depth@0ms")`` from a combined-stream frame, cheaply."""
    marker = '"stream":"'
    start = text.find(marker)
    if start < 0:
        return "_misc", "unknown"
    start += len(marker)
    end = text.find('"', start)
    name = text[start:end]
    symbol, _, stream = name.partition("@")
    if not stream:
        return "_misc", name or "unknown"
    return symbol.upper(), stream


def hour_path(root: Path, symbol: str, recv_ns: int) -> tuple[Path, str]:
    stamp = datetime.fromtimestamp(recv_ns / 1e9, UTC)
    day = stamp.strftime("%Y%m%d")
    hour = stamp.strftime("%H")
    return root / symbol / day / f"{symbol}_{day}_{hour}.gz", f"{day}T{hour}"


@dataclass
class _Open:
    path: Path
    hour: str
    handle: IO[str]
    lines: int = 0
    streams: Counter[str] = field(default_factory=Counter)
    first_ns: int | None = None
    last_ns: int | None = None
    gaps: int = 0


class Recorder:
    def __init__(self, root: Path, *, host_tag: str, queue_limit: int = 500_000) -> None:
        self.root = root
        self.host_tag = host_tag
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
        self._thread = threading.Thread(target=self._run, name="ofi-recorder", daemon=True)
        self._thread.start()

    def write(self, channel: str, recv_ns: int, text: str) -> None:
        """The ``on_raw`` sink. Never blocks, never raises."""
        try:
            self._queue.put_nowait(("line", recv_ns, text))
        except queue.Full:
            self.dropped += 1

    def note_gap(self, symbol: str) -> None:
        try:
            self._queue.put_nowait(("gap", symbol))
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
            "open_files": sorted(str(item.path) for item in self._open.values()),
        }

    # --- writer thread ----------------------------------------------------------

    def _run(self) -> None:
        last_flush = time.monotonic()
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                item = None
            if item is _STOP:
                break
            try:
                if item is not None and item[0] == "line":
                    self._write_line(item[1], item[2])
                elif item is not None and item[0] == "gap":
                    if item[1] in self._open:
                        self._open[item[1]].gaps += 1
                if time.monotonic() - last_flush >= FLUSH_SECONDS:
                    for current in self._open.values():
                        current.handle.flush()
                    last_flush = time.monotonic()
            except Exception as exc:  # noqa: BLE001 - the recorder must not die
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log_event("recorder_error", level=logging.ERROR, error=self.last_error)
        for symbol in list(self._open):
            self._close(symbol)

    def _write_line(self, recv_ns: int, text: str) -> None:
        symbol, stream = symbol_and_stream(text)
        path, hour = hour_path(self.root, symbol, recv_ns)
        current = self._open.get(symbol)
        if current is not None and current.hour != hour:
            self._close(symbol)
            current = None
        if current is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Append: a restart within the hour adds a gzip member, still one valid file.
            handle = gzip.open(path, "at", encoding="utf-8", compresslevel=6)
            current = _Open(path, hour, handle)
            self._open[symbol] = current
        current.handle.write(f"{recv_ns} {text}\n")
        current.lines += 1
        current.streams[stream] += 1
        current.first_ns = current.first_ns if current.first_ns is not None else recv_ns
        current.last_ns = recv_ns
        self.lines_written += 1

    def _close(self, symbol: str) -> None:
        current = self._open.pop(symbol)
        current.handle.close()
        manifest = current.path.with_name(current.path.name + ".json")
        previous: dict[str, Any] = {}
        if manifest.exists():
            previous = json.loads(manifest.read_text())
        streams = Counter(previous.get("streams", {})) + current.streams
        body = {
            "symbol": symbol,
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
