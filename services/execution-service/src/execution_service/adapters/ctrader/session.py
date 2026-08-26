"""The authenticated cTrader session: handshake, subscriptions, reconnect.

This is where dropping the Twisted client pays off. The reference sample drives
the handshake as a callback state machine keyed on response types; here it is
straight-line awaits, and reconnect is "throw the connection away and run the
same function again".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import UTC, datetime, timedelta

from google.protobuf.message import Message
from ta_contracts import Candle, Timeframe

from ...config import Settings
from ...errors import CTraderError, SymbolResolutionError
from ...hub import MarketDataHub
from ...logging_config import log_event
from .decode import decode_spot, decode_trendbars, period_duration
from .proto import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountsTokenInvalidatedEvent,
    ProtoOAApplicationAuthReq,
    ProtoOAClientDisconnectEvent,
    ProtoOAErrorRes,
    ProtoOAGetTrendbarsReq,
    ProtoOARefreshTokenReq,
    ProtoOASpotEvent,
    ProtoOASubscribeSpotsReq,
    ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq,
    ProtoOATrendbarPeriod,
)
from .protocol import Connector, CTraderProtocolClient, tls_connector
from .symbols import SymbolCatalog
from .tokens import TokenPair, TokenStore

# Broker error codes that mean "the access token is no longer usable". A refresh
# is attempted exactly once per connect attempt on these; anything else falls
# through to normal backoff.
TOKEN_ERROR_CODES = frozenset(
    {
        "CH_ACCESS_TOKEN_INVALID",
        "ACCESS_TOKEN_EXPIRED",
        "OA_AUTH_TOKEN_EXPIRED",
        "CH_ACCESS_TOKEN_EXPIRED",
    }
)


class CTraderSession:
    def __init__(
        self,
        settings: Settings,
        hub: MarketDataHub,
        *,
        connector: Connector | None = None,
    ) -> None:
        self._settings = settings
        self._hub = hub
        self._connector = connector or tls_connector(settings.resolved_host, settings.ctrader_port)
        self._tokens = TokenStore(
            settings.token_cache_path,
            fallback=TokenPair(
                access_token=settings.access_token.get_secret_value(),
                refresh_token=(
                    settings.refresh_token.get_secret_value() if settings.refresh_token else None
                ),
            ),
        )
        self._client: CTraderProtocolClient | None = None
        self._catalog: SymbolCatalog | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._refresher: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closing = False
        self._reconnects = 0
        self._historical_lock = asyncio.Lock()
        self._last_historical = 0.0
        # Strong references to in-flight close tasks. Without them the loop only
        # holds a weak reference and a task can be collected before it finishes.
        self._close_tasks: set[asyncio.Task[None]] = set()

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._tokens.load()
        self._closing = False
        self._supervisor = asyncio.create_task(self._supervise(), name="ctrader-supervisor")

    async def wait_ready(self, timeout_seconds: float) -> bool:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_seconds):
                await self._ready.wait()
        return self._ready.is_set()

    async def close(self) -> None:
        self._closing = True
        for task in (self._refresher, self._supervisor):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._refresher = None
        self._supervisor = None
        if self._client is not None:
            await self._client.close()
        self._ready.clear()
        self._hub.publish_status("stopped")

    @property
    def hub(self) -> MarketDataHub:
        """The hub this session publishes into.

        Exposed so an injected session and the API always read the same one; two
        hubs would leave every endpoint permanently empty.
        """
        return self._hub

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def catalog(self) -> SymbolCatalog | None:
        return self._catalog

    @property
    def reconnects(self) -> int:
        return self._reconnects

    # --- supervisor ---------------------------------------------------------

    async def _supervise(self) -> None:
        backoff = self._settings.reconnect_initial_backoff_seconds
        first = True
        while not self._closing:
            last_error: str | None = None
            try:
                if not first:
                    self._reconnects += 1
                first = False
                await self._connect_and_authenticate()
                self._ready.set()
                self._hub.publish_status("connected")
                log_event("ctrader_connected", profile=self._settings.profile)

                connected_at = asyncio.get_running_loop().time()
                error = await self._client.wait_closed()  # type: ignore[union-attr]
                uptime = asyncio.get_running_loop().time() - connected_at
                # Reset only once the connection proved it can stay up. Resetting
                # on a successful handshake alone turns a broker that accepts auth
                # and drops immediately into a permanent ~1s hot retry loop.
                if uptime >= self._settings.reconnect_stability_seconds:
                    backoff = self._settings.reconnect_initial_backoff_seconds
                if error is not None:
                    last_error = f"{type(error).__name__}: {error}"
                log_event(
                    "ctrader_connection_closed",
                    level=logging.WARNING,
                    reason=type(error).__name__ if error else "clean",
                    uptime_seconds=round(uptime, 3),
                )
            except asyncio.CancelledError:
                raise
            except SymbolResolutionError as exc:
                # Configuration is wrong, not the network, so retrying cannot fix
                # it. Keep retrying anyway — the broker's catalog is reloaded on
                # every connect, so a symbol enabled broker-side recovers without
                # a restart — but back off to the ceiling instead of hammering a
                # full auth + symbol-list round trip every second.
                last_error = str(exc)
                backoff = self._settings.reconnect_max_backoff_seconds
                log_event("symbol_resolution_failed", level=logging.ERROR, reason=str(exc))
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log_event(
                    "ctrader_connect_failed",
                    level=logging.ERROR,
                    reason=type(exc).__name__,
                    detail=str(exc),
                )
            finally:
                self._ready.clear()
                if self._client is not None:
                    await self._client.close()
                if not self._closing:
                    # Carrying the error is what populates `last_error` in
                    # /health/ready and the `error` key on the SSE status event.
                    self._hub.publish_status("reconnecting", error=last_error)

            if self._closing:
                break
            # Jitter inside the cap, not outside it: applying it afterwards lets
            # the sleep reach 1.5x RECONNECT_MAX_BACKOFF_SECONDS.
            delay = min(
                backoff * random.uniform(0.5, 1.5),  # noqa: S311
                self._settings.reconnect_max_backoff_seconds,
            )
            await self._sleep_backoff(delay)
            backoff = min(backoff * 2, self._settings.reconnect_max_backoff_seconds)

    @staticmethod
    async def _sleep_backoff(delay: float) -> None:
        """The backoff wait, as its own seam.

        Patching asyncio.sleep to observe the schedule would also capture the
        heartbeat's sleeps, which run at a fixed interval and hide the curve.
        """
        await asyncio.sleep(delay)

    async def _connect_and_authenticate(self) -> None:
        if self._client is not None:
            await self._client.close()

        client = CTraderProtocolClient(
            self._connector,
            on_event=self._on_event,
            request_timeout=self._settings.request_timeout_seconds,
            heartbeat_interval=self._settings.heartbeat_interval_seconds,
            connect_timeout=self._settings.connect_timeout_seconds,
        )
        self._client = client
        await client.connect()

        await client.request(
            ProtoOAApplicationAuthReq(
                clientId=self._settings.client_id.get_secret_value(),
                clientSecret=self._settings.client_secret.get_secret_value(),
            )
        )
        await self._authenticate_account()

        # Rebuilt on every connect: one code path, and it picks up broker-side
        # symbol changes for free. Published only once both requests succeed.
        self._catalog = await self._load_catalog()

        await client.request(
            ProtoOASubscribeSpotsReq(
                ctidTraderAccountId=self._settings.account_id,
                symbolId=self._catalog.ids(),
                subscribeToSpotTimestamp=True,
            )
        )

        self._restart_refresher()

    async def _authenticate_account(self) -> None:
        """Authenticate, refreshing the token at most once on an expiry error.

        The single-retry cap is deliberate: without it an expired refresh token
        turns into a refresh/auth loop hammering the broker.
        """
        assert self._client is not None
        if self._tokens.is_invalidated:
            # The broker already told us this token is dead; presenting it again
            # only works if it answers with one of TOKEN_ERROR_CODES.
            log_event("refreshing_invalidated_token", level=logging.WARNING)
            await self._refresh_token()
        try:
            await self._client.request(
                ProtoOAAccountAuthReq(
                    ctidTraderAccountId=self._settings.account_id,
                    accessToken=self._tokens.current.access_token,
                )
            )
            return
        except CTraderError as exc:
            if exc.error_code not in TOKEN_ERROR_CODES:
                raise
            log_event("access_token_rejected", level=logging.WARNING, error_code=exc.error_code)

        await self._refresh_token()
        await self._client.request(
            ProtoOAAccountAuthReq(
                ctidTraderAccountId=self._settings.account_id,
                accessToken=self._tokens.current.access_token,
            )
        )

    async def _refresh_token(self) -> None:
        assert self._client is not None
        refresh_token = self._tokens.current.refresh_token
        if not refresh_token:
            raise CTraderError(
                "NO_REFRESH_TOKEN",
                "The access token was rejected and no refresh token is configured. "
                "Redo the OAuth flow and set CTRADER_REFRESH_TOKEN.",
            )
        response = await self._client.request(ProtoOARefreshTokenReq(refreshToken=refresh_token))
        # The refresh token rotates: persisting the new pair is what keeps the
        # next restart from needing a manual OAuth round.
        self._tokens.record_refresh(
            access_token=response.accessToken,
            refresh_token=response.refreshToken,
            expires_in=response.expiresIn,
        )
        log_event("access_token_refreshed", expires_in=int(response.expiresIn))

    def _restart_refresher(self) -> None:
        if self._refresher is not None:
            self._refresher.cancel()
        self._refresher = asyncio.create_task(self._refresh_loop(), name="ctrader-token-refresh")

    async def _refresh_loop(self) -> None:
        """Refresh proactively at 80% of the token lifetime.

        Only runs when the broker told us an expiry; a token loaded from .env has
        no known lifetime and is handled reactively on rejection instead.
        """
        while True:
            delay = self._tokens.current.seconds_until_refresh()
            if delay is None:
                return
            await asyncio.sleep(delay)
            try:
                await self._refresh_token()
            except Exception as exc:
                log_event(
                    "proactive_token_refresh_failed",
                    level=logging.WARNING,
                    reason=type(exc).__name__,
                )
                return

    # --- inbound events -----------------------------------------------------

    def _on_event(self, message: Message) -> None:
        """Called from the reader loop. Must not block and must not raise."""
        try:
            if isinstance(message, ProtoOASpotEvent):
                self._handle_spot(message)
            elif isinstance(message, ProtoOAErrorRes):
                log_event(
                    "ctrader_server_error",
                    level=logging.WARNING,
                    error_code=message.errorCode,
                    description=message.description or None,
                )
            elif isinstance(message, ProtoOAAccountsTokenInvalidatedEvent):
                log_event(
                    "access_token_invalidated",
                    level=logging.WARNING,
                    reason=message.reason,
                )
                # Without this the supervisor reconnects and re-authenticates
                # with the same dead token, and only recovers if the broker
                # happens to answer with one of TOKEN_ERROR_CODES.
                self._tokens.invalidate()
                self._close_client_soon("token_invalidated")
            elif isinstance(message, ProtoOAClientDisconnectEvent):
                # The broker is dropping us on purpose. Nothing else notices:
                # wait_closed only fires once the socket actually goes away.
                log_event(
                    "ctrader_client_disconnected",
                    level=logging.WARNING,
                    reason=message.reason,
                )
                self._close_client_soon("client_disconnect")
        except Exception as exc:
            log_event(
                "event_handling_failed",
                level=logging.ERROR,
                console=False,
                reason=type(exc).__name__,
                message_type=type(message).__name__,
            )

    def _close_client_soon(self, reason: str) -> None:
        """Drop the connection from inside the reader loop, which cannot await.

        Closing is what hands control back to `_supervise`, which owns reconnect.
        """
        client = self._client
        if client is None:
            return
        task = asyncio.get_running_loop().create_task(client.close())
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)
        task.add_done_callback(lambda done: self._log_close_failure(done, reason))

    @staticmethod
    def _log_close_failure(task: asyncio.Task[None], reason: str) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log_event(
                "forced_close_failed",
                level=logging.ERROR,
                reason=reason,
                error=type(error).__name__,
            )

    def _handle_spot(self, event: ProtoOASpotEvent) -> None:
        catalog = self._catalog
        if catalog is None or not catalog.has_id(event.symbolId):
            return
        symbol = catalog.name_for_id(event.symbolId)
        tick = decode_spot(
            event,
            symbol=symbol,
            digits=catalog.digits_for_id(event.symbolId),
            previous=self._hub.last_tick(symbol),
        )
        if tick is not None:
            self._hub.publish_tick(tick)

    # --- historical ---------------------------------------------------------

    async def fetch_candles(
        self,
        *,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        to: datetime | None = None,
    ) -> tuple[Candle, ...]:
        """Fetch closed trendbars, paginating on hasMore.

        cTrader does not document the maximum span per bar period, so the loop
        walks backwards from `to` until it has `count` closed bars or the broker
        stops returning more.
        """
        if self._client is None or not self._ready.is_set():
            raise CTraderError("NOT_CONNECTED", "The broker session is not connected")
        catalog = self._catalog
        assert catalog is not None

        info = catalog.info(symbol)
        period = ProtoOATrendbarPeriod.Value(timeframe.value)
        end = to or datetime.now(UTC)

        collected: dict[datetime, Candle] = {}
        remaining = count
        while remaining > 0:
            await self._throttle_historical()
            response = await self._client.request(
                ProtoOAGetTrendbarsReq(
                    ctidTraderAccountId=self._settings.account_id,
                    period=period,
                    symbolId=info.symbol_id,
                    toTimestamp=int(end.timestamp() * 1000),
                    count=min(remaining + 1, 1000),
                )
            )
            candles = decode_trendbars(
                response.trendbar,
                symbol=symbol,
                period=timeframe.value,
                digits=info.digits,
            )
            if not candles:
                break
            for candle in candles:
                collected[candle.ts] = candle
            remaining = count - len(collected)
            if not response.hasMore or remaining <= 0:
                break
            oldest = min(collected)
            next_end = oldest - self._period_delta(timeframe)
            if next_end >= end:
                break
            end = next_end

        ordered = sorted(collected.values(), key=lambda candle: candle.ts)
        return tuple(ordered[-count:])

    @staticmethod
    def _period_delta(timeframe: Timeframe) -> timedelta:
        return period_duration(timeframe.value)

    async def _throttle_historical(self) -> None:
        """Token-bucket-lite over the documented 5 req/s historical limit."""
        interval = 1.0 / self._settings.historical_requests_per_second
        async with self._historical_lock:
            loop = asyncio.get_running_loop()
            wait = self._last_historical + interval - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_historical = loop.time()

    async def _load_catalog(self) -> SymbolCatalog:
        assert self._client is not None
        listed = await self._client.request(
            ProtoOASymbolsListReq(
                ctidTraderAccountId=self._settings.account_id,
                includeArchivedSymbols=False,
            )
        )
        # A list, not a set: protobuf messages are unhashable.
        wanted = [
            symbol
            for symbol in listed.symbol
            if str(symbol.symbolName).strip() in self._settings.symbols
        ]
        missing = self._settings.symbols - {str(s.symbolName).strip() for s in wanted}
        if missing:
            raise SymbolResolutionError(
                f"Broker does not expose these configured symbols: {sorted(missing)}. "
                "Run --discover-symbols and use the exact symbolName values."
            )

        # digits lives on ProtoOASymbol, not ProtoOALightSymbol, and prices
        # cannot be scaled without it — hence the second round trip.
        detailed = await self._client.request(
            ProtoOASymbolByIdReq(
                ctidTraderAccountId=self._settings.account_id,
                symbolId=[int(s.symbolId) for s in wanted],
            )
        )
        digits_by_id = {int(s.symbolId): int(s.digits) for s in detailed.symbol}

        catalog = SymbolCatalog.build(
            requested=sorted(self._settings.symbols),
            light_symbols=wanted,
            digits_by_id=digits_by_id,
        )
        log_event(
            "symbol_catalog_loaded",
            symbols=[
                {"symbol": e.symbol, "symbolId": e.symbol_id, "digits": e.digits}
                for e in catalog.entries()
            ],
        )
        return catalog
