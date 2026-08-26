"""Structured logging: a JSON console handler plus a durable JSONL sink.

Taken from ctrader-markets, which had hardening mt5-trader's copy lacked: the
append retries once through a directory that was rotated or deleted underneath
a running service, and the file is kept at mode 0600.

The logger name is a parameter rather than a module constant, because it used to
be the only thing that differed between the two copies.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_LOGGER_NAME = "ta.events"

# Marks the handler this module owns, so configure_logging replaces its own
# handler on reconfiguration without touching Uvicorn's access-log handlers.
_HANDLER_MARKER = "_ta_console_handler"

_logger_name = DEFAULT_LOGGER_NAME
_events_file_log: JsonlLogger | None = None


def get_logger() -> logging.Logger:
    return logging.getLogger(_logger_name)


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
        if self.path.exists():
            os.chmod(self.path, 0o600)
        self._failed = False

    def append(self, record: dict[str, Any]) -> None:
        """Write one event, and never raise.

        log_event is called from the protocol reader loop, which must not raise;
        a log directory that was rotated or deleted underneath a running service
        must not be able to take the tick stream down. One retry covers the
        common case of the directory having been removed; after that the sink
        goes quiet and only the console handler carries events.
        """
        enriched = {"ts": datetime.now(UTC).isoformat(), **record}
        line = json.dumps(enriched, default=str, ensure_ascii=True) + "\n"
        for attempt in (0, 1):
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                os.chmod(self.path, 0o600)
                self._failed = False
                return
            except OSError:
                if attempt == 0:
                    try:
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        break
        if not self._failed:
            self._failed = True
            get_logger().warning(
                "events_log_unwritable", extra={"event_name": "events_log_unwritable"}
            )


def configure_logging(level: str, *, name: str = DEFAULT_LOGGER_NAME) -> None:
    """Configure one JSON console handler without changing Uvicorn's access logs."""
    global _logger_name
    _logger_name = name
    logger = get_logger()
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(JsonConsoleFormatter())
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper()))
    logger.propagate = False


def configure_file_logs(events_log_path: Path) -> JsonlLogger:
    """Route events to an append-only JSONL file, and return the sink.

    The sink is returned so a service that also keeps a second, domain-specific
    JSONL (mt5-trader's signals log) can build one with the same writer instead
    of reimplementing it.
    """
    global _events_file_log
    _events_file_log = JsonlLogger(events_log_path)
    return _events_file_log


def reset_file_logs() -> None:
    """Drop the configured sink. For tests; a service calls this never."""
    global _events_file_log
    _events_file_log = None


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    console: bool = True,
    file: bool = True,
    **fields: Any,
) -> None:
    """Record one structured event.

    Every event reaches the JSONL file when one is configured; `console=False`
    only suppresses the stdout copy, for events too frequent or too noisy to
    interleave with the operator's log. Routing to one sink *or* the other would
    leave the file — the durable record — missing every important event, since
    connect, disconnect and token-refresh are all console events.

    NOTE: mt5-trader's copy defaulted the file sink to `not console`, so its
    events.jsonl only ever received the console-suppressed events. That is the
    behaviour this deliberately does not keep; after migration its events.jsonl
    records connect/disconnect too.
    """
    if console:
        get_logger().log(
            level,
            event,
            extra={"event_name": event, "event_fields": fields},
            exc_info=exc_info,
        )
    if file and _events_file_log is not None:
        record: dict[str, Any] = {
            "event": event,
            "level": logging.getLevelName(level),
            **fields,
        }
        if exc_info:
            record["exc_info"] = True
        _events_file_log.append(record)
