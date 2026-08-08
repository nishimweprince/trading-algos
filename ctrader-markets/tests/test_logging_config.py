"""Tests for the structured event log.

Nothing referenced JsonlLogger, configure_file_logs or log_event before, so the
file sink never ran in the suite — and it was routing console events away from
the file entirely, leaving events.jsonl missing every connect, disconnect and
token refresh the service produced.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

import logging_config
from logging_config import JsonlLogger, configure_file_logs, configure_logging, log_event


@pytest.fixture(autouse=True)
def _reset_file_sink() -> object:
    """log_event writes through module state; leaking it breaks other modules."""
    original = logging_config._events_file_log
    yield
    logging_config._events_file_log = original


def _read(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_console_events_also_reach_the_file(tmp_path: Path) -> None:
    """The operationally important events — ctrader_connected, connect_failed,
    access_token_refreshed — are all console events. Routing to one sink or the
    other left the durable record missing every one of them."""
    path = tmp_path / "events.jsonl"
    configure_file_logs(path)

    log_event("ctrader_connected", profile="forex")

    records = _read(path)
    assert len(records) == 1
    assert records[0]["event"] == "ctrader_connected"
    assert records[0]["profile"] == "forex"


def test_console_false_events_reach_the_file_but_not_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "events.jsonl"
    configure_logging("INFO")
    configure_file_logs(path)
    capsys.readouterr()

    log_event("stream_subscriber_lagging", console=False, queue_size=256)

    assert capsys.readouterr().out == ""
    assert _read(path)[0]["event"] == "stream_subscriber_lagging"


def test_level_is_recorded_by_name(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    configure_file_logs(path)

    log_event("ctrader_connect_failed", level=logging.ERROR, reason="TimeoutError")

    assert _read(path)[0]["level"] == "ERROR"


def test_events_are_appended_not_truncated(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    configure_file_logs(path)

    log_event("first")
    configure_file_logs(path)
    log_event("second")

    assert [record["event"] for record in _read(path)] == ["first", "second"]


def test_every_record_is_timestamped(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    configure_file_logs(path)

    log_event("ctrader_connected")

    assert "ts" in _read(path)[0]


def test_the_log_directory_is_created(tmp_path: Path) -> None:
    """launchd creates the log file but not intermediate directories, so the
    service has to make its own."""
    path = tmp_path / "nested" / "deeper" / "events.jsonl"

    JsonlLogger(path).append({"event": "ok"})

    assert path.is_file()


def test_events_are_dropped_silently_when_no_file_is_configured() -> None:
    """The one-shot CLI path configures logging lazily; a missing sink must not
    take the process down."""
    logging_config._events_file_log = None

    log_event("ctrader_connected", console=False)


def test_console_output_is_json_with_the_event_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    capsys.readouterr()

    log_event("service_starting", profile="forex", port=8010)

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "service_starting"
    assert payload["profile"] == "forex"
    assert payload["port"] == 8010
    assert payload["level"] == "INFO"


def test_a_deleted_log_directory_is_recreated(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "events.jsonl"
    sink = JsonlLogger(path)
    sink.append({"event": "before"})
    shutil.rmtree(path.parent)

    sink.append({"event": "after"})

    assert [record["event"] for record in _read(path)] == ["after"]


def test_an_unwritable_sink_never_raises(tmp_path: Path) -> None:
    """log_event runs inside the protocol reader loop, which must not raise. A
    log path that cannot be written must not be able to stop the tick stream."""
    path = tmp_path / "logs" / "events.jsonl"
    sink = JsonlLogger(path)
    shutil.rmtree(path.parent)
    path.parent.write_text("not a directory", encoding="utf-8")

    sink.append({"event": "swallowed"})
    sink.append({"event": "still swallowed"})


def test_unserialisable_fields_do_not_raise(tmp_path: Path) -> None:
    """A logging call must never be the thing that kills the reader loop."""
    path = tmp_path / "events.jsonl"
    configure_file_logs(path)

    log_event("symbol_catalog_loaded", when=object())

    assert _read(path)[0]["event"] == "symbol_catalog_loaded"
