from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ta_contracts import Tick

from execution_service.adapters.ctrader.session import CTraderSession
from execution_service.api import create_app
from execution_service.config import Settings
from execution_service.errors import ServiceError
from execution_service.hub import MarketDataHub
from execution_service.stream import tick_stream
from tests.conftest import build_settings
from tests.fakes import FakeCTraderServer
from tests.test_session import happy_server

AUTH = {"X-API-Key": "test-api-key-at-least-16"}


def _tick(symbol: str = "EURUSD", bid: float = 1.08532) -> Tick:
    return Tick(
        symbol=symbol,
        bid=bid,
        ask=round(bid + 0.00013, 5),
        spread=0.00013,
        ts=datetime.now(UTC),
    )


async def _take(generator, count: int) -> list[dict]:
    """Pull exactly `count` events, failing fast rather than hanging."""
    events = []
    for _ in range(count):
        raw = await asyncio.wait_for(anext(generator), timeout=1.0)
        events.append({"event": raw["event"], "data": json.loads(raw["data"])})
    return events


# --- the generator contract --------------------------------------------------
#
# Tested directly rather than through TestClient: an open stream occupies the
# test client's portal thread, so nothing else can drive the app while it is
# held. The generator is where the real contract lives anyway.


async def test_stream_opens_with_the_connection_status() -> None:
    hub = MarketDataHub(queue_size=8)
    hub.publish_status("connected")
    generator = tick_stream(hub, None)

    events = await _take(generator, 1)

    assert events[0]["event"] == "status"
    assert events[0]["data"] == {"state": "connected", "dropped": 0}
    await generator.aclose()


async def test_cached_quotes_are_replayed_on_connect() -> None:
    """Otherwise a client joining mid-session waits unbounded on a quiet symbol."""
    hub = MarketDataHub(queue_size=8)
    hub.publish_tick(_tick("EURUSD"))
    hub.publish_tick(_tick("XAUUSD", bid=3300.5))
    generator = tick_stream(hub, None)

    events = await _take(generator, 3)

    replayed = {event["data"]["symbol"] for event in events if event["event"] == "tick"}
    assert replayed == {"EURUSD", "XAUUSD"}
    await generator.aclose()


async def test_replay_is_limited_to_the_requested_symbols() -> None:
    hub = MarketDataHub(queue_size=8)
    hub.publish_tick(_tick("EURUSD"))
    hub.publish_tick(_tick("XAUUSD", bid=3300.5))
    generator = tick_stream(hub, frozenset({"XAUUSD"}))

    events = await _take(generator, 2)

    ticks = [event for event in events if event["event"] == "tick"]
    assert [tick["data"]["symbol"] for tick in ticks] == ["XAUUSD"]
    await generator.aclose()


async def test_live_ticks_are_pushed_to_an_open_stream() -> None:
    hub = MarketDataHub(queue_size=8)
    generator = tick_stream(hub, None)
    await _take(generator, 1)  # status

    hub.publish_tick(_tick("EURUSD", bid=1.09))
    events = await _take(generator, 1)

    assert events[0]["event"] == "tick"
    assert events[0]["data"]["bid"] == 1.09
    assert events[0]["data"]["provider"] == "ctrader"
    await generator.aclose()


async def test_filtered_stream_ignores_other_symbols() -> None:
    hub = MarketDataHub(queue_size=8)
    generator = tick_stream(hub, frozenset({"XAUUSD"}))
    await _take(generator, 1)

    hub.publish_tick(_tick("EURUSD"))
    hub.publish_tick(_tick("XAUUSD", bid=3300.5))
    events = await _take(generator, 1)

    assert events[0]["data"]["symbol"] == "XAUUSD"
    await generator.aclose()


async def test_status_changes_reach_an_open_stream() -> None:
    hub = MarketDataHub(queue_size=8)
    generator = tick_stream(hub, None)
    await _take(generator, 1)

    hub.publish_status("reconnecting", error="ConnectionResetError")
    events = await _take(generator, 1)

    assert events[0]["data"]["state"] == "reconnecting"
    assert events[0]["data"]["error"] == "ConnectionResetError"
    await generator.aclose()


async def test_drop_count_is_reported_to_a_lagging_consumer() -> None:
    hub = MarketDataHub(queue_size=2)
    generator = tick_stream(hub, None)
    await _take(generator, 1)

    for i in range(20):
        hub.publish_tick(_tick("EURUSD", bid=1.0 + i / 1000))
    hub.publish_status("connected")

    # One replayed cached tick, the newest queued tick, then the status.
    events = await _take(generator, 3)

    status = [event for event in events if event["event"] == "status"]
    assert status and status[0]["data"]["dropped"] > 0
    await generator.aclose()


# --- subscriber lifecycle ----------------------------------------------------


async def test_subscriber_is_registered_only_while_the_stream_is_open() -> None:
    hub = MarketDataHub(queue_size=8)
    assert hub.subscriber_count == 0

    generator = tick_stream(hub, None)
    await _take(generator, 1)
    assert hub.subscriber_count == 1

    await generator.aclose()

    assert hub.subscriber_count == 0


async def test_client_disconnect_unregisters_the_subscriber() -> None:
    """The leak regression test.

    A bare StreamingResponse generator is only cancelled when it next tries to
    yield, so on a quiet symbol a disconnected client would stay registered and
    keep being fed forever. Cleanup has to happen in the generator's finally.
    """
    hub = MarketDataHub(queue_size=8)
    generator = tick_stream(hub, None)
    await _take(generator, 1)

    consumer = asyncio.create_task(anext(generator))  # parked on an empty queue
    await asyncio.sleep(0)
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await generator.aclose()

    assert hub.subscriber_count == 0


async def test_an_error_in_the_consumer_still_unregisters() -> None:
    hub = MarketDataHub(queue_size=8)
    generator = tick_stream(hub, None)
    await _take(generator, 1)

    with pytest.raises(RuntimeError):
        await generator.athrow(RuntimeError("consumer exploded"))

    assert hub.subscriber_count == 0


async def test_streams_are_independent() -> None:
    hub = MarketDataHub(queue_size=8)
    first = tick_stream(hub, None)
    second = tick_stream(hub, frozenset({"EURUSD"}))
    await _take(first, 1)
    await _take(second, 1)
    assert hub.subscriber_count == 2

    hub.publish_tick(_tick("XAUUSD", bid=3300.5))

    assert (await _take(first, 1))[0]["data"]["symbol"] == "XAUUSD"
    await first.aclose()
    await second.aclose()
    assert hub.subscriber_count == 0


# --- the endpoint ------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(
        tmp_path,
        RECONNECT_INITIAL_BACKOFF_SECONDS=0.01,
        REQUEST_TIMEOUT_SECONDS=1,
        STARTUP_READY_TIMEOUT_SECONDS=2,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    server: FakeCTraderServer = happy_server()
    session = CTraderSession(
        settings,
        MarketDataHub(queue_size=8),
        connector=server.connector(),  # type: ignore[arg-type]
    )
    with TestClient(create_app(settings=settings, session=session)) as client:
        yield client


def test_stream_requires_an_api_key(client: TestClient) -> None:
    """Auth rejects before the response starts streaming, so this returns."""
    response = client.get("/v1/stream/ticks")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_unknown_symbol_filter_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/stream/ticks?symbols=EURUSD,GBPUSD", headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "symbol_not_allowed"


async def test_response_declares_an_unbuffered_event_stream(settings: Settings) -> None:
    """Asserts on the response the endpoint constructs, not on a fetched body.

    Neither TestClient nor httpx's ASGITransport can be used here: both wait for
    the response body to complete, and an SSE body never does.
    """
    server: FakeCTraderServer = happy_server()
    session = CTraderSession(
        settings,
        MarketDataHub(queue_size=8),
        connector=server.connector(),  # type: ignore[arg-type]
    )
    app = create_app(settings=settings, session=session)
    route = next(r for r in app.routes if getattr(r, "path", None) == "/v1/stream/ticks")
    await session.start()
    await session.wait_ready(timeout_seconds=2.0)

    try:
        response = await route.endpoint(symbols=None)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] in {"no-store", "no-cache"}
        # Without this, nginx buffers text/event-stream and delays every event
        # until its own buffer fills.
        assert response.headers["x-accel-buffering"] == "no"
        assert response.ping_interval == int(settings.sse_keepalive_seconds)
    finally:
        await session.close()


async def test_unfiltered_stream_is_gated_on_readiness_like_the_filtered_one(
    settings: Settings,
) -> None:
    """Both forms of the endpoint must agree while the broker is down.

    The readiness check used to sit after the empty-filter early return, so
    `?symbols=EURUSD` returned 503 but the unfiltered stream was accepted and
    then sat silent forever.
    """
    server: FakeCTraderServer = happy_server()
    session = CTraderSession(
        settings,
        MarketDataHub(queue_size=8),
        connector=server.connector(),  # type: ignore[arg-type]
    )
    app = create_app(settings=settings, session=session)
    route = next(r for r in app.routes if getattr(r, "path", None) == "/v1/stream/ticks")

    # Session never started, so the broker is not connected.
    for symbols in (None, "EURUSD"):
        with pytest.raises(ServiceError) as caught:
            await route.endpoint(symbols=symbols)
        assert caught.value.status_code == 503
        assert caught.value.code == "broker_not_ready"
