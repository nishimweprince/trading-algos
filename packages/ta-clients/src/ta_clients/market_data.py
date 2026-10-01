"""Client for market-data-service: one market per client instance.

Everything is the neutral contract: ``Candle`` stamped at its UTC interval end,
``MarketQuote`` with bid/ask only when the provider really quotes both sides.
The caller never learns or cares which broker plugin answered.

``candles`` pages backwards on ``to`` past the service's per-request cap, so a
caller can ask for more history than one response carries.
"""

from __future__ import annotations

from datetime import datetime

import httpx
from ta_contracts import (
    TIMEFRAME_MINUTES,
    Candle,
    CandlesResponse,
    CapabilitiesResponse,
    InstrumentsResponse,
    MarketKind,
    MarketQuote,
    Timeframe,
)

from .candle_cache import filter_candles

__all__ = ["DEFAULT_PAGE_SIZE", "MarketDataClient"]

DEFAULT_PAGE_SIZE = 5000


class MarketDataClient:
    def __init__(
        self,
        base_url: str,
        *,
        market: MarketKind | str,
        api_key: str | None,
        client: httpx.AsyncClient,
        timeout_seconds: float = 30.0,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.market = MarketKind(market)
        self._base = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._client = client
        self._timeout = timeout_seconds
        self._page_size = page_size

    # --- reads --------------------------------------------------------------

    async def quote(self, symbol: str) -> MarketQuote:
        body = await self._get("tick", {"symbol": symbol})
        return MarketQuote.model_validate(body)

    async def instruments(self) -> InstrumentsResponse:
        return InstrumentsResponse.model_validate(await self._get("symbols", {}))

    async def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse.model_validate(await self._get("capabilities", {}))

    async def candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        count: int,
        to: datetime | None = None,
    ) -> list[Candle]:
        """Up to ``count`` closed bars ending at ``to`` (default now), oldest first."""
        collected: dict[datetime, Candle] = {}
        cursor = to
        while len(collected) < count:
            # `to` is inclusive, so a page after the first repeats the bar at the
            # cursor; ask for one more so the overlap does not cost a bar.
            overlap = 1 if cursor is not None else 0
            take = min(self._page_size, count - len(collected) + overlap)
            page = await self._candle_page(symbol, timeframe, take, cursor)
            new = [candle for candle in page if candle.ts not in collected]
            if not new:
                break
            collected.update((candle.ts, candle) for candle in new)
            oldest = min(collected)
            if cursor is not None and oldest >= cursor:
                break
            cursor = oldest
        ordered = sorted(collected.values(), key=lambda c: c.ts)
        return ordered[-count:]

    async def candles_range(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> list[Candle]:
        """Every closed bar in [date_from, date_to]; the latest page without date_from."""
        minutes = TIMEFRAME_MINUTES[timeframe]
        if date_from is None:
            count = self._page_size
        else:
            end = date_to or datetime.now(tz=date_from.tzinfo)
            span_minutes = max((end - date_from).total_seconds() / 60.0, minutes)
            count = int(span_minutes / minutes) + 8
        raw = await self.candles(symbol, timeframe, count=count, to=date_to)
        return filter_candles(raw, date_from=date_from, date_to=date_to)

    async def ready(self) -> tuple[bool, str]:
        """The service's own readiness, which includes quote staleness."""
        try:
            response = await self._client.get(f"{self._base}/health/ready", timeout=5.0)
        except httpx.HTTPError as exc:
            return False, str(exc)
        if response.status_code == 200:
            return True, "ok"
        return False, f"status {response.status_code}"

    # --- transport ----------------------------------------------------------

    async def _candle_page(
        self, symbol: str, timeframe: Timeframe, count: int, to: datetime | None
    ) -> list[Candle]:
        params: dict[str, str | int] = {
            "symbol": symbol,
            "timeframe": timeframe.value,
            "count": count,
        }
        if to is not None:
            params["to"] = to.isoformat()
        return list(CandlesResponse.model_validate(await self._get("candles", params)).candles)

    async def _get(self, route: str, params: dict[str, str | int]) -> object:
        response = await self._client.get(
            f"{self._base}/v1/{self.market.value}/{route}",
            params=params,
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()
