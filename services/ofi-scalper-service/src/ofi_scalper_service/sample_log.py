"""Opt-in log of every live feature sample, for replay parity checks.

With ``OFI_SAMPLE_LOG_DIR`` set, each grid and burst sample is appended as one
JSON line to ``<dir>/<YYYYMMDD>/samples_<HH>.jsonl.gz`` (UTC hour of the
sample). ``python -m research.replay compare`` replays the recordings for the
same hour and checks every value matches exactly. Off by default: it is roughly
as large as the recordings themselves, so enable it for a parity window, not
permanently.

Writes happen on a thread; the event loop only enqueues. Never raises.
"""

from __future__ import annotations

import gzip
import json
import logging
import queue
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from ta_core.logging_config import log_event

__all__ = ["SampleLog", "sample_path"]

_STOP = object()


def sample_path(root: Path, t_ns: int) -> Path:
    stamp = datetime.fromtimestamp(t_ns / 1e9, UTC)
    return root / stamp.strftime("%Y%m%d") / f"samples_{stamp.strftime('%H')}.jsonl.gz"


class SampleLog:
    def __init__(self, root: Path, *, queue_limit: int = 200_000) -> None:
        self.root = root
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_limit)
        self._thread: threading.Thread | None = None
        self._path: Path | None = None
        self._handle: IO[str] | None = None
        self.written = 0
        self.dropped = 0
        self.errors = 0

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="ofi-sample-log", daemon=True)
        self._thread.start()

    def write(self, sample: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(sample)
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
            "root": str(self.root),
            "written": self.written,
            "dropped": self.dropped,
            "errors": self.errors,
        }

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                break
            try:
                path = sample_path(self.root, int(item["t_ns"]))
                if path != self._path:
                    if self._handle is not None:
                        self._handle.close()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._handle = gzip.open(path, "at", encoding="utf-8")
                    self._path = path
                assert self._handle is not None
                self._handle.write(json.dumps(item, separators=(",", ":")) + "\n")
                self.written += 1
            except Exception as exc:  # noqa: BLE001 - never take the service down
                self.errors += 1
                log_event("sample_log_error", level=logging.ERROR, error=repr(exc)[:200])
        if self._handle is not None:
            self._handle.close()
