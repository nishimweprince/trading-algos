from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import create_app
from config import Settings
from ctrader.proto import ProtoOAGetTrendbarsRes, ProtoOASpotEvent, ProtoOATrendbar
from ctrader.session import CTraderSession
from hub import MarketDataHub
from tests.conftest import build_settings
from tests.fakes import FakeCTraderServer
from tests.test_session import ACCOUNT_ID, happy_server

API_KEY = "test-api-key-at-least-16"
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(
        tmp_path,
        LIVE_TRENDBAR_PERIODS="",
        RECONNECT_INITIAL_BACKOFF_SECONDS=0.01,
        RECONNECT_MAX_BACKOFF_SECONDS=0.02,
        REQUEST_TIMEOUT_SECONDS=1,
        STARTUP_READY_TIMEOUT_SECONDS=2,
    )


def _client(settings: Settings, server: FakeCTraderServer) -> Iterator[TestClient]:
    hub = MarketDataHub(queue_size=settings.subscriber_queue_size)
    session = CTraderSession(settings, hub, connector=server.connector())  # type: ignore[arg-type]
    with TestClient(create_app(settings=settings, session=session)) as client:
        yield client


@pytest.fixture
def server() -> FakeCTraderServer:
    return happy_server()


@pytest.fixture
def client(settings: Settings, server: FakeCTraderServer) -> Iterator[TestClient]:
    yield from _client(settings, server)


def _push_tick(server: FakeCTraderServer, client: TestClient, symbol_id: int = 1) -> None:
    server.push(
        ProtoOASpotEvent(ctidTraderAccountId=ACCOUNT_ID, symbolId=symbol_id, bid=108532, ask=108545)
    )
    # Let the reader loop drain by making a round trip through the app.
    for _ in range(20):
        client.get("/health/live")
        if client.app.state.hub.known_symbols():
            return


# --- auth --------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/market-data/tick?symbol=EURUSD",
        "/v1/market-data/candles?symbol=EURUSD",
        "/v1/symbols",
        "/v1/stream/ticks",
    ],
)
def test_missing_api_key_is_rejected(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_api_key_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/symbols", headers={"X-API-Key": "wrong-but-long-enough"})
    assert response.status_code == 401


def test_health_endpoints_are_unauthenticated(client: TestClient) -> None:
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code in (200, 503)


# --- symbols -----------------------------------------------------------------


def test_list_symbols_returns_the_resolved_catalog(client: TestClient) -> None:
    body = client.get("/v1/symbols", headers=AUTH).json()

    names = {entry["symbol"]: entry for entry in body["symbols"]}
    assert set(names) == {"EURUSD", "XAUUSD"}
    assert names["XAUUSD"]["digits"] == 2
    assert names["EURUSD"]["symbol_id"] == 1


# --- tick --------------------------------------------------------------------


def test_tick_returns_the_cached_quote(client: TestClient, server: FakeCTraderServer) -> None:
    _push_tick(server, client)

    body = client.get("/v1/market-data/tick?symbol=EURUSD", headers=AUTH).json()

    assert body["bid"] == 1.08532
    assert body["ask"] == 1.08545
    assert body["provider"] == "ctrader"


def test_tick_for_an_unconfigured_symbol_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/market-data/tick?symbol=GBPUSD", headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "symbol_not_allowed"


def test_tick_before_any_quote_is_503(client: TestClient) -> None:
    response = client.get("/v1/market-data/tick?symbol=EURUSD", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "tick_unavailable"


# --- candles -----------------------------------------------------------------


def _trendbars_response(now: datetime) -> ProtoOAGetTrendbarsRes:
    bars = [
        ProtoOATrendbar(
            volume=10,
            low=110000,
            deltaOpen=200,
            deltaHigh=500,
            deltaClose=100,
            utcTimestampInMinutes=int((now - timedelta(hours=h)).timestamp() // 60),
        )
        for h in (3, 2)
    ]
    return ProtoOAGetTrendbarsRes(
        ctidTraderAccountId=ACCOUNT_ID, period=9, symbolId=1, trendbar=bars, hasMore=False
    )


def test_candles_returns_closed_bars(client: TestClient, server: FakeCTraderServer) -> None:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    server.reply_with("ProtoOAGetTrendbarsReq", _trendbars_response(now))

    body = client.get(
        "/v1/market-data/candles?symbol=EURUSD&timeframe=H1&count=10", headers=AUTH
    ).json()

    assert body["symbol"] == "EURUSD"
    assert body["timeframe"] == "H1"
    assert len(body["candles"]) == 2
    candle = body["candles"][0]
    assert (candle["open"], candle["high"], candle["low"], candle["close"]) == (
        1.102,
        1.105,
        1.1,
        1.101,
    )
    assert candle["source_instrument"] == "EURUSD"
    assert candle["provider"] == "ctrader"


def test_candle_shape_matches_the_lookup_trader_contract(
    client: TestClient, server: FakeCTraderServer
) -> None:
    """Field-identical to lookup-trader's providers/base.py::Candle, so a client
    there can do Candle(**payload)."""
    server.reply_with(
        "ProtoOAGetTrendbarsReq", _trendbars_response(datetime.now(UTC).replace(microsecond=0))
    )

    body = client.get("/v1/market-data/candles?symbol=EURUSD&count=5", headers=AUTH).json()

    assert set(body["candles"][0]) == {
        "ts",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "provider",
        "source_instrument",
        "spread",
        "spread_source",
    }


def test_candles_count_over_the_cap_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/market-data/candles?symbol=EURUSD&count=999999", headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "count_exceeds_limit"


def test_candles_rejects_an_unparseable_to(client: TestClient) -> None:
    response = client.get("/v1/market-data/candles?symbol=EURUSD&to=not-a-date", headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_timestamp"


def test_unknown_timeframe_uses_the_validation_error_shape(client: TestClient) -> None:
    response = client.get("/v1/market-data/candles?symbol=EURUSD&timeframe=MN1", headers=AUTH)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]


# --- health ------------------------------------------------------------------


def test_ready_when_connected_without_quotes(client: TestClient) -> None:
    """A connected session with no ticks yet is normal at startup and over a
    market close."""
    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["details"]["connected"] is True
    assert body["details"]["reason"] == "no quotes received yet"


def test_not_ready_when_quotes_are_stale(tmp_path: Path, server: FakeCTraderServer) -> None:
    settings = build_settings(
        tmp_path,
        LIVE_TRENDBAR_PERIODS="",
        TICK_STALENESS_SECONDS=1,
        RECONNECT_INITIAL_BACKOFF_SECONDS=0.01,
        STARTUP_READY_TIMEOUT_SECONDS=2,
    )
    for client in _client(settings, server):
        server.push(
            ProtoOASpotEvent(
                ctidTraderAccountId=ACCOUNT_ID,
                symbolId=1,
                bid=108532,
                ask=108545,
                timestamp=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp() * 1000),
            )
        )
        for _ in range(20):
            client.get("/health/live")
            if client.app.state.hub.known_symbols():
                break

        response = client.get("/health/ready")

        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert "old" in response.json()["details"]["reason"]


def test_not_ready_when_the_broker_never_connects(tmp_path: Path) -> None:
    settings = build_settings(
        tmp_path,
        LIVE_TRENDBAR_PERIODS="",
        RECONNECT_INITIAL_BACKOFF_SECONDS=0.01,
        RECONNECT_MAX_BACKOFF_SECONDS=0.02,
        REQUEST_TIMEOUT_SECONDS=0.05,
        STARTUP_READY_TIMEOUT_SECONDS=0.3,
    )
    server = FakeCTraderServer()
    server.silence("ProtoOAApplicationAuthReq")

    for client in _client(settings, server):
        response = client.get("/health/ready")

        assert response.status_code == 503
        details = response.json()["details"]
        assert details["connected"] is False
        assert details["reason"] == "broker session is not connected"


def test_endpoints_are_503_before_the_broker_is_ready(tmp_path: Path) -> None:
    settings = build_settings(
        tmp_path,
        LIVE_TRENDBAR_PERIODS="",
        RECONNECT_INITIAL_BACKOFF_SECONDS=0.01,
        REQUEST_TIMEOUT_SECONDS=0.05,
        STARTUP_READY_TIMEOUT_SECONDS=0.3,
    )
    server = FakeCTraderServer()
    server.silence("ProtoOAApplicationAuthReq")

    for client in _client(settings, server):
        response = client.get("/v1/symbols", headers=AUTH)

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "broker_not_ready"


# --- error shape -------------------------------------------------------------


def test_service_errors_render_a_consistent_envelope(client: TestClient) -> None:
    body = client.get("/v1/market-data/tick?symbol=NOPE", headers=AUTH).json()

    assert set(body) == {"error"}
    assert set(body["error"]) >= {"code", "message"}
    assert json.dumps(body)  # serialisable
