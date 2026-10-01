"""The legacy OCO import, the /v1/oco routes and their /v1/mt5 aliases."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from ta_contracts import OcoGroupRequest
from ta_store import ExecutionRepository, OcoGroupStore

from execution_service.api import create_app
from execution_service.compat import legacy_oco_path
from execution_service.migration import migrate_legacy_oco, oco_migration_name
from tests.mt5.test_oco import OcoAdapter


def _request(**overrides: Any) -> OcoGroupRequest:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "group_id": uuid4(),
        "profile": "hfm",
        "occurred_at": now,
        "decision_at": now,
        "symbol": "EURUSD",
        "volume": "0.1",
        "upper_trigger": "1.1010",
        "lower_trigger": "1.0990",
        "stop_distance": "0.001",
        "target_distance": "0.002",
        "expires_at": now + timedelta(minutes=15),
        "source": "trading_central",
    }
    values.update(overrides)
    return OcoGroupRequest.model_validate(values)


def _legacy_oco(path: Path, groups: list[OcoGroupRequest]) -> None:
    """The pre-unification OcoRepository schema, exactly."""
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE oco_groups "
            "(group_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, document TEXT NOT NULL)"
        )
        for group in groups:
            document = {
                **group.model_dump(mode="json"),
                "state": "cancelled",
                "created_at": "2026-09-01T10:00:00+00:00",
                "legs": {},
            }
            connection.execute(
                "INSERT INTO oco_groups VALUES (?, ?, ?)",
                (
                    str(group.group_id),
                    hashlib.sha256(group.model_dump_json().encode()).hexdigest(),
                    json.dumps(document),
                ),
            )
    connection.close()


@pytest.fixture
def store(settings) -> OcoGroupStore:
    ExecutionRepository(settings.execution_database_path).initialize()
    store = OcoGroupStore(settings.execution_database_path)
    store.initialize()
    return store


def test_legacy_groups_are_imported_once(settings, store) -> None:
    group = _request()
    _legacy_oco(legacy_oco_path(settings), [group])

    first = migrate_legacy_oco(legacy_oco_path(settings), store, "hfm")
    again = migrate_legacy_oco(legacy_oco_path(settings), store, "hfm")

    assert first["imported"] == 1
    assert again["already_applied"] is True
    assert store.account_of(str(group.group_id)) == "hfm"
    assert store.migration_applied(oco_migration_name("hfm")) is not None


def test_legacy_dry_run_and_missing_source(settings, store) -> None:
    assert migrate_legacy_oco(legacy_oco_path(settings), store, "hfm")["missing"] is True
    _legacy_oco(legacy_oco_path(settings), [_request()])

    summary = migrate_legacy_oco(legacy_oco_path(settings), store, "hfm", dry_run=True)

    assert summary["dry_run"] is True and summary["imported"] == 1
    assert store.all("hfm") == []


@pytest.fixture
def hfm(settings):
    return settings.model_copy(update={"profile": "hfm", "mt5_oco_enabled": True})


def _client(hfm) -> tuple[TestClient, OcoAdapter]:
    adapter = OcoAdapter(hfm.login)
    client = TestClient(create_app(hfm, mt5_adapter=adapter))
    client.headers["X-API-Key"] = hfm.api_key.get_secret_value()
    return client, adapter


def test_imported_group_replays_through_both_paths(hfm) -> None:
    group = _request()
    _legacy_oco(legacy_oco_path(hfm), [group])
    client, adapter = _client(hfm)
    body = json.loads(group.model_dump_json())

    with client:
        generic = client.post("/v1/oco", json=body)
        alias = client.post("/v1/mt5/oco", json=body)
        conflict = client.post("/v1/oco", json={**body, "volume": "0.2"})
        fetched = client.get(f"/v1/oco/{group.group_id}")

    assert generic.status_code == alias.status_code == 200
    assert generic.json() == alias.json() == fetched.json()
    assert generic.json()["state"] == "cancelled"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert adapter.send_requests == []


def test_new_group_on_the_generic_route_accepts_account(hfm) -> None:
    client, adapter = _client(hfm)
    group = _request()
    body = json.loads(group.model_dump_json())
    body["account"] = body.pop("profile")

    with client:
        capabilities = client.get("/v1/oco/capabilities", params={"symbol": "EURUSD"})
        placed = client.post("/v1/oco", json=body)
        via_alias = client.get(f"/v1/mt5/oco/{group.group_id}")
        cancelled = client.post(f"/v1/oco/{group.group_id}/cancel")

    assert capabilities.json()["ready"] is True
    assert capabilities.json()["account"] == "hfm"
    assert placed.status_code == 200 and placed.json()["state"] == "placed"
    assert via_alias.json()["group_id"] == str(group.group_id)
    assert cancelled.status_code == 200
    assert not adapter.live_orders


def test_unknown_group_and_unknown_account(hfm) -> None:
    client, _ = _client(hfm)
    with client:
        missing = client.get(f"/v1/oco/{uuid4()}")
        elsewhere = client.post(
            "/v1/oco", json=json.loads(_request(profile="ftmo").model_dump_json())
        )
        no_account = client.get("/v1/oco/capabilities", params={"account": "ftmo"})

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "oco_not_found"
    assert elsewhere.status_code == 422
    assert elsewhere.json()["error"]["code"] == "oco_profile_mismatch"
    assert no_account.status_code == 422
