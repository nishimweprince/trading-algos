from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER_NAME = "mt5_signal_service.events"
logger = logging.getLogger(LOGGER_NAME)

_events_file_log: JsonlLogger | None = None


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


class JsonlLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        enriched = {"ts": datetime.now(UTC).isoformat(), **record}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(enriched, default=str, ensure_ascii=True))
            handle.write("\n")


class SignalFileLog:
    def __init__(self, path: Path) -> None:
        self._logger = JsonlLogger(path)

    def append(self, record: dict[str, Any]) -> None:
        self._logger.append(record)


def configure_logging(level: str) -> None:
    """Configure one JSON console handler without changing Uvicorn's access logs."""
    for handler in list(logger.handlers):
        if getattr(handler, "mt5_console_handler", False):
            logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.mt5_console_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonConsoleFormatter())
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper()))
    logger.propagate = False


def configure_file_logs(signals_log_path: Path) -> SignalFileLog:
    global _events_file_log
    _events_file_log = JsonlLogger(signals_log_path.parent / "events.jsonl")
    return SignalFileLog(signals_log_path)


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    console: bool = True,
    file: bool | None = None,
    **fields: Any,
) -> None:
    if console:
        logger.log(
            level,
            event,
            extra={"event_name": event, "event_fields": fields},
            exc_info=exc_info,
        )
    write_file = file if file is not None else not console
    if write_file and _events_file_log is not None:
        record: dict[str, Any] = {
            "event": event,
            "level": logging.getLevelName(level),
            **fields,
        }
        if exc_info:
            record["exc_info"] = True
        _events_file_log.append(record)
