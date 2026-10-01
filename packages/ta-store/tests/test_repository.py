from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from ta_contracts import OperationAction, OperationState, TargetState

from ta_store import (
    ExecutionRepository,
    ImportedOperation,
    ImportedTarget,
    OperationConflictError,
)

OP = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


@pytest.fixture
def repo(tmp_path: Path) -> ExecutionRepository:
    repository = ExecutionRepository(tmp_path / "data" / "executions.sqlite3")
    repository.initialize()
    return repository


def reserve(
    repo: ExecutionRepository,
    *,
    operation_id: UUID = OP,
    payload_hash: str = "hash-a",
    accounts: tuple[str, ...] = ("forex-demo",),
):
    return repo.reserve(
        operation_id=operation_id,
        action=OperationAction.PLACE_ORDER,
        source="session_hedging",
        payload_hash=payload_hash,
        payload_json='{"any":"payload"}',
        targets=[(account, f"co-{account}") for account in accounts],
    )


# --- idempotency ------------------------------------------------------------


def test_first_reserve_creates_the_operation(repo: ExecutionRepository) -> None:
    response, created = reserve(repo)
    assert created is True
    assert response.state is OperationState.PENDING
    assert [target.account for target in response.targets] == ["forex-demo"]
    assert response.targets[0].state is TargetState.RESERVED


def test_replay_returns_the_stored_operation_without_recreating_it(
    repo: ExecutionRepository,
) -> None:
    reserve(repo)
    repo.update_target(OP, "forex-demo", TargetState.FILLED, order_id=42)
    response, created = reserve(repo)
    assert created is False, "a replayed operation must not be re-dispatched"
    assert response.targets[0].order_id == 42


def test_same_id_with_a_different_payload_is_a_conflict(repo: ExecutionRepository) -> None:
    reserve(repo)
    with pytest.raises(OperationConflictError):
        reserve(repo, payload_hash="hash-b")


def test_conflict_leaves_the_original_intact(repo: ExecutionRepository) -> None:
    reserve(repo)
    with pytest.raises(OperationConflictError):
        reserve(repo, payload_hash="hash-b")
    stored = repo.get(OP)
    assert stored is not None
    assert stored.state is OperationState.PENDING


# --- parent state rollup ----------------------------------------------------


def test_all_targets_filled_makes_the_operation_succeed(repo: ExecutionRepository) -> None:
    reserve(repo, accounts=("a", "b"))
    repo.update_target(OP, "a", TargetState.FILLED)
    response = repo.update_target(OP, "b", TargetState.FILLED)
    assert response.state is OperationState.SUCCEEDED


def test_all_targets_rejected_makes_the_operation_rejected(repo: ExecutionRepository) -> None:
    reserve(repo, accounts=("a", "b"))
    repo.update_target(OP, "a", TargetState.REJECTED)
    response = repo.update_target(OP, "b", TargetState.REJECTED)
    assert response.state is OperationState.REJECTED


def test_one_filled_one_rejected_is_a_partial_failure(repo: ExecutionRepository) -> None:
    """The reason a fan-out cannot be reported as a single boolean."""
    reserve(repo, accounts=("a", "b"))
    repo.update_target(OP, "a", TargetState.FILLED)
    response = repo.update_target(OP, "b", TargetState.REJECTED)
    assert response.state is OperationState.PARTIAL_FAILURE


def test_any_pending_target_keeps_the_operation_pending(repo: ExecutionRepository) -> None:
    reserve(repo, accounts=("a", "b"))
    response = repo.update_target(OP, "a", TargetState.FILLED)
    assert response.state is OperationState.PENDING


def test_partially_filled_counts_as_pending(repo: ExecutionRepository) -> None:
    reserve(repo, accounts=("a",))
    response = repo.update_target(OP, "a", TargetState.PARTIALLY_FILLED)
    assert response.state is OperationState.PENDING


def test_unknown_without_any_success_is_unknown(repo: ExecutionRepository) -> None:
    """A dispatched order with no confirmation must not be reported as failed."""
    reserve(repo, accounts=("a", "b"))
    repo.update_target(OP, "a", TargetState.UNKNOWN)
    response = repo.update_target(OP, "b", TargetState.REJECTED)
    assert response.state is OperationState.UNKNOWN


# --- target detail ----------------------------------------------------------


def test_update_target_records_broker_detail(repo: ExecutionRepository) -> None:
    reserve(repo)
    response = repo.update_target(
        OP,
        "forex-demo",
        TargetState.FILLED,
        order_id=7,
        position_id=9,
        executed_volume_lots=Decimal("0.10"),
        execution_price=Decimal("2001.25"),
    )
    target = response.targets[0]
    assert (target.order_id, target.position_id) == (7, 9)
    assert target.executed_volume_lots == Decimal("0.10")
    assert target.execution_price == Decimal("2001.25")


def test_update_target_rejects_an_unknown_column(repo: ExecutionRepository) -> None:
    """The column list is an allowlist because it is interpolated into SQL."""
    reserve(repo)
    with pytest.raises(ValueError, match="unknown target update column"):
        repo.update_target(OP, "forex-demo", TargetState.FILLED, injected="boom")


def test_targets_are_returned_ordered_by_account(repo: ExecutionRepository) -> None:
    reserve(repo, accounts=("zulu", "alpha"))
    response = repo.get(OP)
    assert response is not None
    assert [target.account for target in response.targets] == ["alpha", "zulu"]


# --- reconciliation helpers -------------------------------------------------


def test_find_by_client_order_id_locates_the_target(repo: ExecutionRepository) -> None:
    reserve(repo)
    assert repo.find_by_client_order_id("co-forex-demo") == (str(OP), "forex-demo")


def test_find_by_client_order_id_misses_cleanly(repo: ExecutionRepository) -> None:
    assert repo.find_by_client_order_id("nope") is None


def test_unresolved_lists_targets_needing_reconciliation(repo: ExecutionRepository) -> None:
    reserve(repo, accounts=("a", "b"))
    repo.update_target(OP, "a", TargetState.FILLED)
    repo.update_target(OP, "b", TargetState.DISPATCHED)
    unresolved = repo.unresolved()
    assert [row[1] for row in unresolved] == ["b"]


def test_unresolved_is_empty_once_everything_settles(repo: ExecutionRepository) -> None:
    reserve(repo)
    repo.update_target(OP, "forex-demo", TargetState.FILLED)
    assert repo.unresolved() == []


# --- durability -------------------------------------------------------------


def test_get_misses_cleanly(repo: ExecutionRepository) -> None:
    assert repo.get(uuid4()) is None


def test_database_file_is_owner_only(repo: ExecutionRepository) -> None:
    assert (repo.path.stat().st_mode & 0o777) == 0o600


def test_initialize_is_idempotent(repo: ExecutionRepository) -> None:
    reserve(repo)
    repo.initialize()
    assert repo.get(OP) is not None


def test_is_healthy_reports_true_for_a_live_database(repo: ExecutionRepository) -> None:
    assert repo.is_healthy() is True


def test_busy_timeout_is_set(repo: ExecutionRepository) -> None:
    """mt5-trader's copy lacked this; a concurrent writer failed instantly."""
    connection = sqlite3.connect(repo.path)
    try:
        with repo._connect() as configured:
            assert configured.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    finally:
        connection.close()


def test_append_event_records_the_broker_payload(repo: ExecutionRepository) -> None:
    reserve(repo)
    repo.append_event(
        account="forex-demo",
        event_type="ORDER_FILLED",
        payload={"orderId": 7},
        operation_id=OP,
    )
    with repo._connect() as connection:
        rows = connection.execute("SELECT * FROM execution_events").fetchall()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "ORDER_FILLED"


def test_timestamps_are_timezone_aware(repo: ExecutionRepository) -> None:
    response, _ = reserve(repo)
    assert response.created_at.tzinfo is not None
    assert response.created_at <= datetime.now(UTC)


# --- legacy-signal extensions ------------------------------------------------

_ORIGINAL_SCHEMA = """
CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY, action TEXT NOT NULL, source TEXT NOT NULL,
    payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE operation_targets (
    operation_id TEXT NOT NULL, account_alias TEXT NOT NULL, client_order_id TEXT,
    state TEXT NOT NULL, order_id INTEGER, position_id INTEGER, deal_id INTEGER,
    executed_volume_lots TEXT, execution_price TEXT, error_code TEXT,
    error_message TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, account_alias)
);
INSERT INTO operations VALUES (
    'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', 'place_order', 'x', 'h', '{}', 'succeeded',
    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
);
INSERT INTO operation_targets (operation_id, account_alias, state, updated_at) VALUES (
    'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', 'forex-demo', 'filled', '2026-01-01T00:00:00+00:00'
);
"""


def test_initialize_adds_new_columns_to_an_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(_ORIGINAL_SCHEMA)
    repository = ExecutionRepository(path)

    repository.initialize()

    record = repository.record(OP)
    assert record is not None
    assert record.operation.state is OperationState.SUCCEEDED
    assert record.broker_tag is None and record.response is None
    assert record.details == {"forex-demo": {}}


def test_details_are_merged_across_updates(repo: ExecutionRepository) -> None:
    reserve(repo)
    repo.update_target(OP, "forex-demo", TargetState.DISPATCHED, details={"request": {"a": 1}})
    repo.update_target(
        OP, "forex-demo", TargetState.FILLED, deal_id=7, details={"result": {"retcode": 1}}
    )

    record = repo.record(OP)
    assert record is not None
    assert record.details["forex-demo"] == {"request": {"a": 1}, "result": {"retcode": 1}}
    assert record.operation.targets[0].deal_id == 7


def test_update_without_details_keeps_the_stored_details(repo: ExecutionRepository) -> None:
    reserve(repo)
    repo.update_target(OP, "forex-demo", TargetState.DISPATCHED, details={"request": {"a": 1}})
    repo.update_target(OP, "forex-demo", TargetState.FILLED)

    record = repo.record(OP)
    assert record is not None
    assert record.details["forex-demo"] == {"request": {"a": 1}}


def test_set_outcome_and_broker_tag_round_trip(repo: ExecutionRepository) -> None:
    repo.reserve(
        operation_id=OP,
        action=OperationAction.PLACE_ORDER,
        source="ipda",
        payload_hash="h",
        payload_json="{}",
        targets=[("hfm", None)],
        broker_tag="ipda",
    )
    repo.set_outcome(OP, response={"outcome": "FILLED"})

    record = repo.record(OP)
    assert record is not None
    assert record.broker_tag == "ipda"
    assert record.response == {"outcome": "FILLED"}
    assert record.error is None


def test_record_misses_cleanly(repo: ExecutionRepository) -> None:
    assert repo.record(uuid4()) is None


def test_partially_filled_final_counts_as_success(repo: ExecutionRepository) -> None:
    reserve(repo)
    response = repo.update_target(OP, "forex-demo", TargetState.PARTIALLY_FILLED_FINAL)
    assert response.state is OperationState.SUCCEEDED


def test_unresolved_targets_are_scoped_to_the_given_accounts(repo: ExecutionRepository) -> None:
    other = uuid4()
    reserve(repo, accounts=("hfm",))
    reserve(repo, operation_id=other, accounts=("ftmo",))
    repo.update_target(OP, "hfm", TargetState.DISPATCHED, details={"request": {"a": 1}})

    unresolved = repo.unresolved_targets(["hfm"])

    assert [(item.operation_id, item.state) for item in unresolved] == [
        (str(OP), TargetState.DISPATCHED)
    ]
    assert unresolved[0].details == {"request": {"a": 1}}
    assert unresolved[0].created_at.tzinfo is not None
    assert repo.unresolved_targets([]) == []


def _imported(operation_id: str, state: TargetState = TargetState.FILLED) -> ImportedOperation:
    return ImportedOperation(
        operation_id=operation_id,
        action=OperationAction.PLACE_ORDER,
        source="ipda",
        payload_hash="h",
        payload_json="{}",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:05+00:00",
        target=ImportedTarget(
            account="hfm",
            state=state,
            deal_id=9,
            executed_volume_lots=Decimal("0.10"),
            details={"result": {"retcode": 10009}},
        ),
        broker_tag="ipda",
        response={"outcome": "FILLED"},
    )


def test_import_operations_copies_history_once(repo: ExecutionRepository) -> None:
    first = repo.import_operations("legacy", [_imported(str(OP))])
    again = repo.import_operations("legacy", [_imported(str(uuid4()))])

    assert first["imported"] == 1
    assert again["already_applied"] is True
    record = repo.record(OP)
    assert record is not None
    assert record.operation.state is OperationState.SUCCEEDED
    assert record.operation.targets[0].executed_volume_lots == Decimal("0.10")
    assert record.response == {"outcome": "FILLED"}
    assert record.details["hfm"] == {"result": {"retcode": 10009}}
    assert record.operation.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert repo.migration_applied("legacy") is not None


def test_import_never_overwrites_an_existing_operation(repo: ExecutionRepository) -> None:
    reserve(repo)
    summary = repo.import_operations("legacy", [_imported(str(OP))])

    assert summary == {**summary, "imported": 0, "skipped_existing": 1}
    record = repo.record(OP)
    assert record is not None
    assert record.operation.targets[0].state is TargetState.RESERVED


def test_import_dry_run_writes_nothing(repo: ExecutionRepository) -> None:
    summary = repo.import_operations("legacy", [_imported(str(OP))], dry_run=True)

    assert summary["dry_run"] is True and summary["imported"] == 1
    assert repo.get(OP) is None
    assert repo.migration_applied("legacy") is None
