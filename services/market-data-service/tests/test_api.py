from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ta_contracts import MarketKind

from market_data_service.api import create_app
from market_data_service.config import MarketBinding, Settings
from tests.conftest import AUTH, FakeProvider, build_settings

ROUTES = [
    "/v1/forex/tick?symbol=XAUUSD",
    "/v1/forex/candles?symbol=XAUUSD",
    "/v1/forex/symbols",
    "/v1/forex/capabilities",
    "/v1/forex/stream/ticks",
]


@pytest.fixture
def client(settings: Settings, provider: FakeProvider) -> Iterator[TestClient]:
    with TestClient(create_app(settings, providers={"mt5": provider})) as test_client:
        yield test_client


@pytest.mark.parametrize("path", ROUTES)
def test_every_market_route_requires_the_api_key(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_lifespan_starts_and_closes_providers(settings: Settings, provider: FakeProvider) -> None:
    with TestClient(create_app(settings, providers={"mt5": provider})):
        assert provider.started
    assert provider.closed


def test_tick_returns_the_providers_quote(client: TestClient, provider: FakeProvider) -> None:
    provider.publish("XAUUSD")

    response = client.get("/v1/forex/tick?symbol=XAUUSD", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert (body["symbol"], body["source_instrument"], body["provider"]) == (
        "XAUUSD",
        "XAUUSD.b",
        "mt5",
    )
    assert (body["bid"], body["ask"], body["price"]) == (2000.0, 2000.5, 2000.25)


def test_provider_errors_pass_through_unchanged(client: TestClient) -> None:
    response = client.get("/v1/forex/tick?symbol=XAUUSD", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "tick_unavailable"


@pytest.mark.parametrize("market", ["deriv", "crypto", "stocks"])
def test_a_market_this_process_does_not_serve_is_404(client: TestClient, market: str) -> None:
    response = client.get(f"/v1/{market}/symbols", headers=AUTH)

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "market_not_enabled"
    assert error["details"]["enabled"] == ["forex"]


def test_a_matching_provider_override_is_accepted(client: TestClient) -> None:
    assert client.get("/v1/forex/symbols?provider=mt5", headers=AUTH).status_code == 200


def test_a_different_provider_override_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/forex/symbols?provider=ctrader", headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "provider_not_available"


def test_candles_pass_feed_and_parsed_to_through(
    client: TestClient, provider: FakeProvider
) -> None:
    response = client.get(
        "/v1/forex/candles?symbol=XAUUSD&timeframe=H1&count=3&to=2026-03-02T10:00:00Z",
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["symbol"], body["timeframe"], len(body["candles"])) == ("XAUUSD", "H1", 3)
    assert provider.candle_calls == [
        {
            "feed": None,
            "symbol": "XAUUSD",
            "timeframe": "H1",
            "count": 3,
            "to": datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
        }
    ]


def test_candle_count_is_capped(client: TestClient) -> None:
    response = client.get("/v1/forex/candles?symbol=XAUUSD&count=5001", headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "count_exceeds_limit",
        "message": "count exceeds MAX_CANDLES_LOOKBACK",
        "details": {"maximum": 5000},
    }


def test_unsupported_timeframe_is_a_structured_422(client: TestClient) -> None:
    response = client.get("/v1/forex/candles?symbol=XAUUSD&timeframe=W1", headers=AUTH)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "timeframe_not_supported"
    assert error["details"]["supported"] == ["M5", "H1"]


def test_an_unparseable_to_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/forex/candles?symbol=XAUUSD&to=yesterday", headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_timestamp"


def test_symbols_name_the_market_and_provider(client: TestClient) -> None:
    body = client.get("/v1/forex/symbols", headers=AUTH).json()

    assert (body["market"], body["provider"]) == ("forex", "mt5")
    assert [i["symbol"] for i in body["instruments"]] == ["XAUUSD"]


def test_capabilities_describe_the_market(client: TestClient) -> None:
    body = client.get("/v1/forex/capabilities", headers=AUTH).json()

    assert body == {
        "market": "forex",
        "provider": "mt5",
        "timeframes": ["M5", "H1"],
        "streaming": True,
        "bid_ask": True,
        "max_candles": 5000,
    }


def test_an_unknown_stream_symbol_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/forex/stream/ticks?symbols=EURUSD", headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "symbol_not_allowed"


def test_the_unfiltered_stream_is_gated_on_readiness(
    client: TestClient, provider: FakeProvider
) -> None:
    provider.ready = False

    response = client.get("/v1/forex/stream/ticks", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "broker_not_ready"


async def test_the_stream_is_an_unbuffered_event_stream(
    settings: Settings, provider: FakeProvider
) -> None:
    """Asserts on the response the endpoint constructs: an SSE body never
    completes, so neither TestClient nor ASGITransport can fetch it."""
    app = create_app(settings, providers={"mt5": provider})
    route = next(r for r in app.routes if getattr(r, "path", None) == "/v1/{market}/stream/ticks")

    response = await route.endpoint(market="forex", symbols=None, provider=None)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.ping_interval == int(settings.sse_keepalive_seconds)


def test_ready_when_providers_are_connected_and_quotes_fresh(
    client: TestClient, provider: FakeProvider
) -> None:
    provider.publish("XAUUSD", age=5)

    response = client.get("/health/ready")

    assert response.status_code == 200
    details = response.json()["details"]
    assert details["providers"]["mt5"] == {"connected": True}
    assert details["markets"]["forex"]["provider"] == "mt5"


def test_ready_before_the_first_quote(client: TestClient) -> None:
    assert client.get("/health/ready").status_code == 200


def test_a_stale_market_is_not_ready(client: TestClient, provider: FakeProvider) -> None:
    provider.publish("XAUUSD", age=120)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert "120s old" in response.json()["details"]["markets"]["forex"]["reason"]


def test_a_disconnected_provider_is_not_ready(client: TestClient, provider: FakeProvider) -> None:
    provider.ready = False

    assert client.get("/health/ready").status_code == 503


def test_one_process_routes_two_markets_to_two_feeds(tmp_path: Path) -> None:
    ctrader = FakeProvider("ctrader", feeds=("forex_demo", "deriv_demo"), symbols=("EURUSD",))
    settings = build_settings(
        tmp_path,
        markets={
            MarketKind.FOREX: MarketBinding(provider="ctrader", feed="forex_demo"),
            MarketKind.DERIV: MarketBinding(provider="ctrader", feed="deriv_demo"),
        },
    )
    ctrader.publish("EURUSD", feed="deriv_demo")

    with TestClient(create_app(settings, providers={"ctrader": ctrader})) as client:
        deriv = client.get("/v1/deriv/tick?symbol=EURUSD", headers=AUTH)
        forex = client.get("/v1/forex/tick?symbol=EURUSD", headers=AUTH)

    assert deriv.status_code == 200
    assert forex.status_code == 503  # forex_demo has no quote yet: different feed
