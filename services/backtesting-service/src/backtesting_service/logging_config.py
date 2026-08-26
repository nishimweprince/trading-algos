"""Structured logging for backtesting-service, bound to ta-core's implementation.

Kept as a service-local module so the call sites are unchanged; all it supplies
is the logger name.
"""

from __future__ import annotations

from ta_core.logging_config import (
    JsonlLogger,
    configure_file_logs,
    log_event,
    reset_file_logs,
)
from ta_core.logging_config import configure_logging as _configure_logging

LOGGER_NAME = "backtesting_service.events"

__all__ = [
    "LOGGER_NAME",
    "JsonlLogger",
    "configure_file_logs",
    "configure_logging",
    "log_event",
    "reset_file_logs",
]


def configure_logging(level: str) -> None:
    _configure_logging(level, name=LOGGER_NAME)
