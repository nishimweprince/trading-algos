"""cTrader as a ``ta_plugin_api.MarketDataProvider``.

A thin face over ``CTraderGateway``: one gateway connects every account in the
registry, and each account alias is a feed. Quotes come from the per-account
hub the gateway already maintains from spot events; candles are trendbar
requests through the gateway's historical-rate throttle.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ta_contracts import Candle, InstrumentInfo, MarketQuote, SymbolInfo, Timeframe
from ta_core import ServiceError
from ta_plugin_api import MarketDataHub, ProviderCapabilities, SymbolResolutionError

from .decode import PERIOD_SECONDS
from .errors import CTraderError
from .gateway import CTraderGateway, GatewayAccount

__all__ = ["CTraderMarketData"]

CAPABILITIES = ProviderCapabilities(
    timeframes=tuple(tf for tf in Timeframe if tf.value in PERIOD_SECONDS),
    streaming=True,
    bid_ask=True,
)


class CTraderMarketData:
    name = "ctrader"

    def __init__(self, gateway: CTraderGateway) -> None:
        self._gateway = gateway

    @property
    def gateway(self) -> CTraderGateway:
        return self._gateway

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self._gateway.start()

    async def wait_ready(self, timeout_seconds: float) -> bool:
        return await self._gateway.wait_ready(timeout_seconds=timeout_seconds)

    async def close(self) -> None:
        await self._gateway.close()

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        return self._gateway.readiness()

    def feeds(self) -> frozenset[str | None]:
        return frozenset({None, *self._gateway.aliases()})

    # --- reads --------------------------------------------------------------

    def capabilities(self, feed: str | None) -> ProviderCapabilities:
        self._account(feed)
        return CAPABILITIES

    def instruments(self, feed: str | None) -> list[InstrumentInfo]:
        account = self._ready_account(feed)
        assert account.catalog is not None
        return [_instrument(entry, account) for entry in account.catalog.entries()]

    def resolve_symbols(self, feed: str | None, symbols: Iterable[str]) -> frozenset[str]:
        account = self._ready_account(feed)
        assert account.catalog is not None
        try:
            return account.catalog.resolve_many([symbol.upper() for symbol in symbols])
        except SymbolResolutionError as exc:
            raise ServiceError(422, "symbol_not_allowed", str(exc)) from exc

    async def quote(self, feed: str | None, symbol: str) -> MarketQuote:
        account = self._ready_account(feed)
        self._require_symbol(account, symbol)
        quote = account.hub.last_quote(symbol)
        if quote is None:
            raise ServiceError(503, "tick_unavailable", "No quote received for this instrument")
        return quote

    async def candles(
        self,
        feed: str | None,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        to: datetime | None = None,
    ) -> list[Candle]:
        account = self._ready_account(feed)
        self._require_symbol(account, symbol)
        try:
            candles = await self._gateway.fetch_candles(
                account_alias=account.definition.alias,
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
        return list(candles)

    def hub(self, feed: str | None) -> MarketDataHub:
        return self._account(feed).hub

    # --- helpers ------------------------------------------------------------

    def _account(self, feed: str | None) -> GatewayAccount:
        alias = feed or self._gateway.default_account_alias
        try:
            return self._gateway.account(alias)
        except KeyError as exc:
            raise ServiceError(
                422,
                "account_not_allowed",
                "Unknown or disabled account alias",
                {"account": alias, "configured": list(self._gateway.aliases())},
            ) from exc

    def _ready_account(self, feed: str | None) -> GatewayAccount:
        account = self._account(feed)
        if not self._gateway.account_ready(account.definition.alias):
            raise ServiceError(
                503,
                "broker_not_ready",
                "The account is not connected and loaded",
                {"account": account.definition.alias},
            )
        return account

    @staticmethod
    def _require_symbol(account: GatewayAccount, symbol: str) -> None:
        assert account.catalog is not None
        if symbol not in account.catalog:
            raise ServiceError(
                422,
                "symbol_not_allowed",
                "Unknown canonical instrument",
                {"symbol": symbol, "configured": list(account.catalog.names())},
            )


def _instrument(entry: SymbolInfo, account: GatewayAccount) -> InstrumentInfo:
    """cTrader volumes are in cents of a unit; lot_size converts them to lots."""
    lots = (lambda value: value / entry.lot_size) if entry.lot_size else (lambda _value: None)
    return InstrumentInfo(
        symbol=entry.symbol,
        source_instrument=account.definition.instruments.get(entry.symbol, entry.symbol),
        provider="ctrader",
        digits=entry.digits,
        description=entry.description,
        price_increment=10.0**-entry.digits,
        quantity_increment=lots(entry.step_volume) if entry.step_volume else None,
        min_quantity=lots(entry.min_volume) if entry.min_volume else None,
        max_quantity=lots(entry.max_volume) if entry.max_volume else None,
    )
