"""JSON console logging."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

LOGGER_NAME = "session_hedging.events"
logger = logging.getLogger(LOGGER_NAME)


class JsonConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event_name", record.getMessage()),
        }
        fields = getattr(record, "event_fields", None)
        if fields:
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "session_hedging_console_handler", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.session_hedging_console_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonConsoleFormatter())
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper()))
    logger.propagate = False


def log_event(
    event: str, *, level: int = logging.INFO, exc_info: bool = False, **fields: Any
) -> None:
    logger.log(
        level,
        event,
        extra={"event_name": event, "event_fields": fields},
        exc_info=exc_info,
    )
