"""Structured logging for lux-algo, bound to ta-core's implementation.

Kept as a package-local module so the call sites are unchanged; all it supplies
is the logger name and ``RuntimeLogs``.

``RuntimeLogs`` stays here rather than moving to ta-core because ta-core owns a
single events sink, while lux-algo keeps three domain-specific ones written
directly by ``service.py`` -- separately from ``log_event``. That is the case
ta-core's ``configure_file_logs`` docstring anticipates: reuse the writer, not
the routing.

One deliberate behaviour change: ta-core's ``JsonlLogger.append`` never raises,
where the copy this replaces propagated ``OSError``. A logs directory rotated
underneath a running poller now goes quiet with a single
``events_log_unwritable`` warning instead of surfacing at the call site. That is
ta-core's contract -- a logging failure must not take down the thing being
logged -- and it matters here because ``service.py`` appends to ``errors`` from
inside an exception handler.
"""

from __future__ import annotations

from pathlib import Path

from ta_core.logging_config import (
    JsonlLogger,
    configure_file_logs,
    log_event,
    reset_file_logs,
)
from ta_core.logging_config import configure_logging as _configure_logging

LOGGER_NAME = "lux_algo.events"

__all__ = [
    "LOGGER_NAME",
    "JsonlLogger",
    "RuntimeLogs",
    "configure_file_logs",
    "configure_logging",
    "log_event",
    "reset_file_logs",
]


def configure_logging(level: str) -> None:
    # The name keyword is load-bearing: without it every event reparents to
    # ta-core's default "ta.events" logger and anything filtering on
    # lux_algo.events goes silent.
    _configure_logging(level, name=LOGGER_NAME)


class RuntimeLogs:
    def __init__(self, logs_dir: Path) -> None:
        self.signals = JsonlLogger(logs_dir / "signals.jsonl")
        self.executions = JsonlLogger(logs_dir / "executions.jsonl")
        self.errors = JsonlLogger(logs_dir / "errors.jsonl")
