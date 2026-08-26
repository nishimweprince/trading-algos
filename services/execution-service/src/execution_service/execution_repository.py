from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from .models import (
    OperationAction,
    OperationResponse,
    OperationState,
    TargetResult,
    TargetState,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class OperationConflictError(Exception):
    pass


class ExecutionRepository:
    """Durable idempotency and execution-event ledger.

    A process-local lock plus SQLite IMMEDIATE transactions make reservation
    safe across concurrent HTTP requests and across accidental second workers.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operation_targets (
                    operation_id TEXT NOT NULL
                        REFERENCES operations(operation_id) ON DELETE CASCADE,
                    account_alias TEXT NOT NULL,
                    client_order_id TEXT,
                    state TEXT NOT NULL,
                    order_id INTEGER,
                    position_id INTEGER,
                    deal_id INTEGER,
                    executed_volume_lots TEXT,
                    execution_price TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (operation_id, account_alias)
                );

                CREATE INDEX IF NOT EXISTS operation_targets_client_order
                    ON operation_targets(client_order_id);
                CREATE INDEX IF NOT EXISTS operation_targets_state
                    ON operation_targets(state);

                CREATE TABLE IF NOT EXISTS execution_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT,
                    account_alias TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    broker_payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
        os.chmod(self.path, 0o600)

    def is_healthy(self) -> bool:
        try:
            with self._lock, self._connect() as connection:
                return bool(connection.execute("SELECT 1").fetchone()[0])
        except sqlite3.Error:
            return False

    def reserve(
        self,
        *,
        operation_id: UUID,
        action: OperationAction,
        source: str,
        payload_hash: str,
        payload_json: str,
        targets: list[tuple[str, str | None]],
    ) -> tuple[OperationResponse, bool]:
        oid = str(operation_id)
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_hash FROM operations WHERE operation_id = ?", (oid,)
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise OperationConflictError(
                        "operation_id was already used with a different request payload"
                    )
                connection.commit()
                response = self._get_with_connection(connection, oid)
                assert response is not None
                return response, False

            connection.execute(
                """
                INSERT INTO operations (
                    operation_id, action, source, payload_hash, payload_json,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    oid,
                    action.value,
                    source,
                    payload_hash,
                    payload_json,
                    OperationState.PENDING.value,
                    timestamp,
                    timestamp,
                ),
            )
            connection.executemany(
                """
                INSERT INTO operation_targets (
                    operation_id, account_alias, client_order_id, state, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (oid, account, client_order_id, TargetState.RESERVED.value, timestamp)
                    for account, client_order_id in targets
                ],
            )
            connection.commit()
            response = self._get_with_connection(connection, oid)
            assert response is not None
            return response, True

    def get(self, operation_id: UUID | str) -> OperationResponse | None:
        with self._lock, self._connect() as connection:
            return self._get_with_connection(connection, str(operation_id))

    def update_target(
        self,
        operation_id: UUID | str,
        account: str,
        state: TargetState,
        **values: Any,
    ) -> OperationResponse:
        allowed = {
            "order_id",
            "position_id",
            "deal_id",
            "executed_volume_lots",
            "execution_price",
            "error_code",
            "error_message",
        }
        if not set(values).issubset(allowed):
            raise ValueError("unknown target update column")
        timestamp = _now()
        assignments = ["state = ?", "updated_at = ?"]
        parameters: list[Any] = [state.value, timestamp]
        for column, value in values.items():
            assignments.append(f"{column} = ?")
            parameters.append(None if value is None else str(value))
        parameters.extend((str(operation_id), account))
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE operation_targets SET {', '.join(assignments)} "  # noqa: S608
                "WHERE operation_id = ? AND account_alias = ?",
                parameters,
            )
            self._refresh_parent(connection, str(operation_id), timestamp)
            response = self._get_with_connection(connection, str(operation_id))
            assert response is not None
            return response

    def append_event(
        self,
        *,
        account: str,
        event_type: str,
        payload: dict[str, Any],
        operation_id: UUID | str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_events (
                    operation_id, account_alias, event_type, broker_payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    None if operation_id is None else str(operation_id),
                    account,
                    event_type,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    _now(),
                ),
            )

    def find_by_client_order_id(self, client_order_id: str) -> tuple[str, str] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT operation_id, account_alias FROM operation_targets
                WHERE client_order_id = ?
                """,
                (client_order_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["operation_id"]), str(row["account_alias"])

    def unresolved(self) -> list[tuple[str, str, str | None]]:
        states = (
            TargetState.RESERVED.value,
            TargetState.DISPATCHED.value,
            TargetState.ACCEPTED.value,
            TargetState.UNKNOWN.value,
        )
        placeholders = ",".join("?" for _ in states)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT operation_id, account_alias, client_order_id FROM operation_targets "  # noqa: S608
                f"WHERE state IN ({placeholders})",
                states,
            ).fetchall()
        return [
            (str(row["operation_id"]), str(row["account_alias"]), row["client_order_id"])
            for row in rows
        ]

    @staticmethod
    def _refresh_parent(connection: sqlite3.Connection, operation_id: str, timestamp: str) -> None:
        states = [
            TargetState(row["state"])
            for row in connection.execute(
                "SELECT state FROM operation_targets WHERE operation_id = ?", (operation_id,)
            ).fetchall()
        ]
        successful = {
            TargetState.PLACED,
            TargetState.FILLED,
            TargetState.AMENDED,
            TargetState.CANCELLED,
            TargetState.CLOSED,
        }
        pending = {
            TargetState.RESERVED,
            TargetState.DISPATCHED,
            TargetState.ACCEPTED,
            TargetState.PARTIALLY_FILLED,
        }
        if any(state in pending for state in states):
            parent = OperationState.PENDING
        elif all(state in successful for state in states):
            parent = OperationState.SUCCEEDED
        elif all(state is TargetState.REJECTED for state in states):
            parent = OperationState.REJECTED
        elif any(state is TargetState.UNKNOWN for state in states) and not any(
            state in successful for state in states
        ):
            parent = OperationState.UNKNOWN
        else:
            parent = OperationState.PARTIAL_FAILURE
        connection.execute(
            "UPDATE operations SET state = ?, updated_at = ? WHERE operation_id = ?",
            (parent.value, timestamp, operation_id),
        )

    @staticmethod
    def _get_with_connection(
        connection: sqlite3.Connection, operation_id: str
    ) -> OperationResponse | None:
        operation = connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if operation is None:
            return None
        rows = connection.execute(
            """
            SELECT * FROM operation_targets WHERE operation_id = ?
            ORDER BY account_alias
            """,
            (operation_id,),
        ).fetchall()
        return OperationResponse(
            operation_id=UUID(operation_id),
            action=OperationAction(operation["action"]),
            state=OperationState(operation["state"]),
            targets=[
                TargetResult(
                    account=row["account_alias"],
                    state=TargetState(row["state"]),
                    order_id=row["order_id"],
                    position_id=row["position_id"],
                    deal_id=row["deal_id"],
                    executed_volume_lots=row["executed_volume_lots"],
                    execution_price=row["execution_price"],
                    error_code=row["error_code"],
                    error_message=row["error_message"],
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
                for row in rows
            ],
            created_at=datetime.fromisoformat(operation["created_at"]),
            updated_at=datetime.fromisoformat(operation["updated_at"]),
        )
