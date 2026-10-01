"""Durable OCO group documents, in the execution ledger's database.

A group is one JSON document (request, legs, broker intents and results,
faults) that the coordinator rewrites as it observes the broker. Documents are
stored whole: every intent is saved before the broker call it describes, so a
crash leaves the evidence a restart needs to reconcile.

The table lives in the same SQLite file as the operations ledger and shares
its ``ledger_migrations`` marker table, so the one-time import of a
pre-unification ``.oco.sqlite3`` is recorded the same way as the signal import.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["ImportedGroup", "OcoGroupStore"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ImportedGroup:
    group_id: str
    payload_hash: str
    document: dict[str, Any]


class OcoGroupStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=10000")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oco_groups (
                    group_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    document TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS oco_groups_account ON oco_groups(account);

                CREATE TABLE IF NOT EXISTS ledger_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );
                """
            )
        os.chmod(self.path, 0o600)

    def reserve(
        self, group_id: str, account: str, payload_hash: str, document: dict[str, Any]
    ) -> bool:
        """Insert a new group; False if the ID is already taken."""
        timestamp = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO oco_groups VALUES (?, ?, ?, ?, ?, ?)",
                (group_id, account, payload_hash, json.dumps(document), timestamp, timestamp),
            )
            return cursor.rowcount == 1

    def get(self, group_id: str) -> tuple[str, dict[str, Any]] | None:
        """(payload_hash, document), or None."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_hash, document FROM oco_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
        return None if row is None else (row[0], json.loads(row[1]))

    def account_of(self, group_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT account FROM oco_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    def all(self, account: str) -> list[dict[str, Any]]:
        """Every group on one account, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document FROM oco_groups WHERE account = ? ORDER BY rowid", (account,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save(self, document: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE oco_groups SET document = ?, updated_at = ? WHERE group_id = ?",
                (json.dumps(document), _now(), document["group_id"]),
            )

    def migration_applied(self, name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT summary_json FROM ledger_migrations WHERE name = ?", (name,)
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def import_groups(
        self,
        name: str,
        account: str,
        groups: Iterable[ImportedGroup],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Copy historical groups in, once; an existing group ID is never overwritten."""
        previous = self.migration_applied(name)
        if previous is not None:
            return {**previous, "already_applied": True}
        imported = skipped = 0
        timestamp = _now()
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for group in groups:
                created = str(group.document.get("created_at") or timestamp)
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO oco_groups VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        group.group_id,
                        account,
                        group.payload_hash,
                        json.dumps(group.document),
                        created,
                        str(group.document.get("updated_at") or created),
                    ),
                )
                if cursor.rowcount == 1:
                    imported += 1
                else:
                    skipped += 1
            summary = {"imported": imported, "skipped_existing": skipped, "applied_at": timestamp}
            if dry_run:
                connection.rollback()
                return {**summary, "dry_run": True}
            connection.execute(
                "INSERT INTO ledger_migrations (name, applied_at, summary_json) VALUES (?, ?, ?)",
                (name, timestamp, json.dumps(summary, sort_keys=True)),
            )
            connection.commit()
        finally:
            connection.close()
        return summary
