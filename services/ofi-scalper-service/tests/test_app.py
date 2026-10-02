"""Routes and auth, against a runtime that is built but not started."""

from __future__ import annotations

import httpx

from ofi_scalper_service.app import create_app
from tests.conftest import AUTH, FakeFuturesStream


async def client_for(runtime) -> httpx.AsyncClient:
    app = create_app(runtime.settings, runtime=runtime)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_routes_need_the_api_key(build) -> None:
    runtime = build(FakeFuturesStream({}))
    async with await client_for(runtime) as client:
        assert (await client.get("/v1/status")).status_code == 401
        assert (await client.post("/v1/kill")).status_code == 401
        assert (await client.get("/health/live")).status_code == 200


async def test_not_ready_until_booted(build) -> None:
    runtime = build(FakeFuturesStream({}))
    async with await client_for(runtime) as client:
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["details"]["booted"] is False


async def test_kill_then_ack_over_http(build) -> None:
    runtime = build(FakeFuturesStream({}))
    async with await client_for(runtime) as client:
        response = await client.post("/v1/kill", json={"reason": "drill"}, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["fired"] is True
        assert response.json()["risk"]["source"] == "http"
        again = await client.post("/v1/kill", headers=AUTH)
        assert again.json()["fired"] is False
        status = (await client.get("/v1/status", headers=AUTH)).json()
        assert status["risk"]["halted"] is True
        assert status["execution_mode"] == "off"
        ack = await client.post("/v1/kill/ack", headers=AUTH)
        assert ack.status_code == 200 and ack.json()["halted"] is False
        refused = await client.post("/v1/kill/ack", headers=AUTH)
        assert refused.status_code == 409


async def test_features_404_before_any_sample(build) -> None:
    runtime = build(FakeFuturesStream({}))
    async with await client_for(runtime) as client:
        response = await client.get("/v1/features", params={"symbol": "btcusdt"}, headers=AUTH)
        assert response.status_code == 404
        runtime.latest["BTCUSDT"] = {"symbol": "BTCUSDT", "mid": 1.0}
        response = await client.get("/v1/features", params={"symbol": "btcusdt"}, headers=AUTH)
        assert response.json()["mid"] == 1.0
