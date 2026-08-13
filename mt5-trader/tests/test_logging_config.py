from __future__ import annotations

import json
import logging
from io import StringIO

from mt5_signal_service.logging_config import (
    JsonConsoleFormatter,
    configure_file_logs,
    log_event,
    logger,
)


def test_log_event_console_false_writes_file_only(tmp_path) -> None:
    signals_path = tmp_path / "signals.jsonl"
    configure_file_logs(signals_path)

    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonConsoleFormatter())
    logger.addHandler(handler)

    log_event("verbose_event", console=False, detail="hidden")
    log_event("visible_event", detail="shown")

    output = stream.getvalue()
    assert "verbose_event" not in output
    assert "visible_event" in output

    events_path = tmp_path / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "verbose_event"


def test_log_event_can_write_console_and_file(tmp_path) -> None:
    configure_file_logs(tmp_path / "signals.jsonl")

    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonConsoleFormatter())
    logger.addHandler(handler)

    log_event("failed_request", console=True, file=True, path="/v1/signals")

    assert "failed_request" in stream.getvalue()
    events_path = tmp_path / "events.jsonl"
    record = json.loads(events_path.read_text(encoding="utf-8").strip())
    assert record["event"] == "failed_request"
    assert record["path"] == "/v1/signals"


def test_signal_file_log_appends(tmp_path) -> None:
    signal_log = configure_file_logs(tmp_path / "signals.jsonl")
    signal_log.append({"signal_id": "abc", "state": "filled"})
    content = (tmp_path / "signals.jsonl").read_text(encoding="utf-8")
    record = json.loads(content.strip())
    assert record["signal_id"] == "abc"
    assert record["state"] == "filled"
    assert "ts" in record
