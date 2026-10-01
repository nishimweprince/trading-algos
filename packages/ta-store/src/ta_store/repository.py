"""Durable idempotency and execution-event ledger.

Extracted from ctrader-markets/src/execution_repository.py, and since the
execution unification also the home of MT5 signals: the legacy `/v1/signals`
contract stores its exact response or error body (`response_json`,
`error_json`), its broker comment (`broker_tag`) and the broker's own
request/preflight/result payloads per target (`details_json`), so replays and
status reads stay byte-identical. Every schema change here is additive: an
existing database gains nullable columns in place and nothing is rewritten.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from ta_contracts import (
    OperationAction,
    OperationResponse,
    OperationState,
    TargetResult,
    TargetState,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dump(value: Any | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def _text(value: Any | None) -> str | None:
    return None if value is None else str(value)


def _load(value: str | None) -> Any | None:
    return None if value is None else json.loads(value)


# Nullable columns added after the original schema; initialize() adds any that
# an existing database lacks.
_ADDED_COLUMNS = (
    ("operations", "broker_tag"),
    ("operations", "response_json"),
    ("operations", "error_json"),
    ("operation_targets", "details_json"),
)

_UNRESOLVED = (
    TargetState.RESERVED,
    TargetState.DISPATCHED,
    TargetState.ACCEPTED,
    TargetState.UNKNOWN,
)


@dataclass(frozen=True)
class OperationRecord:
    """Everything stored for one operation, including the legacy-signal extras."""

    operation: OperationResponse
    payload_hash: str
    payload_json: str
    broker_tag: str | None
    response: dict[str, Any] | None
    error: dict[str, Any] | None
    details: dict[str, dict[str, Any]]
    """details_json per target account."""


@dataclass(frozen=True)
class UnresolvedTarget:
    operation_id: str
    account: str
    client_order_id: str | None
    state: TargetState
    broker_tag: str | None
    details: dict[str, Any] | None
    created_at: datetime


@dataclass(frozen=True)
class ImportedTarget:
    account: str
    state: TargetState
    client_order_id: str | None = None
    order_id: int | None = None
    position_id: int | None = None
    deal_id: int | None = None
    executed_volume_lots: Any = None
    execution_price: Any = None
    error_code: str | None = None
    error_message: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class ImportedOperation:
    """One historical operation for `import_operations`, timestamps preserved."""

    operation_id: str
    action: OperationAction
    source: str
    payload_hash: str
    payload_json: str
    created_at: str
    updated_at: str
    target: ImportedTarget
    broker_tag: str | None = None
    response: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


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

                CREATE TABLE IF NOT EXISTS ledger_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );
                """
            )
            for table, column in _ADDED_COLUMNS:
                existing = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
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
        broker_tag: str | None = None,
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
                    state, created_at, updated_at, broker_tag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    broker_tag,
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
        *,
        details: dict[str, Any] | None = None,
        **values: Any,
    ) -> OperationResponse:
        """Move one target to `state`. `details`, when given, is merged into the
        target's stored broker payloads (request, preflight, result)."""
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
            if details:
                row = connection.execute(
                    "SELECT details_json FROM operation_targets "
                    "WHERE operation_id = ? AND account_alias = ?",
                    (str(operation_id), account),
                ).fetchone()
                merged = {**((_load(row["details_json"]) if row else None) or {}), **details}
                assignments.append("details_json = ?")
                parameters.insert(-2, _dump(merged))
            connection.execute(
                f"UPDATE operation_targets SET {', '.join(assignments)} "  # noqa: S608
                "WHERE operation_id = ? AND account_alias = ?",
                parameters,
            )
            self._refresh_parent(connection, str(operation_id), timestamp)
            response = self._get_with_connection(connection, str(operation_id))
            assert response is not None
            return response

    def set_outcome(
        self,
        operation_id: UUID | str,
        *,
        response: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Store the exact body a legacy caller received, for byte-identical replay."""
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE operations SET response_json = ?, error_json = ?, updated_at = ? "
                "WHERE operation_id = ?",
                (_dump(response), _dump(error), _now(), str(operation_id)),
            )

    def record(self, operation_id: UUID | str) -> OperationRecord | None:
        with self._lock, self._connect() as connection:
            operation = self._get_with_connection(connection, str(operation_id))
            if operation is None:
                return None
            row = connection.execute(
                "SELECT payload_hash, payload_json, broker_tag, response_json, error_json "
                "FROM operations WHERE operation_id = ?",
                (str(operation_id),),
            ).fetchone()
            details = {
                str(target["account_alias"]): _load(target["details_json"]) or {}
                for target in connection.execute(
                    "SELECT account_alias, details_json FROM operation_targets "
                    "WHERE operation_id = ?",
                    (str(operation_id),),
                ).fetchall()
            }
        return OperationRecord(
            operation=operation,
            payload_hash=str(row["payload_hash"]),
            payload_json=str(row["payload_json"]),
            broker_tag=row["broker_tag"],
            response=_load(row["response_json"]),
            error=_load(row["error_json"]),
            details=details,
        )

    def unresolved_targets(self, accounts: Iterable[str]) -> list[UnresolvedTarget]:
        """Targets whose broker outcome is not settled, for these accounts only."""
        aliases = tuple(accounts)
        if not aliases:
            return []
        states = tuple(state.value for state in _UNRESOLVED)
        query = (
            "SELECT t.operation_id, t.account_alias, t.client_order_id, t.state, "
            "t.details_json, o.broker_tag, o.created_at "
            "FROM operation_targets t JOIN operations o USING (operation_id) "
            f"WHERE t.state IN ({','.join('?' for _ in states)}) "
            f"AND t.account_alias IN ({','.join('?' for _ in aliases)}) "
            "ORDER BY o.created_at"
        )
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, (*states, *aliases)).fetchall()
        return [
            UnresolvedTarget(
                operation_id=str(row["operation_id"]),
                account=str(row["account_alias"]),
                client_order_id=row["client_order_id"],
                state=TargetState(row["state"]),
                broker_tag=row["broker_tag"],
                details=_load(row["details_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def migration_applied(self, name: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT summary_json FROM ledger_migrations WHERE name = ?", (name,)
            ).fetchone()
        return None if row is None else _load(row["summary_json"])

    def import_operations(
        self, name: str, operations: Iterable[ImportedOperation], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Copy historical operations in, once, in one transaction.

        Existing operation IDs are left untouched (INSERT OR IGNORE), so a re-run
        or a partially pre-populated ledger never overwrites live state. The
        migration is recorded under `name`; a second call returns that summary.
        """
        previous = self.migration_applied(name)
        if previous is not None:
            return {**previous, "already_applied": True}
        imported = skipped = 0
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in operations:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO operations (
                        operation_id, action, source, payload_hash, payload_json, state,
                        created_at, updated_at, broker_tag, response_json, error_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.operation_id,
                        item.action.value,
                        item.source,
                        item.payload_hash,
                        item.payload_json,
                        OperationState.PENDING.value,
                        item.created_at,
                        item.updated_at,
                        item.broker_tag,
                        _dump(item.response),
                        _dump(item.error),
                    ),
                )
                if cursor.rowcount == 0:
                    skipped += 1
                    continue
                imported += 1
                target = item.target
                connection.execute(
                    """
                    INSERT INTO operation_targets (
                        operation_id, account_alias, client_order_id, state, order_id,
                        position_id, deal_id, executed_volume_lots, execution_price,
                        error_code, error_message, updated_at, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.operation_id,
                        target.account,
                        target.client_order_id,
                        target.state.value,
                        target.order_id,
                        target.position_id,
                        target.deal_id,
                        _text(target.executed_volume_lots),
                        _text(target.execution_price),
                        target.error_code,
                        target.error_message,
                        item.updated_at,
                        _dump(target.details),
                    ),
                )
                self._refresh_parent(connection, item.operation_id, item.updated_at)
            summary = {"imported": imported, "skipped_existing": skipped, "applied_at": _now()}
            if dry_run:
                connection.rollback()
                return {**summary, "dry_run": True}
            connection.execute(
                "INSERT INTO ledger_migrations (name, applied_at, summary_json) VALUES (?, ?, ?)",
                (name, summary["applied_at"], _dump(summary)),
            )
            connection.commit()
        return summary

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
            TargetState.PARTIALLY_FILLED_FINAL,
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
