"""Binance stand-ins for consumers' tests: the REST API and the book stream.

``FakeBinance.handler`` is an ``httpx.MockTransport`` handler serving the
recorded exchangeInfo and book-ticker payloads in ``fixtures/`` and synthetic
one-minute klines; ``FakeStream`` is a ``ws_connect`` that plays frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import httpx

__all__ = ["NOW", "FakeBinance", "FakeStream", "book_ticker_frames", "fixture"]

NOW = datetime(2026, 10, 1, 10, 7, 30, tzinfo=UTC)
MINUTE_MS = 60_000


def fixture(name: str) -> Any:
    return json.loads(resources.files(__package__).joinpath("fixtures", name).read_text())


def book_ticker_frames() -> list[str]:
    return [json.dumps(row) for row in fixture("book_ticker_stream.json")]


class FakeBinance:
    """The REST endpoints the plugin calls, answering like Binance does."""

    def __init__(self, now: datetime = NOW) -> None:
        self.requests: list[httpx.Request] = []
        self.rate_limited = False
        self.used_weight = "12"
        # One-minute bars every minute up to and including the forming one.
        last_open = int(now.timestamp() * 1000) // MINUTE_MS * MINUTE_MS
        self.minute_opens = [last_open - i * MINUTE_MS for i in range(3000)][::-1]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"x-mbx-used-weight-1m": self.used_weight}
        if self.rate_limited:
            return httpx.Response(429, headers={**headers, "retry-after": "30"}, json={})
        path = request.url.path
        params = request.url.params
        if path == "/api/v3/exchangeInfo":
            return httpx.Response(200, headers=headers, json=fixture("exchange_info.json"))
        if path == "/api/v3/ticker/bookTicker":
            book = fixture("book_ticker.json")
            if "symbol" in params:
                book = next(row for row in book if row["symbol"] == params["symbol"])
            return httpx.Response(200, headers=headers, json=book)
        if path == "/api/v3/klines":
            if params["symbol"] == "NOPE":
                return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
            end = int(params["endTime"])
            limit = int(params["limit"])
            opens = [t for t in self.minute_opens if t <= end][-limit:]
            rows = [
                [t, "100.0", "101.0", "99.0", str(100 + i % 7), "5.5", t + MINUTE_MS - 1]
                for i, t in enumerate(opens)
            ]
            return httpx.Response(200, headers=headers, json=rows)
        return httpx.Response(404, json={"code": -1, "msg": path})


class FakeStream:
    """A WebSocket connector: each connection plays ``messages`` then closes."""

    def __init__(self, messages: list[str], *, hold_open: bool = True) -> None:
        self.messages = messages
        self.hold_open = hold_open
        self.urls: list[str] = []
        self.release = asyncio.Event()

    def __call__(self, url: str) -> contextlib.AbstractAsyncContextManager[AsyncIterator[str]]:
        self.urls.append(url)

        @contextlib.asynccontextmanager
        async def connect() -> AsyncIterator[AsyncIterator[str]]:
            async def frames() -> AsyncIterator[str]:
                for message in self.messages:
                    yield message
                if self.hold_open:
                    await self.release.wait()

            yield frames()

        return connect()
