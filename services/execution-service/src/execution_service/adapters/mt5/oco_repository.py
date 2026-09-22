"""Durable OCO intent and lifecycle records in a separate SQLite database."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class OcoRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS oco_groups "
                "(group_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, document TEXT NOT NULL)"
            )

    def reserve(self, group_id: str, payload_hash: str, document: dict[str, Any]) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO oco_groups VALUES (?, ?, ?)",
                (group_id, payload_hash, json.dumps(document)),
            )
            return cursor.rowcount == 1

    def get(self, group_id: str) -> tuple[str, dict[str, Any]] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_hash, document FROM oco_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
        return None if row is None else (row[0], json.loads(row[1]))

    def all(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT document FROM oco_groups ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, document: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE oco_groups SET document = ? WHERE group_id = ?",
                (json.dumps(document), document["group_id"]),
            )
