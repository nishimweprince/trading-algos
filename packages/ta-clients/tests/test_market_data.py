"""MarketDataClient and JsonlCandleCache, with no service imports.

The cache takes a plain path function and the client a plain httpx client, which
is what proves both stand free of any one consumer's settings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from ta_contracts import Candle, CandlesResponse, MarketKind, Timeframe

from ta_clients import JsonlCandleCache, MarketDataClient

START = datetime(2026, 1, 5, tzinfo=UTC)


def _candles(n: int, start: datetime = START, step: int = 15) -> list[Candle]:
    return [
        Candle(
            ts=start + timedelta(minutes=step * i),
            open=1.0 + i,
            high=2.0 + i,
            low=0.5 + i,
            close=1.5 + i,
            volume=100 + i,
            provider="mt5",
            source_instrument="XAUUSDb",
        )
        for i in range(n)
    ]


# --- cache -----------------------------------------------------------------


@pytest.fixture
def cache(tmp_path: Path) -> JsonlCandleCache:
    return JsonlCandleCache(lambda symbol, tf: tmp_path / symbol.upper() / f"{tf.value}.jsonl")


def test_round_trips_through_the_jsonl_cache(cache: JsonlCandleCache) -> None:
    written = _candles(5)
    path = cache.write("XAUUSD", Timeframe.M15, written)

    assert path.is_file()
    assert cache.exists("XAUUSD", Timeframe.M15)
    assert cache.load("XAUUSD", Timeframe.M15) == written


def test_write_sorts_by_timestamp(cache: JsonlCandleCache) -> None:
    written = _candles(4)
    cache.write("XAUUSD", Timeframe.M15, reversed(written))

    assert [c.ts for c in cache.load("XAUUSD", Timeframe.M15)] == [c.ts for c in written]


def test_load_is_empty_when_nothing_was_seeded(cache: JsonlCandleCache) -> None:
    assert cache.load("XAUUSD", Timeframe.H1) == []
    assert cache.exists("XAUUSD", Timeframe.H1) is False


def test_load_filters_by_date_range_and_count(cache: JsonlCandleCache) -> None:
    cache.write("XAUUSD", Timeframe.M15, _candles(10))

    ranged = cache.load(
        "XAUUSD",
        Timeframe.M15,
        date_from=START + timedelta(minutes=30),
        date_to=START + timedelta(minutes=75),
    )
    latest = cache.load("XAUUSD", Timeframe.M15, count=3)

    assert [c.ts for c in ranged] == [START + timedelta(minutes=m) for m in (30, 45, 60, 75)]
    assert [c.ts for c in latest] == [START + timedelta(minutes=m) for m in (105, 120, 135)]


def test_the_consumer_owns_the_path_layout(cache: JsonlCandleCache) -> None:
    assert cache.path("xauusd", Timeframe.M15).parent.name == "XAUUSD"


# --- client ----------------------------------------------------------------


class FakeService:
    """Serves `history` the way market-data-service does: newest `count` before `to`."""

    def __init__(self, history: list[Candle], cap: int) -> None:
        self.history = history
        self.cap = cap
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        count = min(int(params["count"]), self.cap)
        to = datetime.fromisoformat(params["to"]) if "to" in params else None
        eligible = [c for c in self.history if to is None or c.ts <= to]
        page = eligible[-count:]
        body = CandlesResponse(symbol=params["symbol"], timeframe=params["timeframe"], candles=page)
        return httpx.Response(200, json=body.model_dump(mode="json"))


def _client(handler, *, page_size: int = 5000, api_key: str | None = "k" * 16):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MarketDataClient(
        "http://md:8020/",
        market=MarketKind.DERIV,
        api_key=api_key,
        client=http,
        page_size=page_size,
    )


async def test_candles_hit_the_market_route_with_the_api_key() -> None:
    service = FakeService(_candles(3), cap=5000)

    candles = await _client(service).candles("Volatility 75 Index", Timeframe.M15, count=3)

    assert len(candles) == 3
    [request] = service.requests
    assert request.url.path == "/v1/deriv/candles"
    assert request.url.params["symbol"] == "Volatility 75 Index"
    assert request.headers["X-API-Key"] == "k" * 16


async def test_candles_page_backwards_past_the_per_request_cap() -> None:
    history = _candles(25)
    service = FakeService(history, cap=10)

    candles = await _client(service, page_size=10).candles("XAUUSD", Timeframe.M15, count=22)

    assert candles == history[-22:]
    assert len(service.requests) == 3


async def test_candles_stop_when_history_runs_out() -> None:
    history = _candles(7)
    service = FakeService(history, cap=5)

    candles = await _client(service, page_size=5).candles("XAUUSD", Timeframe.M15, count=50)

    assert candles == history


async def test_candles_range_filters_to_the_window() -> None:
    history = _candles(40)
    service = FakeService(history, cap=5000)

    candles = await _client(service).candles_range(
        "XAUUSD",
        Timeframe.M15,
        date_from=START + timedelta(hours=2),
        date_to=START + timedelta(hours=3),
    )

    assert [c.ts for c in candles] == [
        START + timedelta(minutes=m) for m in (120, 135, 150, 165, 180)
    ]


async def test_quote_parses_a_market_quote() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/deriv/tick"
        return httpx.Response(
            200,
            json={
                "symbol": "Volatility 75 Index",
                "source_instrument": "Volatility 75 Index",
                "provider": "mt5",
                "ts": "2026-03-02T10:00:00Z",
                "price": 100.2,
                "bid": 100.1,
                "ask": 100.3,
                "spread": 0.2,
            },
        )

    quote = await _client(handler).quote("Volatility 75 Index")

    assert (quote.bid, quote.ask, quote.provider) == (100.1, 100.3, "mt5")


async def test_errors_raise_with_the_response_attached() -> None:
    client = _client(lambda request: httpx.Response(404, json={"error": {"code": "x"}}))

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await client.quote("XAUUSD")

    assert raised.value.response.status_code == 404


async def test_no_api_key_sends_no_header() -> None:
    service = FakeService(_candles(1), cap=5000)

    await _client(service, api_key=None).candles("XAUUSD", Timeframe.M15, count=1)

    assert "X-API-Key" not in service.requests[0].headers


async def test_ready_reports_transport_failure_rather_than_raising() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    ready, reason = await _client(boom).ready()

    assert ready is False and "refused" in reason


async def test_ready_is_true_on_200() -> None:
    assert await _client(lambda request: httpx.Response(200, json={})).ready() == (True, "ok")
