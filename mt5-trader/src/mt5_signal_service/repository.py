from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import SignalState


@dataclass(frozen=True)
class StoredSignal:
    signal_id: str
    payload_hash: str
    payload_json: str
    state: SignalState
    broker_tag: str
    request: dict[str, Any] | None
    check: dict[str, Any] | None
    result: dict[str, Any] | None
    response: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dump(value: Any | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load(value: str | None) -> Any | None:
    return None if value is None else json.loads(value)


class SignalRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
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
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(signals)").fetchall()
            }
            if "result_json" not in columns:
                connection.execute("ALTER TABLE signals ADD COLUMN result_json TEXT")

    def is_healthy(self) -> bool:
        try:
            with self._lock, self._connect() as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    def reserve(
        self, signal_id: str, payload_hash: str, payload_json: str, broker_tag: str
    ) -> tuple[StoredSignal, bool]:
        timestamp = _now()
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO signals (
                        signal_id, payload_hash, payload_json, state, broker_tag,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal_id,
                        payload_hash,
                        payload_json,
                        SignalState.RECEIVED.value,
                        broker_tag,
                        timestamp,
                        timestamp,
                    ),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = connection.execute(
                "SELECT * FROM signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("reserved signal could not be read")
        return self._from_row(row), created

    def get(self, signal_id: str) -> StoredSignal | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        return None if row is None else self._from_row(row)

    def list_states(self, *states: SignalState) -> list[StoredSignal]:
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM signals WHERE state IN ({placeholders})",  # noqa: S608
                tuple(state.value for state in states),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def mark_executing(
        self, signal_id: str, request: dict[str, Any], check: dict[str, Any]
    ) -> None:
        self._update(
            signal_id,
            SignalState.EXECUTING,
            request_json=_dump(request),
            check_json=_dump(check),
        )

    def mark_success(
        self,
        signal_id: str,
        state: SignalState,
        response: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> None:
        self._update(
            signal_id,
            state,
            result_json=_dump(result),
            response_json=_dump(response),
            error_json=None,
        )

    def mark_rejected(
        self,
        signal_id: str,
        error: dict[str, Any],
        *,
        request: dict[str, Any] | None = None,
        check: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        columns = {"error_json": _dump(error)}
        if request is not None:
            columns["request_json"] = _dump(request)
        if check is not None:
            columns["check_json"] = _dump(check)
        if result is not None:
            columns["result_json"] = _dump(result)
        self._update(signal_id, SignalState.REJECTED, **columns)

    def mark_unknown(self, signal_id: str, error: dict[str, Any]) -> None:
        self._update(signal_id, SignalState.UNKNOWN, error_json=_dump(error))

    def _update(self, signal_id: str, state: SignalState, **columns: Any) -> None:
        allowed = {
            "request_json",
            "check_json",
            "result_json",
            "response_json",
            "error_json",
        }
        if not set(columns).issubset(allowed):  # pragma: no cover - internal contract
            raise ValueError("invalid update column")
        assignments = ["state = ?", "updated_at = ?"]
        values: list[Any] = [state.value, _now()]
        for column, value in columns.items():
            assignments.append(f"{column} = ?")
            values.append(value)
        values.append(signal_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE signals SET {', '.join(assignments)} WHERE signal_id = ?",  # noqa: S608
                values,
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> StoredSignal:
        return StoredSignal(
            signal_id=row["signal_id"],
            payload_hash=row["payload_hash"],
            payload_json=row["payload_json"],
            state=SignalState(row["state"]),
            broker_tag=row["broker_tag"],
            request=_load(row["request_json"]),
            check=_load(row["check_json"]),
            result=_load(row["result_json"]),
            response=_load(row["response_json"]),
            error=_load(row["error_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
