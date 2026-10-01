from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Query
from sse_starlette.sse import EventSourceResponse
from ta_contracts import (
    CandlesResponse,
    CapabilitiesResponse,
    InstrumentsResponse,
    MarketQuote,
    Timeframe,
)
from ta_core import COMMON_ERRORS, ServiceError, create_base_app
from ta_core.logging_config import configure_file_logs, configure_logging, log_event
from ta_plugin_api import MarketDataProvider

from .config import Settings, load_settings
from .router import MarketRouter, build_providers
from .stream import SSE_HEADERS, tick_stream

LOGGER_NAME = "market_data_service.events"

MARKET_PATH = "/v1/{market}"
PROVIDER_QUERY = Query(
    default=None,
    min_length=1,
    max_length=32,
    description="Optional; must match the provider configured for this market.",
)


def parse_to_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ServiceError(422, "invalid_timestamp", "`to` must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def create_app(
    settings: Settings | None = None,
    providers: dict[str, MarketDataProvider] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level, name=LOGGER_NAME)
    configure_file_logs(settings.events_log_path)

    providers = providers if providers is not None else build_providers(settings)
    router = MarketRouter(settings.markets, providers)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for name, provider in providers.items():
            log_event("provider_starting", provider=name, profile=settings.profile)
            await provider.start()
        for name, provider in providers.items():
            ready = await provider.wait_ready(settings.startup_ready_timeout_seconds)
            log_event(
                "provider_ready" if ready else "provider_not_ready_at_startup",
                level=logging.INFO if ready else logging.WARNING,
                provider=name,
            )
        try:
            yield
        finally:
            log_event("service_stopping", profile=settings.profile)
            for provider in providers.values():
                await provider.close()

    def readiness() -> tuple[bool, dict[str, Any]]:
        """Every provider connected, and no market's newest quote stale."""
        now = datetime.now(UTC)
        ready = True
        details: dict[str, Any] = {"providers": {}, "markets": {}}
        for name, provider in providers.items():
            provider_ready, provider_details = provider.readiness()
            ready = ready and provider_ready
            details["providers"][name] = provider_details
        for route in router.routes():
            hub = route.provider.hub(route.feed)
            market = {"provider": route.provider.name, "feed": route.feed, **hub.snapshot(now)}
            age = hub.newest_quote_age_seconds(now)
            if age is not None and age > settings.tick_staleness_seconds:
                ready = False
                market["reason"] = (
                    f"newest quote is {age:.0f}s old, over the "
                    f"{settings.tick_staleness_seconds:.0f}s threshold"
                )
            details["markets"][route.market.value] = market
        return ready, details

    app, authenticate = create_base_app(
        settings,
        title="Market Data Service",
        version="0.1.0",
        lifespan=lifespan,
        readiness=readiness,
    )
    app.description = (
        "Provider-neutral quotes, closed UTC candles and tick streams per market "
        "(forex, deriv, crypto). Each market is served by one provider plugin; "
        "see GET /v1/{market}/capabilities."
    )
    app.state.settings = settings
    app.state.router = router
    auth = [Depends(authenticate)]

    @app.get(
        f"{MARKET_PATH}/tick",
        response_model=MarketQuote,
        responses=COMMON_ERRORS,
        dependencies=auth,
    )
    async def get_tick(
        market: str,
        symbol: str = Query(..., min_length=1, max_length=64),
        provider: str | None = PROVIDER_QUERY,
    ) -> MarketQuote:
        route = router.route(market, provider)
        return await route.provider.quote(route.feed, symbol)

    @app.get(
        f"{MARKET_PATH}/candles",
        response_model=CandlesResponse,
        responses=COMMON_ERRORS,
        dependencies=auth,
    )
    async def get_candles(
        market: str,
        symbol: str = Query(..., min_length=1, max_length=64),
        timeframe: Timeframe = Query(default=Timeframe.H1),  # noqa: B008
        count: int = Query(default=500, gt=0),
        to: str | None = Query(
            default=None,
            description="ISO-8601 upper bound. Defaults to now. Only closed bars are returned.",
        ),
        provider: str | None = PROVIDER_QUERY,
    ) -> CandlesResponse:
        route = router.route(market, provider)
        if count > settings.max_candles_lookback:
            raise ServiceError(
                422,
                "count_exceeds_limit",
                "count exceeds MAX_CANDLES_LOOKBACK",
                {"maximum": settings.max_candles_lookback},
            )
        supported = route.provider.capabilities(route.feed).timeframes
        if timeframe not in supported:
            raise ServiceError(
                422,
                "timeframe_not_supported",
                f"{route.provider.name} does not serve {timeframe.value}",
                {"supported": [tf.value for tf in supported]},
            )
        candles = await route.provider.candles(
            route.feed, symbol, timeframe, count, parse_to_timestamp(to)
        )
        return CandlesResponse(symbol=symbol, timeframe=timeframe, candles=candles)

    @app.get(
        f"{MARKET_PATH}/symbols",
        response_model=InstrumentsResponse,
        responses=COMMON_ERRORS,
        dependencies=auth,
    )
    async def list_symbols(
        market: str, provider: str | None = PROVIDER_QUERY
    ) -> InstrumentsResponse:
        route = router.route(market, provider)
        return InstrumentsResponse(
            market=route.market,
            provider=route.provider.name,
            instruments=route.provider.instruments(route.feed),
        )

    @app.get(
        f"{MARKET_PATH}/capabilities",
        response_model=CapabilitiesResponse,
        responses=COMMON_ERRORS,
        dependencies=auth,
    )
    async def capabilities(
        market: str, provider: str | None = PROVIDER_QUERY
    ) -> CapabilitiesResponse:
        route = router.route(market, provider)
        caps = route.provider.capabilities(route.feed)
        return CapabilitiesResponse(
            market=route.market,
            provider=route.provider.name,
            timeframes=list(caps.timeframes),
            streaming=caps.streaming,
            bid_ask=caps.bid_ask,
            max_candles=settings.max_candles_lookback,
        )

    @app.get(f"{MARKET_PATH}/stream/ticks", responses=COMMON_ERRORS, dependencies=auth)
    async def stream_ticks(
        market: str,
        symbols: str | None = Query(
            default=None,
            description="Comma-separated subset. Omit for every configured symbol.",
        ),
        provider: str | None = PROVIDER_QUERY,
    ) -> EventSourceResponse:
        route = router.route(market, provider)
        names = [value.strip() for value in (symbols or "").split(",") if value.strip()]
        # Resolved even when empty: it is also the readiness gate, so the
        # unfiltered stream cannot be accepted and then sit silent while the
        # broker is down.
        resolved = route.provider.resolve_symbols(route.feed, names)
        requested = resolved if names else None
        log_event(
            "stream_subscriber_opened",
            console=False,
            market=route.market.value,
            symbols=sorted(requested) if requested else None,
        )
        return EventSourceResponse(
            tick_stream(route.provider.hub(route.feed), requested),
            ping=int(settings.sse_keepalive_seconds),
            headers=SSE_HEADERS,
        )

    return app
