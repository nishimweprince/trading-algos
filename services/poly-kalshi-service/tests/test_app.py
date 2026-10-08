from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import AUTH, make_engine, make_settings
from fastapi.testclient import TestClient

from poly_kalshi_service.alerts import Alerts
from poly_kalshi_service.app import create_app
from poly_kalshi_service.runtime import Runtime
from poly_kalshi_service.windows import Discovery


def _nothing_listed(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=[] if request.url.host == "g.test" else {"markets": []})


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = make_settings(tmp_path)
    http = httpx.AsyncClient(transport=httpx.MockTransport(_nothing_listed))
    runtime = Runtime(
        settings,
        engine=make_engine(tmp_path, assets=settings.assets, kill_file=tmp_path / "KILL"),
        discovery=Discovery(http, gamma_url="https://g.test", kalshi_rest_url="https://k.test"),
        kalshi=None,
        poly=None,
        alerts=Alerts(None),
    )
    return TestClient(create_app(settings, runtime))


def test_status_needs_the_key(client: TestClient) -> None:
    with client:
        assert client.get("/v1/status").status_code == 401
        body = client.get("/v1/status", headers=AUTH).json()
        assert body["execution_mode"] == "paper"
        assert body["params"]["max_entry_px"] == 0.60
        assert set(body["engine"]["assets"]) == {"BTC", "ETH", "SOL", "XRP", "BNB"}
        assert client.get("/health/live").status_code == 200


def test_not_ready_until_windows_are_identified(client: TestClient) -> None:
    with client:
        response = client.get("/health/ready")
        assert response.status_code == 503


def test_kill_and_ack(client: TestClient, tmp_path: Path) -> None:
    with client:
        assert client.post("/v1/kill", headers=AUTH, json={"reason": "test"}).json()["fired"]
        status = client.get("/v1/status", headers=AUTH).json()
        assert status["engine"]["risk"]["halted"] is True
        (tmp_path / "KILL").touch()
        assert client.post("/v1/kill/ack", headers=AUTH).status_code == 409
        (tmp_path / "KILL").unlink()
        assert client.post("/v1/kill/ack", headers=AUTH).json()["halted"] is False
