"""The two WebSocket feeds. Transport and subscriptions only: parsing is the engine's.

Every frame is handed to ``emit(venue, text)`` untouched, so the tape holds
exactly what arrived and a replay parses it with the live code. Each
(re)connect first emits a ``reconnect`` control line, which makes the engine
drop that venue's books until fresh snapshots arrive.

- **Kalshi** (``k``): authenticated upgrade (RSA-PSS over
  ``timestamp + "GET" + path``), channel ``orderbook_delta``. Tickers rotate
  each window and are added and removed on the open socket with
  ``update_subscription``. ``seq`` must be contiguous; on a gap the feed writes
  a ``resync`` control line and reconnects for fresh snapshots.
- **Polymarket** (``p``): public market channel with
  ``custom_feature_enabled`` (``best_bid_ask``, ``market_resolved``). Text
  ``PING`` every 10 s. Tokens are added with ``operation: subscribe``.
  Unsubscribe was not seen to stop the flow (2026-10-06), so the engine
  ignores tokens it does not hold, and ``recycle()`` reconnects with only the
  live set once enough have piled up.
- **Polymarket shards** (``PolyShards``, PLAN 2.1): ``PK_POLY_SOCKETS`` sockets,
  each with its own reader task and reconnect, assets spread across them. A
  shard's ``reconnect`` control line names the shard and its tokens, so the
  engine drops only those books. The tape stays one stream (venue ``p``).
- **PolyBolt** (``b``, ``PolyBoltFeed``): Polymarket's reference-price socket
  (``wss://ws-live-v2.polymarket.com/ws``, 2026-09-15): ``price.crypto`` (Pyth)
  and ``price.crypto.twap`` (the Chainlink 60 s TWAP the 15-minute markets settle
  on). Authenticated with CLOB API credentials, ``{"op": "auth"}`` first, then
  ``{"op": "subscribe", "subscriptions": [...]}``; the server pings every 25 s.
  It is a research feed (PLAN 4.1, a fast spot signal): the observer records it,
  the engine ignores venue ``b``. Its connects are written as ``b`` lines, never
  as ``c`` control lines, which the engine would read as a book reset.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlparse

import orjson
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from ta_core.logging_config import log_event

__all__ = ["KalshiFeed", "KalshiSigner", "PolyBoltFeed", "PolyFeed", "PolyShards", "WsConnect"]

Emit = Callable[[str, str], None]
WsConnect = Callable[..., contextlib.AbstractAsyncContextManager[Any]]


@contextlib.asynccontextmanager
async def _websocket(
    url: str, headers: dict[str, str] | None = None, max_queue: int = 32768
) -> AsyncIterator[Any]:
    from websockets.asyncio.client import connect

    # max_size: a Polymarket book frame for several tokens can pass the 1 MiB default.
    # max_queue: once this many frames wait, the client stops reading the socket and
    # Polymarket drops a reader that falls behind (1013 "slow consumer").
    async with connect(
        url,
        additional_headers=headers,
        ping_interval=20,
        ping_timeout=20,
        max_queue=max_queue,
        max_size=8 * 2**20,
    ) as socket:
        yield socket


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem: bytes) -> None:
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("the Kalshi key must be an RSA private key")
        self.key_id = key_id
        self._key = key

    def sign(self, message: str) -> str:
        signature = self._key.sign(
            message.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def headers(self, method: str, path: str, ts_ms: int | None = None) -> dict[str, str]:
        ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts + method + path.split("?", 1)[0]),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }


class _WsFeed:
    venue = "?"

    def __init__(
        self,
        url: str,
        emit: Emit,
        *,
        ws_connect: WsConnect | None = None,
        max_backoff: float = 30.0,
        max_queue: int = 32768,
    ) -> None:
        self.url = url
        self._emit = emit
        self._connect = ws_connect or _websocket
        self._max_queue = max_queue
        self.slow_consumer = 0  # closes for falling behind (1013)
        self._max_backoff = max_backoff
        self.wanted: set[str] = set()
        self.subscribed: set[str] = set()
        self._ws: Any = None
        self._lock = asyncio.Lock()
        self.connected = False
        self.connects = 0
        self.messages = 0
        self.errors = 0
        self.last_error: str | None = None

    def _headers(self) -> dict[str, str] | None:
        return None

    def _open(self) -> contextlib.AbstractAsyncContextManager[Any]:
        if self._connect is _websocket:
            return _websocket(self.url, self._headers(), self._max_queue)
        return self._connect(self.url, self._headers())

    def _control(self, kind: str, **fields: Any) -> None:
        self._emit("c", json.dumps({"type": kind, "venue": self.venue, **fields}))

    def _reconnect_fields(self) -> dict[str, Any]:
        return {}

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with self._open() as ws:
                    self._ws = ws
                    self.connected = True
                    self.connects += 1
                    self._control("reconnect", n=self.connects, **self._reconnect_fields())
                    log_event("feed_connected", venue=self.venue, n=self.connects)
                    async with self._lock:
                        self.subscribed = set()
                        await self._on_open(ws)
                    backoff = 1.0
                    async for raw in ws:
                        self.messages += 1
                        text = raw.decode() if isinstance(raw, bytes) else raw
                        if not await self._on_message(text):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                if "1013" in self.last_error:
                    self.slow_consumer += 1
                log_event(
                    "feed_error", level=logging.WARNING, venue=self.venue, error=self.last_error
                )
            finally:
                self._ws = None
                self.connected = False
                await self._on_close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def set_wanted(self, ids: set[str]) -> None:
        """Make the live subscription ``ids``. Kept across reconnects."""
        self.wanted = set(ids)
        async with self._lock:
            if self._ws is None:
                return
            add = sorted(self.wanted - self.subscribed)
            remove = sorted(self.subscribed - self.wanted)
            try:
                if add:
                    await self._add(self._ws, add)
                if remove:
                    await self._remove(self._ws, remove)
            except Exception as exc:  # noqa: BLE001 - the reader loop reconnects
                self.errors += 1
                self.last_error = f"subscribe: {type(exc).__name__}: {exc}"[:200]

    async def recycle(self) -> None:
        """Reconnect: the new socket subscribes ``wanted`` only."""
        if self._ws is not None:
            await self._ws.close()

    async def _on_open(self, ws: Any) -> None:
        if self.wanted:
            await self._add(ws, sorted(self.wanted))

    async def _on_close(self) -> None:
        return None

    async def _add(self, ws: Any, ids: list[str]) -> None:
        raise NotImplementedError

    async def _remove(self, ws: Any, ids: list[str]) -> None:
        raise NotImplementedError

    async def _on_message(self, text: str) -> bool:
        """Handle one frame; False closes the socket (and reconnects)."""
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connects": self.connects,
            "messages": self.messages,
            "errors": self.errors,
            "last_error": self.last_error,
            "subscribed": len(self.subscribed),
            "wanted": len(self.wanted),
            "slow_consumer": self.slow_consumer,
        }


class KalshiFeed(_WsFeed):
    venue = "k"
    CHANNEL = "orderbook_delta"

    def __init__(self, url: str, emit: Emit, signer: KalshiSigner, **kwargs: Any) -> None:
        super().__init__(url, emit, **kwargs)
        self._signer = signer
        self._path = urlparse(url).path or "/trade-api/ws/v2"
        self._sid: int | None = None
        self._sid_ready = asyncio.Event()
        self._seq: dict[int, int] = {}  # per sid
        self._next_id = 0
        self.seq_gaps = 0

    def _headers(self) -> dict[str, str]:
        return self._signer.headers("GET", self._path)

    def _cmd_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _on_open(self, ws: Any) -> None:
        self._sid = None
        self._sid_ready = asyncio.Event()
        self._seq = {}
        await super()._on_open(ws)

    async def _add(self, ws: Any, ids: list[str]) -> None:
        if self._sid is None and self.subscribed:
            # The first subscribe is in flight: one sid only, so wait for its id.
            async with asyncio.timeout(10):
                await self._sid_ready.wait()
        if self._sid is None:
            params: dict[str, Any] = {"channels": [self.CHANNEL], "market_tickers": ids}
            await ws.send(json.dumps({"id": self._cmd_id(), "cmd": "subscribe", "params": params}))
        else:
            params = {"sids": [self._sid], "market_tickers": ids, "action": "add_markets"}
            await ws.send(
                json.dumps({"id": self._cmd_id(), "cmd": "update_subscription", "params": params})
            )
        self.subscribed.update(ids)

    async def _remove(self, ws: Any, ids: list[str]) -> None:
        if self._sid is None:
            return
        params = {"sids": [self._sid], "market_tickers": ids, "action": "delete_markets"}
        await ws.send(
            json.dumps({"id": self._cmd_id(), "cmd": "update_subscription", "params": params})
        )
        self.subscribed.difference_update(ids)

    async def _on_message(self, text: str) -> bool:
        try:
            body = orjson.loads(text)
        except ValueError:
            self._emit(self.venue, text)
            return True
        kind = body.get("type")
        if kind == "subscribed":
            self._sid = (body.get("msg") or {}).get("sid", self._sid)
            self._sid_ready.set()
        elif kind == "error":
            self.errors += 1
            self.last_error = f"kalshi error: {body.get('msg')}"[:200]
            log_event("kalshi_ws_error", level=logging.WARNING, msg=body.get("msg"))
            self._emit(self.venue, text)
            # A refused update leaves the subscription unknown: start over.
            return False
        # Every frame carrying a seq advances it, not only orderbook frames: the
        # acknowledgement of an update_subscription takes a number too. Counting
        # only orderbook frames read each add/delete as a one-off gap and forced
        # a reconnect twice per window (production, 2026-10-06).
        seq, sid = body.get("seq"), body.get("sid")
        if isinstance(seq, int) and sid is not None:
            last = self._seq.get(sid)
            if last is not None and seq != last + 1:
                self.seq_gaps += 1
                self._control("resync", sid=sid, expected=last + 1, got=seq, frame=kind)
                log_event(
                    "kalshi_seq_gap", level=logging.WARNING, expected=last + 1, got=seq, frame=kind
                )
                return False
            self._seq[sid] = seq
        self._emit(self.venue, text)
        return True

    def stats(self) -> dict[str, Any]:
        return {**super().stats(), "sid": self._sid, "seq_gaps": self.seq_gaps}


class PolyFeed(_WsFeed):
    venue = "p"
    PING_SECONDS = 10.0

    def __init__(self, url: str, emit: Emit, *, shard: int | None = None, **kwargs: Any) -> None:
        super().__init__(url, emit, **kwargs)
        self.shard = shard
        self._initial_sent = False
        self._pinger: asyncio.Task[None] | None = None
        self.removed_since_connect = 0

    def _reconnect_fields(self) -> dict[str, Any]:
        """A shard names itself and its tokens: only those books go stale."""
        if self.shard is None:
            return {}
        return {"shard": self.shard, "tokens": sorted(self.wanted)}

    async def _on_open(self, ws: Any) -> None:
        self._initial_sent = False
        self.removed_since_connect = 0
        self._pinger = asyncio.create_task(self._ping(ws))
        await super()._on_open(ws)

    async def _on_close(self) -> None:
        if self._pinger is not None:
            self._pinger.cancel()
            self._pinger = None

    async def _ping(self, ws: Any) -> None:
        with contextlib.suppress(Exception):
            while True:
                await asyncio.sleep(self.PING_SECONDS)
                await ws.send("PING")

    async def _add(self, ws: Any, ids: list[str]) -> None:
        if not self._initial_sent:
            body: dict[str, Any] = {"assets_ids": ids, "type": "market"}
            self._initial_sent = True
        else:
            body = {"assets_ids": ids, "operation": "subscribe"}
        body["custom_feature_enabled"] = True
        await ws.send(json.dumps(body))
        self.subscribed.update(ids)

    async def _remove(self, ws: Any, ids: list[str]) -> None:
        await ws.send(json.dumps({"assets_ids": ids, "operation": "unsubscribe"}))
        self.subscribed.difference_update(ids)
        self.removed_since_connect += len(ids)

    async def _on_message(self, text: str) -> bool:
        if text == "PONG":
            return True
        self._emit(self.venue, text)
        return True

    def stats(self) -> dict[str, Any]:
        return {**super().stats(), "removed_since_connect": self.removed_since_connect}


class PolyBoltFeed(_WsFeed):
    """PolyBolt reference prices for the traded assets, recorded as venue ``b``."""

    venue = "b"
    SYMBOLS = {
        "BTC": "btcusd", "ETH": "ethusd", "SOL": "solusd", "XRP": "xrpusd",
        "BNB": "bnbusd", "DOGE": "dogeusd", "HYPE": "hypeusd",
    }  # fmt: skip
    TWAP_WINDOW_S = 60

    def __init__(
        self,
        url: str,
        emit: Emit,
        credentials: tuple[str, str, str],
        assets: tuple[str, ...],
        **kwargs: Any,
    ) -> None:
        super().__init__(url, emit, **kwargs)
        self._credentials = credentials
        self.wanted = {self.SYMBOLS[a] for a in assets if a in self.SYMBOLS}
        self._authed = False
        self.auth_failures = 0

    def _control(self, kind: str, **fields: Any) -> None:
        # Never a ``c`` line: a replay would read a ``reconnect`` there as a reset
        # of every Polymarket book.
        self._emit(self.venue, json.dumps({"type": f"pk_{kind}", "venue": self.venue, **fields}))

    def subscriptions(self, symbols: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol in symbols:
            out.append({"channel": "price.crypto", "filter": {"symbol": symbol}})
            out.append(
                {
                    "channel": "price.crypto.twap",
                    "filter": {"symbol": symbol, "window_seconds": self.TWAP_WINDOW_S},
                }
            )
        return out

    async def _on_open(self, ws: Any) -> None:
        self._authed = False
        key, secret, passphrase = self._credentials
        await ws.send(
            json.dumps(
                {
                    "op": "auth",
                    "rid": "auth",
                    "auth": {"apiKey": key, "secret": secret, "passphrase": passphrase},
                }
            )
        )

    async def _add(self, ws: Any, ids: list[str]) -> None:
        if not self._authed:
            return  # subscribed once the auth is acknowledged
        await ws.send(
            json.dumps({"op": "subscribe", "rid": "sub", "subscriptions": self.subscriptions(ids)})
        )
        self.subscribed.update(ids)

    async def _remove(self, ws: Any, ids: list[str]) -> None:
        return None  # the symbol set is fixed for the process

    async def _on_message(self, text: str) -> bool:
        if not self._authed:
            try:
                body = orjson.loads(text)
            except ValueError:
                body = {}
            failed = isinstance(body, dict) and (
                body.get("ok") is False
                or body.get("error") is not None
                or body.get("status") == "error"
            )
            if failed:
                self.auth_failures += 1
                self.errors += 1
                self.last_error = f"polybolt auth: {text[:150]}"
                log_event("polybolt_auth_failed", level=logging.WARNING, frame=text[:200])
                self._emit(self.venue, text)
                return False
            # The first frame that is not an error acknowledges the auth.
            self._authed = True
            if self._ws is not None and self.wanted:
                await self._add(self._ws, sorted(self.wanted))
        self._emit(self.venue, text)
        return True

    def stats(self) -> dict[str, Any]:
        return {**super().stats(), "authed": self._authed, "auth_failures": self.auth_failures}


class PolyShards:
    """``n`` Polymarket sockets, assets spread round-robin across them.

    The same surface the runtime uses on one ``PolyFeed`` (``run``,
    ``set_wanted``, ``recycle``, ``connected``, ``stats``), plus
    ``set_wanted_groups`` so each asset's tokens stay on one shard.
    """

    def __init__(self, url: str, emit: Emit, assets: tuple[str, ...], n: int, **kwargs: Any):
        n = max(1, min(n or len(assets), len(assets)))
        self.shards = [PolyFeed(url, emit, shard=i, **kwargs) for i in range(n)]
        self.shard_of = {asset: i % n for i, asset in enumerate(assets)}

    async def run(self) -> None:
        await asyncio.gather(*(shard.run() for shard in self.shards))

    async def set_wanted_groups(self, groups: dict[str, set[str]]) -> None:
        wanted: list[set[str]] = [set() for _ in self.shards]
        for asset, ids in groups.items():
            wanted[self.shard_of.get(asset, 0)] |= ids
        await asyncio.gather(*(s.set_wanted(w) for s, w in zip(self.shards, wanted, strict=True)))

    async def set_wanted(self, ids: set[str]) -> None:
        """Without asset groups everything goes to shard 0 (tests, one-socket use)."""
        await self.set_wanted_groups({"": ids})

    @property
    def removed_since_connect(self) -> int:
        return max(s.removed_since_connect for s in self.shards)

    async def recycle(self, min_removed: int = 0) -> None:
        for shard in self.shards:
            if shard.removed_since_connect >= min_removed:
                await shard.recycle()

    @property
    def connected(self) -> bool:
        return all(s.connected for s in self.shards)

    @property
    def last_error(self) -> str | None:
        return next((s.last_error for s in self.shards if s.last_error), None)

    def stats(self) -> dict[str, Any]:
        per = [s.stats() for s in self.shards]
        total = {
            k: sum(p[k] for p in per)
            for k in ("connects", "messages", "errors", "subscribed", "wanted", "slow_consumer")
        }
        return {
            "connected": self.connected,
            **total,
            "last_error": self.last_error,
            "removed_since_connect": self.removed_since_connect,
            "sockets": len(self.shards),
            "shards": per,
        }
