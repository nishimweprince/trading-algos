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


async def test_signals_trades_and_dashboard(build, tmp_path) -> None:
    from ofi_scalper_service.alerts import Alerts
    from ofi_scalper_service.config import ExecutionMode
    from ofi_scalper_service.execution_bridge import DEFAULT_FILTERS, ExecutionBridge
    from tests.model_fixture import Dial, dial_model

    runtime = build(FakeFuturesStream({}))
    async with await client_for(runtime) as client:
        empty = await client.get("/v1/trades", headers=AUTH)
        assert empty.json()["trades"] == [] and empty.json()["summary"]["trades"] == 0

    settings = runtime.settings.model_copy(update={"execution_mode": ExecutionMode.SHADOW})
    runtime.bridge = ExecutionBridge(
        settings,
        risk=runtime.risk,
        alerts=Alerts(None),
        model=dial_model(tmp_path / "models", Dial()),
        filters=dict(DEFAULT_FILTERS),
        state_dir=tmp_path / "state",
        fees=lambda _s: (2.0, 5.0),
    )
    t = 1_790_000_000_000_000_000
    runtime.bridge.book.signal(t, {"symbol": "BTCUSDT", "action": "enter", "p": 0.8})
    record = {"cycle_id": "BTCUSDT-1", "entry_qty_ordered": "0.003", "filled": False}
    runtime.bridge.book.trade(t, record)
    async with await client_for(runtime) as client:
        signals = (await client.get("/v1/signals", headers=AUTH)).json()["signals"]
        assert signals == [{"symbol": "BTCUSDT", "action": "enter", "p": 0.8}]
        recent = (await client.get("/v1/trades", headers=AUTH)).json()
        assert recent["trades"] == [record] and recent["summary"]["entries"] == 1
        day = (await client.get("/v1/trades", params={"date": "2026-09-21"}, headers=AUTH)).json()
        assert day["trades"] == [record] and day["summary"]["signals"] == 1
        page = await client.get("/dashboard")
        assert page.status_code == 200 and "OFI scalper" in page.text
        assert (await client.get("/v1/signals")).status_code == 401
