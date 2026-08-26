from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

MODULE_PATH = Path(__file__).resolve().parents[1] / "cutover_audit.py"
SPEC = importlib.util.spec_from_file_location("cutover_audit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
cutover_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cutover_audit)


def test_launchd_snapshot_keeps_top_level_state(monkeypatch) -> None:
    output = """gui/501/com.execution-service.production = {
    state = running
    runs = 2
    pid = 6657
    arguments = {
        /repo/.venv/bin/execution-service
        --profile
        production
    }
    resource coalition = {
        state = active
    }
}
"""

    def fake_run(command: list[str]) -> CompletedProcess[str]:
        if command == ["id", "-u"]:
            return CompletedProcess(command, 0, "501\n", "")
        return CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(cutover_audit, "run", fake_run)

    result = cutover_audit.launchd_snapshot("com.execution-service.production")

    assert result["state"] == "running"
    assert result["runs"] == 2
    assert result["pid"] == 6657
    assert result["production_profile_argument"] is True


def test_read_jsonl_supports_both_timestamp_fields_and_bounds(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-08-26T05:07:50Z", "event": "before"}),
                json.dumps({"ts": "2026-08-26T05:07:51Z", "event": "first"}),
                json.dumps({"timestamp": "2026-08-26T05:07:52Z", "event": "second"}),
                "not-json",
                json.dumps({"event": "missing"}),
            ]
        ),
        encoding="utf-8",
    )

    rows, errors, missing = cutover_audit.read_jsonl(
        path,
        datetime(2026, 8, 26, 5, 7, 51, tzinfo=UTC),
        datetime(2026, 8, 26, 5, 7, 52, tzinfo=UTC),
        ("ts", "timestamp"),
    )

    assert [row["event"] for row in rows] == ["first", "second"]
    assert errors == 1
    assert missing == 1


def test_log_summary_classifies_failures_without_exposing_payloads() -> None:
    rows = [
        {"level": "ERROR", "event": "ctrader_connect_failed", "secret": "hidden"},
        {"level": "INFO", "event": "trade_operation_completed", "state": "failed"},
        {"level": "WARNING", "event": "service_error_response", "status_code": 503},
    ]

    result = cutover_audit.log_summary(rows, 0, 0)

    assert result["levels"] == {"ERROR": 1, "INFO": 1, "WARNING": 1}
    assert result["anomalies"] == {
        "ctrader_connect_failed": 1,
        "service_5xx_response": 1,
        "trade_operation_failed": 1,
    }
    assert "secret" not in json.dumps(result)


def test_stderr_summary_counts_suspicious_lines(tmp_path: Path) -> None:
    path = tmp_path / "production.error.log"
    path.write_text(
        "INFO: Started server process\nTraceback (most recent call last):\nERROR: bind failed\n",
        encoding="utf-8",
    )

    result = cutover_audit.stderr_summary(path)

    assert result == {"present": True, "lines": 3, "suspicious_lines": 2}


def test_ledger_snapshot_reports_duplicates_and_errors(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE operations (
            operation_id TEXT PRIMARY KEY,
            action TEXT,
            source TEXT,
            payload_hash TEXT,
            state TEXT
        );
        CREATE TABLE operation_targets (
            operation_id TEXT,
            state TEXT,
            error_code TEXT,
            error_message TEXT
        );
        CREATE TABLE execution_events (event_type TEXT);
        INSERT INTO operations VALUES ('one', 'place_order', 'ipda', 'same', 'succeeded');
        INSERT INTO operations VALUES ('two', 'place_order', 'ipda', 'same', 'failed');
        INSERT INTO operation_targets VALUES ('one', 'placed', NULL, NULL);
        INSERT INTO operation_targets VALUES ('two', 'failed', 'broker_error', 'redacted');
        INSERT INTO execution_events VALUES ('ProtoOAExecutionEvent');
        """
    )
    connection.commit()
    connection.close()

    result = cutover_audit.ledger_snapshot(path)

    assert result["integrity_check"] == "ok"
    assert result["operations_total"] == 2
    assert result["failed_operations"] == 1
    assert result["target_errors"] == 1
    assert result["duplicate_payload_groups"] == 1
    assert result["duplicate_operation_ids"] == 0


def test_acceptance_rejects_non_workspace_binary() -> None:
    report = {
        "window": {"elapsed_seconds": cutover_audit.WINDOW_SECONDS},
        "supervision": {
            "unified_launchd": {
                "loaded": True,
                "state": "running",
                "pid": 6657,
                "runs": 2,
                "program": "/unexpected/execution-service",
                "production_profile_argument": True,
                "working_directory": "/repo/services/execution-service",
                "stdout_path": "/repo/services/execution-service/logs/production.log",
                "stderr_path": "/repo/services/execution-service/logs/production.error.log",
            },
            "legacy_launchd": {"loaded": False},
            "port_8010_listeners": [{"pid": 6657}],
            "expected_paths": {
                "program": "/repo/.venv/bin/execution-service",
                "working_directory": "/repo/services/execution-service",
                "stdout_path": "/repo/services/execution-service/logs/production.log",
                "stderr_path": "/repo/services/execution-service/logs/production.error.log",
            },
        },
        "runtime": {
            "health_ready": {"http_status": 200, "status": "ready"},
            "health_trading_ready": {
                "http_status": 200,
                "status": "ready",
                "database_healthy": True,
            },
            "reconnects_by_environment": {"demo": 0, "live": 0},
            "accounts": {
                "count": 3,
                "by_environment": {"demo": 2, "live": 1},
                "unconfigured_authorized_accounts": 0,
                "unavailable_authorized_accounts": 1,
                "all_connected": True,
                "all_reconciled": True,
                "all_full_access": True,
                "all_available_for_trading": True,
                "all_order_entry_enabled": True,
                "all_position_close_enabled": True,
            },
            "xauusd_tick": {"http_status": 200, "age_seconds": 1},
        },
        "logs": {
            "durable": {
                "parse_errors": 0,
                "missing_timestamps": 0,
                "levels": {},
                "anomalies": {},
            },
            "console": {"parse_errors": 0, "missing_timestamps": 0, "levels": {}},
            "stderr": {"present": True, "suspicious_lines": 0},
        },
        "ledger": {
            "integrity_check": "ok",
            "failed_operations": 0,
            "target_errors": 0,
            "duplicate_operation_ids": 0,
            "duplicate_payload_groups": 0,
        },
    }

    result = cutover_audit.acceptance(report, expected_pid=6657, expected_runs=2)

    assert result["checks"]["workspace_binary_and_profile"] is False
    assert result["passed"] is False
