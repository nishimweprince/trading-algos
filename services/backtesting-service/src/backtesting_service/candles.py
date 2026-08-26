"""ctrader-markets candle client plus a local JSONL cache."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx

from .config import Settings
from .models import TIMEFRAME_MINUTES, Candle, CandlesResponse, Timeframe

DEFAULT_PAGE_SIZE = 5000


class CandleStore:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._s = settings
        self._client = client

    def local_path(self, symbol: str, timeframe: Timeframe) -> Path:
        return self._s.local_candles_path(symbol, timeframe)

    def local_exists(self, symbol: str, timeframe: Timeframe) -> bool:
        path = self.local_path(symbol, timeframe)
        return path.is_file() and path.stat().st_size > 0

    def load_local(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        count: int | None = None,
    ) -> list[Candle]:
        path = self.local_path(symbol, timeframe)
        if not path.is_file():
            return []
        candles: list[Candle] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                candles.append(Candle.model_validate_json(line))
        return _filter(candles, date_from=date_from, date_to=date_to, count=count)

    def write_local(self, symbol: str, timeframe: Timeframe, candles: list[Candle]) -> Path:
        path = self.local_path(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(candles, key=lambda c: c.ts)
        with path.open("w", encoding="utf-8") as handle:
            for candle in ordered:
                handle.write(candle.model_dump_json() + "\n")
        return path

    async def fetch_ctrader(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        count: int,
        to: datetime | None = None,
    ) -> list[Candle]:
        """Fetch up to ``count`` closed bars, paging on ``to`` past the gateway cap."""
        collected: dict[datetime, Candle] = {}
        remaining = count
        cursor = to
        page_size = min(DEFAULT_PAGE_SIZE, count)
        while remaining > 0:
            take = min(page_size, remaining)
            page = await self._fetch_page(symbol, timeframe, take, cursor)
            if not page:
                break
            new = 0
            for candle in page:
                if candle.ts in collected:
                    continue
                collected[candle.ts] = candle
                new += 1
            if new == 0:
                break
            remaining = count - len(collected)
            oldest = min(collected)
            next_cursor = oldest
            if cursor is not None and next_cursor >= cursor:
                break
            cursor = next_cursor
        ordered = sorted(collected.values(), key=lambda c: c.ts)
        return ordered[-count:] if len(ordered) > count else ordered

    async def fetch_range(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> list[Candle]:
        minutes = TIMEFRAME_MINUTES[timeframe]
        if date_from is None:
            count = DEFAULT_PAGE_SIZE
        else:
            end = date_to or datetime.now(tz=date_from.tzinfo)
            span_minutes = max((end - date_from).total_seconds() / 60.0, minutes)
            count = int(span_minutes / minutes) + 8
        raw = await self.fetch_ctrader(symbol, timeframe, count=count, to=date_to)
        return _filter(raw, date_from=date_from, date_to=date_to, count=None)

    async def _fetch_page(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        to: datetime | None,
    ) -> list[Candle]:
        params: dict[str, str | int] = {
            "symbol": symbol,
            "timeframe": timeframe.value,
            "count": count,
        }
        if to is not None:
            params["to"] = to.isoformat()
        headers: dict[str, str] = {}
        if self._s.ctrader_api_key is not None:
            headers["X-API-Key"] = self._s.ctrader_api_key.get_secret_value()
        url = f"{self._s.ctrader_markets_url.rstrip('/')}/v1/market-data/candles"
        response = await self._client.get(url, params=params, headers=headers, timeout=30.0)
        response.raise_for_status()
        body = CandlesResponse.model_validate(response.json())
        return list(body.candles)

    async def gateway_ready(self) -> tuple[bool, str]:
        url = f"{self._s.ctrader_markets_url.rstrip('/')}/health/ready"
        try:
            response = await self._client.get(url, timeout=5.0)
        except httpx.HTTPError as exc:
            return False, str(exc)
        if response.status_code == 200:
            return True, "ok"
        return False, f"status {response.status_code}"


def _filter(
    candles: list[Candle],
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    count: int | None,
) -> list[Candle]:
    out = candles
    if date_from is not None:
        out = [c for c in out if c.ts >= date_from]
    if date_to is not None:
        out = [c for c in out if c.ts <= date_to]
    out = sorted(out, key=lambda c: c.ts)
    if count is not None and len(out) > count:
        out = out[-count:]
    return out
