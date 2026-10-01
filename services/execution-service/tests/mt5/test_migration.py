from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from ta_contracts import OperationState, SignalRequest, SignalState, TargetState
from ta_core import ServiceError
from ta_store import ExecutionRepository

from execution_service.api import create_app
from execution_service.main import _migrate as migrate_cli
from execution_service.main import parse_args
from execution_service.migration import migrate_legacy_ledger, migration_name

# The pre-unification signals.db schema, exactly as mt5-trader created it.
LEGACY_SCHEMA = """
CREATE TABLE signals (
    signal_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    broker_tag TEXT NOT NULL,
    request_json TEXT,
    check_json TEXT,
    result_json TEXT,
    response_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _row(
    signal: SignalRequest,
    state: SignalState,
    *,
    response: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    request: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    payload = signal.canonical_json()
    return (
        str(signal.signal_id),
        hashlib.sha256(payload.encode()).hexdigest(),
        payload,
        state.value,
        signal.source,
        None if request is None else json.dumps(request),
        None,
        None,
        None if response is None else json.dumps(response),
        None if error is None else json.dumps(error),
        "2026-09-01T10:00:00+00:00",
        "2026-09-01T10:00:01+00:00",
    )


def _legacy_db(path: Path, rows: list[tuple[Any, ...]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(LEGACY_SCHEMA)
        connection.executemany(
            "INSERT INTO signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
    connection.close()
    return path


def _filled(signal: SignalRequest) -> dict[str, Any]:
    return {
        "signal_id": str(signal.signal_id),
        "outcome": "filled",
        "order_ticket": 1001,
        "deal_ticket": 2001,
        "executed_volume": "0.1",
        "execution_price": "1.1002",
        "broker_retcode": 10009,
        "broker_comment": "Request executed",
        "processed_at": "2026-09-01T10:00:01Z",
        "reconciled": False,
    }


@pytest.fixture
def filled_signal(signal_factory) -> SignalRequest:
    return signal_factory()


def test_history_is_imported_with_its_exact_bodies(
    settings, repository, signal_factory, filled_signal
) -> None:
    rejected = signal_factory()
    error = {"status_code": 422, "code": "stale_signal", "message": "too old"}
    _legacy_db(
        settings.database_path,
        [
            _row(filled_signal, SignalState.FILLED, response=_filled(filled_signal)),
            _row(rejected, SignalState.REJECTED, error=error),
        ],
    )

    summary = migrate_legacy_ledger(settings.database_path, repository, "mt5")

    assert summary["imported"] == 2
    record = repository.record(filled_signal.signal_id)
    assert record is not None
    assert record.response == _filled(filled_signal)
    assert record.broker_tag == "trading_central"
    assert record.operation.state is OperationState.SUCCEEDED
    target = record.operation.targets[0]
    assert (target.account, target.order_id, target.deal_id) == ("mt5", 1001, 2001)
    rejected_record = repository.record(rejected.signal_id)
    assert rejected_record is not None and rejected_record.error == error
    assert rejected_record.operation.targets[0].error_code == "stale_signal"


def test_rerun_is_a_no_op(settings, repository, filled_signal) -> None:
    _legacy_db(
        settings.database_path,
        [_row(filled_signal, SignalState.FILLED, response=_filled(filled_signal))],
    )
    migrate_legacy_ledger(settings.database_path, repository, "mt5")

    again = migrate_legacy_ledger(settings.database_path, repository, "mt5")

    assert again["already_applied"] is True
    assert repository.migration_applied(migration_name("mt5")) is not None


def test_missing_source_records_nothing(settings, repository) -> None:
    summary = migrate_legacy_ledger(settings.database_path, repository, "mt5")

    assert summary["missing"] is True
    assert repository.migration_applied(migration_name("mt5")) is None


def test_dry_run_writes_nothing(settings, repository, filled_signal) -> None:
    _legacy_db(
        settings.database_path,
        [_row(filled_signal, SignalState.FILLED, response=_filled(filled_signal))],
    )

    summary = migrate_legacy_ledger(settings.database_path, repository, "mt5", dry_run=True)

    assert summary["imported"] == 1 and summary["dry_run"] is True
    assert repository.get(filled_signal.signal_id) is None


def test_the_source_is_never_written(settings, repository, filled_signal) -> None:
    source = _legacy_db(
        settings.database_path,
        [_row(filled_signal, SignalState.FILLED, response=_filled(filled_signal))],
    )
    before = source.read_bytes()

    migrate_legacy_ledger(source, repository, "mt5")

    assert source.read_bytes() == before


async def test_migrated_signal_replays_without_reaching_the_terminal(
    settings, adapter, repository, service, filled_signal
) -> None:
    _legacy_db(
        settings.database_path,
        [_row(filled_signal, SignalState.FILLED, response=_filled(filled_signal))],
    )
    migrate_legacy_ledger(settings.database_path, repository, service.account)

    replay = await service.execute(filled_signal)

    assert replay.order_ticket == 1001
    assert adapter.send_requests == []
    status = await service.status(filled_signal.signal_id)
    assert status.state is SignalState.FILLED


async def test_migrated_signal_with_a_changed_payload_still_conflicts(
    settings, adapter, repository, service, filled_signal
) -> None:
    _legacy_db(
        settings.database_path,
        [_row(filled_signal, SignalState.FILLED, response=_filled(filled_signal))],
    )
    migrate_legacy_ledger(settings.database_path, repository, service.account)

    with pytest.raises(ServiceError) as raised:
        await service.execute(filled_signal.model_copy(update={"symbol": "GBPUSD"}))

    assert raised.value.status_code == 409
    assert raised.value.code == "idempotency_conflict"
    assert raised.value.details == {"state": "filled"}
    assert adapter.send_requests == []


def test_interrupted_legacy_signal_is_reconciled_after_import(
    settings, adapter, repository, service, signal_factory
) -> None:
    executing = signal_factory()
    received = signal_factory()
    _legacy_db(
        settings.database_path,
        [
            _row(
                executing,
                SignalState.EXECUTING,
                request={"symbol": "EURUSD", "volume": 0.1},
            ),
            _row(received, SignalState.RECEIVED),
        ],
    )
    adapter.deals = [
        {"ticket": 9, "order": 8, "symbol": "EURUSD", "volume": 0.1, "comment": "trading_central"}
    ]
    migrate_legacy_ledger(settings.database_path, repository, service.account)

    service.reconcile_startup()

    assert service.get(executing.signal_id).state is SignalState.FILLED
    stored = service.get(received.signal_id)
    assert stored.state is SignalState.REJECTED
    assert stored.error["code"] == "restart_before_execution"


def test_startup_imports_before_serving(settings, adapter, filled_signal) -> None:
    _legacy_db(
        settings.database_path,
        [_row(filled_signal, SignalState.FILLED, response=_filled(filled_signal))],
    )
    app = create_app(settings, mt5_adapter=adapter)
    with TestClient(app) as client:
        response = client.post(
            "/v1/signals",
            json=json.loads(filled_signal.model_dump_json()),
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        )

    assert response.status_code == 200
    assert response.json()["order_ticket"] == 1001
    assert adapter.send_requests == []


def test_cli_imports_once_and_reports(settings, filled_signal, capsys) -> None:
    _legacy_db(
        settings.database_path,
        [_row(filled_signal, SignalState.FILLED, response=_filled(filled_signal))],
    )

    dry = migrate_cli(parse_args(["--migrate-legacy-ledger", "--dry-run"]), settings)
    dry_report = json.loads(capsys.readouterr().out)
    real = migrate_cli(parse_args(["--migrate-legacy-ledger"]), settings)
    real_report = json.loads(capsys.readouterr().out)

    assert (dry, real) == (0, 0)
    assert dry_report["signals"]["dry_run"] is True
    assert real_report["signals"]["imported"] == 1
    assert real_report["oco"]["missing"] is True
    ledger = ExecutionRepository(settings.execution_database_path)
    target = ledger.get(filled_signal.signal_id).targets[0]
    assert target.state is TargetState.FILLED


def test_cli_reports_a_missing_source(settings, tmp_path) -> None:
    args = parse_args(["--migrate-legacy-ledger", str(tmp_path / "absent.db")])

    assert migrate_cli(args, settings) == 1


def test_a_new_signal_id_is_unaffected(settings, repository, service, signal_factory) -> None:
    _legacy_db(settings.database_path, [])
    summary = migrate_legacy_ledger(settings.database_path, repository, service.account)

    assert summary["imported"] == 0
    assert repository.get(uuid4()) is None
