"""JSON console logging plus append-only JSONL run logs.

Console formatter adapted from mt5-trader/logging_config.py; the JSONL run logs mirror
telegram-metatrader/jsonl.py so signals, executions and errors are machine-readable.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER_NAME = "lux_algo.events"
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
        if getattr(handler, "lux_console_handler", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.lux_console_handler = True  # type: ignore[attr-defined]
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


class JsonlLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        enriched = {"ts": datetime.now(UTC).isoformat(), **record}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(enriched, default=str, ensure_ascii=True))
            handle.write("\n")


class RuntimeLogs:
    def __init__(self, logs_dir: Path) -> None:
        self.signals = JsonlLogger(logs_dir / "signals.jsonl")
        self.executions = JsonlLogger(logs_dir / "executions.jsonl")
        self.errors = JsonlLogger(logs_dir / "errors.jsonl")
