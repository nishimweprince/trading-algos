"""Resolve a request's market (and optional provider) to a provider and feed."""

from __future__ import annotations

from dataclasses import dataclass

from ta_contracts import MarketKind
from ta_core import ServiceError
from ta_plugin_api import MARKET_DATA_GROUP, MarketDataProvider, load_providers

from .config import MarketBinding, Settings

__all__ = ["MarketRouter", "Route", "build_providers"]


@dataclass(frozen=True)
class Route:
    market: MarketKind
    provider: MarketDataProvider
    feed: str | None


def build_providers(settings: Settings) -> dict[str, MarketDataProvider]:
    factories = load_providers(MARKET_DATA_GROUP, settings.providers)
    return {name: factory.market_data(settings) for name, factory in factories.items()}


class MarketRouter:
    def __init__(
        self,
        bindings: dict[MarketKind, MarketBinding],
        providers: dict[str, MarketDataProvider],
    ) -> None:
        for market, binding in bindings.items():
            provider = providers.get(binding.provider)
            if provider is None:
                raise ValueError(f"market {market.value}: provider {binding.provider} not loaded")
            if binding.feed not in provider.feeds():
                feeds = sorted(feed for feed in provider.feeds() if feed is not None)
                raise ValueError(
                    f"market {market.value}: {binding.provider} has no feed {binding.feed!r}; "
                    f"available: {', '.join(feeds) or '(single feed, omit it)'}"
                )
        self._routes = {
            market: Route(market, providers[binding.provider], binding.feed)
            for market, binding in bindings.items()
        }
        self.providers = providers

    def routes(self) -> list[Route]:
        return [self._routes[market] for market in sorted(self._routes)]

    def route(self, market: str, provider: str | None = None) -> Route:
        try:
            kind = MarketKind(market)
            route = self._routes[kind]
        except (ValueError, KeyError) as exc:
            raise ServiceError(
                404,
                "market_not_enabled",
                "This process does not serve that market",
                {"market": market, "enabled": [m.value for m in sorted(self._routes)]},
            ) from exc
        if provider is not None and provider != route.provider.name:
            raise ServiceError(
                422,
                "provider_not_available",
                "That provider does not serve this market here",
                {"requested": provider, "available": route.provider.name},
            )
        return route
