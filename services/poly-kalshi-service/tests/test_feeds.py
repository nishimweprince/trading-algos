from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from conftest import PEM, PUBLIC_KEY
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from poly_kalshi_service.feeds import KalshiFeed, KalshiSigner, PolyFeed


class FakeSocket:
    """Frames queued by the test; sends recorded. Iteration ends when ``closed``."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, data: Any) -> None:
        self.sent.append(json.loads(data) if data != "PING" else data)

    async def close(self) -> None:
        self.closed = True
        await self.inbox.put(None)

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item


class FakeConnect:
    def __init__(self) -> None:
        self.sockets: list[FakeSocket] = []
        self.headers: list[dict[str, str] | None] = []
        self.opened = asyncio.Event()

    @contextlib.asynccontextmanager
    async def __call__(self, url: str, headers: Any = None) -> AsyncIterator[FakeSocket]:
        socket = FakeSocket()
        self.sockets.append(socket)
        self.headers.append(headers)
        self.opened.set()
        yield socket


def test_signer_signs_timestamp_method_path_with_pss() -> None:
    signer = KalshiSigner("kid", PEM)
    headers = signer.headers("GET", "/trade-api/ws/v2?x=1", ts_ms=1700000000000)
    assert headers["KALSHI-ACCESS-KEY"] == "kid"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    PUBLIC_KEY.verify(  # raises if the signature does not match
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000GET/trade-api/ws/v2",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


async def _until(predicate: Any, seconds: float = 2.0) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():  # noqa: ASYNC110 - polling test state is the point
            await asyncio.sleep(0.005)


async def test_kalshi_subscribes_then_updates_on_the_same_sid() -> None:
    emitted: list[tuple[str, str]] = []
    connect = FakeConnect()
    feed = KalshiFeed(
        "wss://k.test/trade-api/ws/v2",
        lambda v, t: emitted.append((v, t)),
        KalshiSigner("kid", PEM),
        ws_connect=connect,
    )
    feed.wanted = {"T1"}
    task = asyncio.create_task(feed.run())
    await connect.opened.wait()
    socket = connect.sockets[0]
    await _until(lambda: socket.sent)
    assert socket.sent[0]["cmd"] == "subscribe"
    assert socket.sent[0]["params"] == {"channels": ["orderbook_delta"], "market_tickers": ["T1"]}
    assert connect.headers[0]["KALSHI-ACCESS-KEY"] == "kid"
    await socket.inbox.put(json.dumps({"type": "subscribed", "msg": {"sid": 7}}))
    await feed.set_wanted({"T2"})
    add, delete = socket.sent[1], socket.sent[2]
    assert add["cmd"] == "update_subscription"
    assert add["params"] == {"sids": [7], "market_tickers": ["T2"], "action": "add_markets"}
    assert delete["params"]["action"] == "delete_markets"
    assert delete["params"]["market_tickers"] == ["T1"]
    assert emitted[0][0] == "c" and json.loads(emitted[0][1])["type"] == "reconnect"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_kalshi_seq_gap_resyncs_and_reconnects() -> None:
    emitted: list[tuple[str, str]] = []
    connect = FakeConnect()
    feed = KalshiFeed(
        "wss://k.test/trade-api/ws/v2",
        lambda v, t: emitted.append((v, t)),
        KalshiSigner("kid", PEM),
        ws_connect=connect,
        max_backoff=0.01,
    )
    task = asyncio.create_task(feed.run())
    await connect.opened.wait()
    socket = connect.sockets[0]
    frame = {"type": "orderbook_delta", "sid": 1, "msg": {}}
    await socket.inbox.put(json.dumps(frame | {"seq": 1}))
    await socket.inbox.put(json.dumps(frame | {"seq": 2}))
    await socket.inbox.put(json.dumps(frame | {"seq": 4}))
    await _until(lambda: len(connect.sockets) == 2, 5)
    kinds = [(v, json.loads(t).get("type")) for v, t in emitted]
    assert kinds[:4] == [
        ("c", "reconnect"),
        ("k", "orderbook_delta"),
        ("k", "orderbook_delta"),
        ("c", "resync"),
    ]
    assert feed.seq_gaps == 1
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_poly_initial_subscribe_then_operations_and_pong_is_dropped() -> None:
    emitted: list[tuple[str, str]] = []
    connect = FakeConnect()
    feed = PolyFeed(
        "wss://p.test/ws/market", lambda v, t: emitted.append((v, t)), ws_connect=connect
    )
    task = asyncio.create_task(feed.run())
    await connect.opened.wait()
    socket = connect.sockets[0]
    await feed.set_wanted({"a", "b"})
    await feed.set_wanted({"b", "c"})
    first, add, remove = socket.sent
    assert first == {"assets_ids": ["a", "b"], "type": "market", "custom_feature_enabled": True}
    assert add == {"assets_ids": ["c"], "operation": "subscribe", "custom_feature_enabled": True}
    assert remove == {"assets_ids": ["a"], "operation": "unsubscribe"}
    assert feed.removed_since_connect == 1
    await socket.inbox.put("PONG")
    await socket.inbox.put('{"event_type":"best_bid_ask"}')
    await _until(lambda: len(emitted) == 2)
    assert emitted[1] == ("p", '{"event_type":"best_bid_ask"}')
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_signer_rejects_a_non_rsa_key() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(ValueError, match="RSA"):
        KalshiSigner("kid", pem)
