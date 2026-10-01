"""One-time import of the pre-unification MT5 signal ledger.

Before execution unified on ta-store, each MT5 host kept its own
``signals`` table in DATABASE_PATH (signals.db). Replay safety depends on that
history: a signal ID already executed must replay its stored response, never
reach the terminal a second time. So the history is copied, once, into the
execution ledger as one-target PLACE_ORDER operations on the host's account.

The import is idempotent twice over: a marker row records that it ran, and
each operation is inserted only if its ID is new, so a signal the unified
service already handled is never overwritten. The source database is opened
read-only and left in place as the backup.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

from ta_contracts import OperationAction, SignalState
from ta_store import (
    ExecutionRepository,
    ImportedGroup,
    ImportedOperation,
    ImportedTarget,
    OcoGroupStore,
)

from .signals import TARGET_STATES

__all__ = [
    "migrate_legacy_ledger",
    "migrate_legacy_oco",
    "migration_name",
    "oco_migration_name",
]


def migration_name(account: str) -> str:
    return f"legacy-signals:{account}"


def oco_migration_name(account: str) -> str:
    return f"legacy-oco:{account}"


def _load(value: str | None) -> Any | None:
    return None if value is None else json.loads(value)


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _read_legacy(path: Path, account: str) -> Iterator[ImportedOperation]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM signals ORDER BY created_at").fetchall()
    finally:
        connection.close()
    for row in rows:
        keys = row.keys()
        payload = json.loads(row["payload_json"])
        response = _load(row["response_json"])
        error = _load(row["error_json"])
        details = {
            name: value
            for name in ("request", "check", "result")
            if f"{name}_json" in keys and (value := _load(row[f"{name}_json"])) is not None
        }
        state = TARGET_STATES[SignalState(row["state"])]
        yield ImportedOperation(
            operation_id=str(row["signal_id"]),
            action=OperationAction.PLACE_ORDER,
            source=str(payload.get("source", row["broker_tag"])),
            payload_hash=str(row["payload_hash"]),
            payload_json=str(row["payload_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            broker_tag=str(row["broker_tag"]),
            response=response,
            error=error,
            target=ImportedTarget(
                account=account,
                state=state,
                order_id=_int((response or {}).get("order_ticket")),
                deal_id=_int((response or {}).get("deal_ticket")),
                executed_volume_lots=(
                    None
                    if (response or {}).get("executed_volume") is None
                    else Decimal(str(response["executed_volume"]))
                ),
                execution_price=(
                    None
                    if (response or {}).get("execution_price") is None
                    else Decimal(str(response["execution_price"]))
                ),
                error_code=None if error is None else str(error.get("code")),
                error_message=None if error is None else str(error.get("message")),
                details=details or None,
            ),
        )


def migrate_legacy_ledger(
    source: Path,
    repository: ExecutionRepository,
    account: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import ``source`` into ``repository`` under ``account``; return a summary.

    A missing source, or one with no ``signals`` table, imports nothing and
    records nothing, so a later run against the real file still happens.
    """
    if not source.is_file():
        return {"imported": 0, "skipped_existing": 0, "source": str(source), "missing": True}
    try:
        operations = list(_read_legacy(source, account))
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {"imported": 0, "skipped_existing": 0, "source": str(source), "missing": True}
    summary = repository.import_operations(migration_name(account), operations, dry_run=dry_run)
    return {**summary, "source": str(source), "account": account}


def _read_legacy_oco(path: Path) -> list[ImportedGroup]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT group_id, payload_hash, document FROM oco_groups ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()
    return [ImportedGroup(str(row[0]), str(row[1]), json.loads(row[2])) for row in rows]


def migrate_legacy_oco(
    source: Path,
    store: OcoGroupStore,
    account: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import the pre-unification ``<DATABASE_PATH>.oco.sqlite3`` groups, once.

    Documents are copied verbatim, so an imported group replays its stored
    document and keeps being monitored; its legs' broker tags are unchanged,
    so the monitor still recognises their orders as owned.
    """
    if not source.is_file():
        return {"imported": 0, "skipped_existing": 0, "source": str(source), "missing": True}
    try:
        groups = _read_legacy_oco(source)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {"imported": 0, "skipped_existing": 0, "source": str(source), "missing": True}
    summary = store.import_groups(oco_migration_name(account), account, groups, dry_run=dry_run)
    return {**summary, "source": str(source), "account": account}
