from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ta_store import ExecutionRepository, ImportedGroup, OcoGroupStore


@pytest.fixture
def store(tmp_path: Path) -> OcoGroupStore:
    path = tmp_path / "data" / "executions.sqlite3"
    ExecutionRepository(path).initialize()
    store = OcoGroupStore(path)
    store.initialize()
    return store


def _doc(group_id: str, **extra: object) -> dict[str, object]:
    return {"group_id": group_id, "state": "staging", **extra}


def test_reserve_is_first_writer_wins(store: OcoGroupStore) -> None:
    assert store.reserve("g1", "hfm", "h", _doc("g1")) is True
    assert store.reserve("g1", "hfm", "other", _doc("g1", state="x")) is False
    assert store.get("g1") == ("h", _doc("g1"))
    assert store.account_of("g1") == "hfm"


def test_save_rewrites_the_document(store: OcoGroupStore) -> None:
    store.reserve("g1", "hfm", "h", _doc("g1"))
    store.save(_doc("g1", state="placed"))
    record = store.get("g1")
    assert record is not None and record[1]["state"] == "placed"


def test_all_is_scoped_to_one_account(store: OcoGroupStore) -> None:
    store.reserve("g1", "hfm", "h", _doc("g1"))
    store.reserve("g2", "ftmo", "h", _doc("g2"))
    store.reserve("g3", "hfm", "h", _doc("g3"))
    assert [doc["group_id"] for doc in store.all("hfm")] == ["g1", "g3"]
    assert store.get("missing") is None


def test_shares_the_ledger_database_and_marker_table(store: OcoGroupStore) -> None:
    with sqlite3.connect(store.path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"operations", "oco_groups", "ledger_migrations"} <= tables


def test_import_runs_once_and_never_overwrites(store: OcoGroupStore) -> None:
    store.reserve("g1", "hfm", "live", _doc("g1", state="placed"))
    groups = [
        ImportedGroup("g1", "old", _doc("g1", state="stale")),
        ImportedGroup("g2", "h2", _doc("g2", created_at="2026-09-01T00:00:00+00:00")),
    ]

    first = store.import_groups("legacy-oco:hfm", "hfm", groups)
    again = store.import_groups("legacy-oco:hfm", "hfm", groups)

    assert (first["imported"], first["skipped_existing"]) == (1, 1)
    assert again["already_applied"] is True
    assert store.get("g1") == ("live", _doc("g1", state="placed"))
    assert store.account_of("g2") == "hfm"


def test_import_dry_run_writes_nothing(store: OcoGroupStore) -> None:
    summary = store.import_groups(
        "legacy-oco:hfm", "hfm", [ImportedGroup("g1", "h", _doc("g1"))], dry_run=True
    )
    assert summary["dry_run"] is True and summary["imported"] == 1
    assert store.get("g1") is None
    assert store.migration_applied("legacy-oco:hfm") is None
