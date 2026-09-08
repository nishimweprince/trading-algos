"""Structured logging for ipda, bound to ta-core's implementation.

Kept as a service-local module so the call sites are unchanged; all it supplies
is the logger name and ``RuntimeLogs``.

``RuntimeLogs`` stays here rather than moving to ta-core because ta-core owns a
single events sink, while ipda keeps three domain-specific ones. That is the
case ta-core's ``configure_file_logs`` docstring anticipates: reuse the writer,
not the routing.

Adopting ``ta_core.JsonlLogger`` is a hardening. The copy this replaces could
raise from ``append`` and left the file at the process umask; ta-core's never
raises, retries once through a directory that was rotated underneath a running
service, and keeps the file at mode 0600.
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

LOGGER_NAME = "ipda.events"

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
    _configure_logging(level, name=LOGGER_NAME)


class RuntimeLogs:
    def __init__(self, logs_dir: Path) -> None:
        self.signals = JsonlLogger(logs_dir / "signals.jsonl")
        self.executions = JsonlLogger(logs_dir / "executions.jsonl")
        self.errors = JsonlLogger(logs_dir / "errors.jsonl")
