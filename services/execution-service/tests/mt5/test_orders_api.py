"""`/v1/orders` and friends on an MT5 host, where the account alias is the profile."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from execution_service.api import create_app


@pytest.fixture
def client(settings, adapter) -> Iterator[TestClient]:
    hfm = settings.model_copy(update={"profile": "hfm"})
    with TestClient(create_app(hfm, mt5_adapter=adapter)) as client:
        client.headers["X-API-Key"] = settings.api_key.get_secret_value()
        yield client


def _order(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operation_id": str(uuid4()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "source": "trading_central",
        "instrument": "EURUSD",
        "execution_type": "market",
        "direction": "buy",
        "targets": [{"account": "hfm", "volume_lots": "0.10"}],
    }
    payload.update(overrides)
    return payload


def test_market_order_fills_and_is_readable(client: TestClient, adapter) -> None:
    payload = _order()

    created = client.post("/v1/orders", json=payload)
    fetched = client.get(f"/v1/operations/{payload['operation_id']}")
    replay = client.post("/v1/orders", json=payload)

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["state"] == "succeeded"
    assert body["targets"][0]["state"] == "filled"
    assert body["targets"][0]["deal_id"] == 2001
    assert fetched.json() == body
    assert replay.json() == body
    assert len(adapter.send_requests) == 1
    assert adapter.send_requests[0]["comment"].startswith("o-")


def test_order_sources_default_to_the_signal_allowlist(client: TestClient) -> None:
    response = client.post("/v1/orders", json=_order(source="not_allowed"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "source_not_allowed"


def test_unknown_account_is_rejected(client: TestClient, adapter) -> None:
    response = client.post(
        "/v1/orders", json=_order(targets=[{"account": "ftmo", "volume_lots": "0.1"}])
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "account_not_allowed"
    assert adapter.send_requests == []


def test_rejected_order_leaves_a_rejected_operation(client: TestClient, adapter) -> None:
    adapter.send_result = {"retcode": 10006, "comment": "Request rejected"}

    response = client.post("/v1/orders", json=_order())

    assert response.status_code == 201
    assert response.json()["state"] == "rejected"
    assert response.json()["targets"][0]["error_code"] == "10006"


def test_position_close_and_inventory(client: TestClient, adapter) -> None:
    adapter.positions = [{"ticket": 77, "symbol": "EURUSD", "type": 0, "volume": 0.1}]

    positions = client.get("/v1/accounts/hfm/positions")
    closed = client.post(
        "/v1/positions/close",
        json={
            "operation_id": str(uuid4()),
            "occurred_at": datetime.now(UTC).isoformat(),
            "source": "trading_central",
            "targets": [{"account": "hfm", "position_id": 77, "volume_lots": "0.1"}],
        },
    )

    assert positions.status_code == 200
    assert positions.json()[0]["position_id"] == 77
    assert closed.status_code == 201
    assert closed.json()["targets"][0]["state"] == "closed"
    assert client.get("/v1/accounts/ftmo/orders").status_code == 404


def test_accounts_lists_the_terminal_account(client: TestClient) -> None:
    response = client.get("/v1/accounts")

    assert response.status_code == 200
    [account] = response.json()["accounts"]
    assert account["alias"] == "hfm"
    assert account["provider"] == "mt5"
    assert account["ctid_trader_account_id"] is None
    assert account["order_entry_enabled"] is True


def test_a_signal_id_reused_as_an_operation_id_conflicts(
    client: TestClient, signal_factory
) -> None:
    signal = signal_factory()
    first = client.post("/v1/signals", json=signal.model_dump(mode="json"))

    reused = client.post("/v1/orders", json=_order(operation_id=str(signal.signal_id)))

    assert first.status_code == 200
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "operation_id_conflict"


def _wait_for(client: TestClient, operation_id: str, state: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while True:
        body = client.get(f"/v1/operations/{operation_id}").json()
        if body["targets"][0]["state"] == state or time.monotonic() > deadline:
            return body
        time.sleep(0.02)


def test_a_send_that_outlives_the_timeout_still_lands_its_outcome(settings, adapter) -> None:
    slow = settings.model_copy(
        update={
            "profile": "hfm",
            "execution_response_timeout_seconds": 0.05,
            "reconcile_interval_seconds": 0,
        }
    )
    adapter.send_delay = 0.3
    payload = _order()
    with TestClient(create_app(slow, mt5_adapter=adapter)) as client:
        client.headers["X-API-Key"] = settings.api_key.get_secret_value()

        response = client.post("/v1/orders", json=payload)
        settled = _wait_for(client, payload["operation_id"], "filled")

    assert response.status_code == 202
    assert response.json()["targets"][0]["state"] == "unknown"
    assert response.json()["targets"][0]["error_code"] == "EXECUTION_TIMEOUT"
    assert settled["state"] == "succeeded"
    assert settled["targets"][0]["deal_id"] == 2001
    assert settled["targets"][0]["error_code"] is None
    assert len(adapter.send_requests) == 1


def test_the_sweep_settles_an_unknown_order_from_history(settings, adapter) -> None:
    swept = settings.model_copy(update={"profile": "hfm", "reconcile_interval_seconds": 0.05})
    adapter.send_exception = ConnectionError("terminal dropped the reply")
    payload = _order()
    with TestClient(create_app(swept, mt5_adapter=adapter)) as client:
        client.headers["X-API-Key"] = settings.api_key.get_secret_value()

        response = client.post("/v1/orders", json=payload)
        comment = adapter.send_requests[0]["comment"]
        adapter.deals = [
            {"ticket": 77, "order": 76, "symbol": "EURUSD", "volume": 0.1, "comment": comment}
        ]
        settled = _wait_for(client, payload["operation_id"], "filled")

    assert response.json()["targets"][0]["state"] == "unknown"
    assert settled["targets"][0]["deal_id"] == 77
    assert len(adapter.send_requests) == 1, "reconciliation never re-sends"


def test_orders_on_a_live_account_need_live_trading_enabled(
    client: TestClient, adapter, signal_factory
) -> None:
    adapter.trade_mode = 2  # ACCOUNT_TRADE_MODE_REAL

    order = client.post("/v1/orders", json=_order())
    [account] = client.get("/v1/accounts").json()["accounts"]
    signal = client.post("/v1/signals", json=signal_factory().model_dump(mode="json"))

    assert order.status_code == 503
    assert order.json()["error"]["code"] == "live_trading_disabled"
    assert (account["environment"], account["is_live"]) == ("live", True)
    assert account["order_entry_enabled"] is False
    # /v1/signals keeps its own gates; live HFM/FTMO hosts still take signals.
    assert signal.status_code == 200, signal.text
    assert len(adapter.send_requests) == 1


def test_orders_on_a_live_account_pass_with_live_trading_enabled(settings, adapter) -> None:
    live = settings.model_copy(update={"profile": "hfm", "live_trading_enabled": True})
    adapter.trade_mode = 2
    with TestClient(create_app(live, mt5_adapter=adapter)) as client:
        client.headers["X-API-Key"] = settings.api_key.get_secret_value()
        response = client.post("/v1/orders", json=_order())

    assert response.status_code == 201, response.text
