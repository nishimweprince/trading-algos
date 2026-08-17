"""REST use-cases. Translates session/hub state into responses or ServiceError."""

from __future__ import annotations

from datetime import UTC, datetime

from config import Settings
from ctrader.decode import Clock, utc_now
from ctrader.gateway import CTraderGateway
from ctrader.session import CTraderSession
from errors import CTraderError, ServiceError, SymbolResolutionError
from hub import MarketDataHub
from models import CandlesResponse, SymbolsResponse, Tick, Timeframe


class MarketDataService:
    def __init__(
        self,
        settings: Settings,
        session: CTraderSession,
        hub: MarketDataHub,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._settings = settings
        self._session = session
        self._hub = hub
        self._clock = clock

    # --- reads ---------------------------------------------------------------

    def get_tick(self, symbol: str) -> Tick:
        self._require_ready()
        self._validate_symbol(symbol)
        tick = self._hub.last_tick(symbol)
        if tick is None:
            raise ServiceError(
                503,
                "tick_unavailable",
                "No quote has been received for this symbol yet",
                {"symbol": symbol},
            )
        return tick

    def list_symbols(self) -> SymbolsResponse:
        self._require_ready()
        catalog = self._session.catalog
        assert catalog is not None
        return SymbolsResponse(symbols=list(catalog.entries()))

    async def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        to: datetime | None = None,
    ) -> CandlesResponse:
        self._require_ready()
        self._validate_count(count)
        self._validate_symbol(symbol)
        if to is not None and (to.tzinfo is None or to.utcoffset() is None):
            raise ServiceError(422, "naive_timestamp", "`to` must include a timezone offset")

        try:
            candles = await self._session.fetch_candles(
                symbol=symbol, timeframe=timeframe, count=count, to=to
            )
        except CTraderError as exc:
            raise ServiceError(
                503,
                "candles_unavailable",
                "The broker did not return candle data",
                {"error_code": exc.error_code},
            ) from exc

        return CandlesResponse(symbol=symbol, timeframe=timeframe, candles=list(candles))

    def resolve_stream_symbols(self, symbols: str | None) -> frozenset[str] | None:
        """Parse and validate the SSE `symbols` filter. None means all.

        Readiness is checked before the filter is inspected so that an unfiltered
        stream and a filtered one agree: previously the unfiltered form was
        accepted while the broker was down and the filtered form returned 503.
        """
        self._require_ready()
        if symbols is None or not symbols.strip():
            return None
        requested = [token.strip() for token in symbols.split(",") if token.strip()]
        if not requested:
            return None
        catalog = self._session.catalog
        assert catalog is not None
        try:
            return catalog.resolve_many(requested)
        except SymbolResolutionError as exc:
            raise ServiceError(422, "symbol_not_allowed", str(exc)) from exc

    # --- health --------------------------------------------------------------

    def readiness(self) -> tuple[bool, dict[str, object]]:
        now = self._clock()
        details: dict[str, object] = {
            "profile": self._settings.profile,
            "environment": self._settings.environment,
            "connected": self._session.is_ready,
            "reconnects": self._session.reconnects,
            "symbols_configured": len(self._settings.symbols),
            **self._hub.snapshot(now),
        }

        if not self._session.is_ready:
            details["reason"] = "broker session is not connected"
            return False, details

        age = self._hub.newest_tick_age_seconds(now)
        if age is None:
            # Connected but nothing has ticked yet — normal at startup and over
            # a market close, so not a failure on its own.
            details["reason"] = "no quotes received yet"
            return True, details

        if age > self._settings.tick_staleness_seconds:
            details["reason"] = (
                f"newest quote is {age:.0f}s old, over the "
                f"{self._settings.tick_staleness_seconds:.0f}s threshold"
            )
            return False, details

        return True, details

    # --- guards --------------------------------------------------------------

    def _require_ready(self) -> None:
        if not self._session.is_ready or self._session.catalog is None:
            raise ServiceError(
                503,
                "broker_not_ready",
                "The broker session is not connected",
                {"state": self._hub.state},
            )

    def _validate_symbol(self, symbol: str) -> None:
        catalog = self._session.catalog
        assert catalog is not None
        if symbol not in catalog:
            raise ServiceError(
                422,
                "symbol_not_allowed",
                "The symbol is not in this profile's SYMBOLS",
                {"symbol": symbol, "configured": list(catalog.names())},
            )

    def _validate_count(self, count: int) -> None:
        if count > self._settings.max_candles_lookback:
            raise ServiceError(
                422,
                "count_exceeds_limit",
                "count exceeds MAX_CANDLES_LOOKBACK",
                {"maximum": self._settings.max_candles_lookback},
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


class GatewayMarketDataService:
    def __init__(self, settings: Settings, gateway: CTraderGateway) -> None:
        self._settings = settings
        self._gateway = gateway

    def resolve_account(self, alias: str | None) -> str:
        resolved = alias or self._gateway.default_account_alias
        try:
            self._gateway.account(resolved)
        except KeyError as exc:
            raise ServiceError(
                422,
                "account_not_allowed",
                "Unknown or disabled account alias",
                {"account": resolved, "configured": list(self._gateway.aliases())},
            ) from exc
        return resolved

    def get_tick(self, symbol: str, account: str | None = None) -> Tick:
        alias = self.resolve_account(account)
        state = self._gateway.account(alias)
        self._require_ready(alias)
        assert state.catalog is not None
        if symbol not in state.catalog:
            raise ServiceError(422, "symbol_not_allowed", "Unknown canonical instrument")
        tick = state.hub.last_tick(symbol)
        if tick is None:
            raise ServiceError(503, "tick_unavailable", "No quote received for this instrument")
        return tick

    def list_symbols(self, account: str | None = None) -> SymbolsResponse:
        alias = self.resolve_account(account)
        state = self._gateway.account(alias)
        self._require_ready(alias)
        assert state.catalog is not None
        return SymbolsResponse(symbols=list(state.catalog.entries()))

    async def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        to: datetime | None = None,
        account: str | None = None,
    ) -> CandlesResponse:
        alias = self.resolve_account(account)
        self._require_ready(alias)
        if count > self._settings.max_candles_lookback:
            raise ServiceError(422, "count_exceeds_limit", "count exceeds configured maximum")
        state = self._gateway.account(alias)
        assert state.catalog is not None
        if symbol not in state.catalog:
            raise ServiceError(422, "symbol_not_allowed", "Unknown canonical instrument")
        try:
            candles = await self._gateway.fetch_candles(
                account_alias=alias,
                symbol=symbol,
                timeframe=timeframe,
                count=count,
                to=to,
            )
        except CTraderError as exc:
            raise ServiceError(
                503,
                "candles_unavailable",
                "The broker did not return candle data",
                {"error_code": exc.error_code},
            ) from exc
        return CandlesResponse(symbol=symbol, timeframe=timeframe, candles=list(candles))

    def resolve_stream(
        self, symbols: str | None, account: str | None = None
    ) -> tuple[MarketDataHub, frozenset[str] | None]:
        alias = self.resolve_account(account)
        self._require_ready(alias)
        state = self._gateway.account(alias)
        assert state.catalog is not None
        if symbols is None or not symbols.strip():
            return state.hub, None
        requested = [value.strip().upper() for value in symbols.split(",") if value.strip()]
        try:
            return state.hub, state.catalog.resolve_many(requested)
        except SymbolResolutionError as exc:
            raise ServiceError(422, "symbol_not_allowed", str(exc)) from exc

    def readiness(self) -> tuple[bool, dict[str, object]]:
        ready, details = self._gateway.readiness()
        default = self._gateway.account(self._gateway.default_account_alias)
        now = datetime.now(UTC)
        details["default_market_data_account"] = default.definition.alias
        details["market_data"] = default.hub.snapshot(now)
        age = default.hub.newest_tick_age_seconds(now)
        if age is not None and age > self._settings.tick_staleness_seconds:
            ready = False
            details["reason"] = (
                f"default-account quote is {age:.0f}s old, over the "
                f"{self._settings.tick_staleness_seconds:.0f}s threshold"
            )
        return ready, details

    def _require_ready(self, alias: str) -> None:
        if not self._gateway.account_ready(alias):
            raise ServiceError(
                503,
                "broker_not_ready",
                "The target account is not connected and reconciled",
                {"account": alias},
            )
