"""Structured logging for execution-service, bound to ta-core's implementation.

This module exists so the ~8 call sites keep importing `log_event` from a
service-local name while the implementation lives in one place. The only thing
it adds is the logger name, which is the sole thing that differed between the
two hand-written copies this replaces.

NOTE the logger name changed from `ctrader_markets.events` to
`execution_service.events` along with the service. Anything grepping the console
JSON for the old name needs updating.
"""

from __future__ import annotations

from ta_core.logging_config import (
    JsonlLogger,
    configure_file_logs,
    log_event,
    reset_file_logs,
)
from ta_core.logging_config import configure_logging as _configure_logging

LOGGER_NAME = "execution_service.events"

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
