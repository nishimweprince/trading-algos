"""The USDⓈ-M user-data stream: order updates and account updates for the adapter.

Lifecycle: ``POST /fapi/v1/listenKey`` (API-key header) gives a key valid for
60 minutes; ``PUT`` re-arms it (every ``KEEPALIVE_SECONDS``); a connection
lives at most 24 h. Since the 2026-03 routing split the socket is
``<ws root>/private/ws?listenKey=<key>&events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE``.

Every (re)connect awaits ``on_connected`` before the first event is handled:
updates sent while the socket was down are gone, so the adapter resyncs its
orders and positions from REST there. ``listenKeyExpired`` ends the connection
and the loop starts again with a fresh key.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ta_core.logging_config import log_event

from .rest import FapiRest, VenueNotSent, VenueTransportError
from .streams import WsConnect, _websocket

__all__ = ["EVENTS", "KEEPALIVE_SECONDS", "UserStream"]

EVENTS = "ORDER_TRADE_UPDATE/ACCOUNT_UPDATE"
KEEPALIVE_SECONDS = 30 * 60
WEIGHT_LISTEN_KEY = 1


class UserStream:
    def __init__(
        self,
        rest: FapiRest,
        ws_root: str,
        on_event: Callable[[dict[str, Any]], None],
        *,
        on_connected: Callable[[], Awaitable[None]],
        ws_connect: WsConnect | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        max_backoff_seconds: float = 30.0,
    ) -> None:
        self._rest = rest
        self._root = ws_root.rstrip("/")
        self._on_event = on_event
        self._on_connected = on_connected
        self._ws_connect = ws_connect or _websocket
        self._sleep = sleep
        self._max_backoff = max_backoff_seconds
        self.listen_key: str | None = None
        self.connected = False
        self.reconnects = 0
        self.last_error: str | None = None

    def url(self, listen_key: str) -> str:
        return f"{self._root}/private/ws?listenKey={listen_key}&events={EVENTS}"

    async def _listen_key(self, method: str) -> str | None:
        response = await self._rest.raw(
            method, "/fapi/v1/listenKey", {}, WEIGHT_LISTEN_KEY, auth="key"
        )
        if response.status_code >= 400:
            raise ConnectionError(f"listenKey {method}: HTTP {response.status_code}")
        body = response.json() if response.content else {}
        return body.get("listenKey") if isinstance(body, dict) else None

    async def _keepalive(self) -> None:
        while True:
            await self._sleep(KEEPALIVE_SECONDS)
            with contextlib.suppress(VenueNotSent, VenueTransportError, ConnectionError):
                await self._listen_key("PUT")

    async def run(self) -> None:
        backoff = 1.0
        while True:
            keepalive: asyncio.Task[None] | None = None
            try:
                self.listen_key = await self._listen_key("POST")
                if not self.listen_key:
                    raise ConnectionError("no listenKey in the response")
                async with self._ws_connect(self.url(self.listen_key)) as socket:
                    self.connected = True
                    self.last_error = None
                    backoff = 1.0
                    await self._on_connected()
                    keepalive = asyncio.create_task(self._keepalive(), name="listen-key-keepalive")
                    async for message in socket:
                        try:
                            payload = json.loads(message)
                        except ValueError:
                            continue
                        data = payload.get("data", payload) if isinstance(payload, dict) else None
                        if not isinstance(data, dict):
                            continue
                        if data.get("e") == "listenKeyExpired":
                            raise ConnectionError("listenKey expired")
                        self._on_event(data)
                raise ConnectionError("user stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                self.reconnects += 1
                log_event(
                    "binance_futures_user_stream_reconnecting",
                    level=logging.WARNING,
                    error=self.last_error,
                    backoff_seconds=backoff,
                )
            finally:
                self.connected = False
                if keepalive is not None:
                    keepalive.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await keepalive
            await self._sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def close(self) -> None:
        if self.listen_key:
            with contextlib.suppress(VenueNotSent, VenueTransportError, ConnectionError):
                await self._listen_key("DELETE")
