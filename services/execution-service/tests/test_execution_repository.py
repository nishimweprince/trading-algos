from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from ta_contracts import OperationAction, OperationState, TargetState
from ta_store import ExecutionRepository, OperationConflictError


def test_repository_reservation_is_idempotent_and_hash_protected(tmp_path: Path) -> None:
    repository = ExecutionRepository(tmp_path / "executions.sqlite3")
    repository.initialize()
    operation_id = uuid4()

    first, created = repository.reserve(
        operation_id=operation_id,
        action=OperationAction.PLACE_ORDER,
        source="strategy_a",
        payload_hash="same-hash",
        payload_json="{}",
        targets=[("forex_demo", "client-order-id")],
    )
    replay, replay_created = repository.reserve(
        operation_id=operation_id,
        action=OperationAction.PLACE_ORDER,
        source="strategy_a",
        payload_hash="same-hash",
        payload_json="{}",
        targets=[("forex_demo", "client-order-id")],
    )

    assert created is True
    assert replay_created is False
    assert replay == first
    with pytest.raises(OperationConflictError):
        repository.reserve(
            operation_id=operation_id,
            action=OperationAction.PLACE_ORDER,
            source="strategy_a",
            payload_hash="different-hash",
            payload_json='{"changed":true}',
            targets=[("forex_demo", "client-order-id")],
        )


def test_repository_computes_partial_failure_from_target_outcomes(tmp_path: Path) -> None:
    repository = ExecutionRepository(tmp_path / "executions.sqlite3")
    repository.initialize()
    operation_id = uuid4()
    repository.reserve(
        operation_id=operation_id,
        action=OperationAction.PLACE_ORDER,
        source="strategy_a",
        payload_hash="hash",
        payload_json="{}",
        targets=[("one", "one-id"), ("two", "two-id")],
    )

    repository.update_target(operation_id, "one", TargetState.FILLED, order_id=11)
    response = repository.update_target(
        operation_id,
        "two",
        TargetState.REJECTED,
        error_code="NOT_ENOUGH_MONEY",
    )

    assert response.state is OperationState.PARTIAL_FAILURE
    assert {target.account: target.state for target in response.targets} == {
        "one": TargetState.FILLED,
        "two": TargetState.REJECTED,
    }
