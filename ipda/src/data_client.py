"""HTTP client for the 1-minute market-data endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .candles import Candle
from .config import Settings

_TIME_KEYS = ("start", "time", "timestamp", "t", "date", "datetime", "openTime")
_OPEN_KEYS = ("open", "o")
_HIGH_KEYS = ("high", "h")
_LOW_KEYS = ("low", "l")
_CLOSE_KEYS = ("close", "c")


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    raise KeyError(f"none of {keys} present in candle payload")


def _parse_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1_000_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _parse_time(int(text))
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    raise ValueError(f"unsupported timestamp value: {value!r}")


def parse_candles(payload: Any) -> list[Candle]:
    """Parse a variety of feed shapes into ``Candle`` objects (volume optional)."""
    rows: Any = payload
    if isinstance(payload, dict):
        for key in ("candles", "data", "results", "bars", "values"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    if not isinstance(rows, list):
        raise ValueError("candle payload is not a list")

    candles: list[Candle] = []
    for row in rows:
        if isinstance(row, dict):
            candles.append(
                Candle(
                    start=_parse_time(_first(row, _TIME_KEYS)),
                    open=float(_first(row, _OPEN_KEYS)),
                    high=float(_first(row, _HIGH_KEYS)),
                    low=float(_first(row, _LOW_KEYS)),
                    close=float(_first(row, _CLOSE_KEYS)),
                    volume=float(row.get("volume", row.get("vol", row.get("v", 0.0))) or 0.0),
                    closed=True,
                )
            )
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            candles.append(
                Candle(
                    start=_parse_time(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]) if len(row) > 5 and row[5] is not None else 0.0,
                    closed=True,
                )
            )
        else:
            raise ValueError(f"unsupported candle row shape: {row!r}")
    candles.sort(key=lambda c: c.start)
    return candles


@dataclass(slots=True)
class Tick:
    symbol: str
    bid: float
    ask: float


class MarketDataClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    def _headers(self) -> dict[str, str]:
        api_key = self._settings.data_api_key
        if api_key is None:
            return {}
        return {"X-API-Key": api_key.get_secret_value()}

    async def fetch_minute_candles(self, quote: str) -> list[Candle]:
        s = self._settings
        params = {
            s.data_quote_param: quote,
            s.data_count_param: str(s.data_lookback),
        }
        response = await self._client.get(
            s.data_api_url,
            params=params,
            headers=self._headers(),
            timeout=s.data_timeout_seconds,
        )
        response.raise_for_status()
        return parse_candles(response.json())

    async def fetch_tick(self, quote: str) -> Tick:
        """Current bid/ask from mt5-trader's ``GET /v1/market-data/tick``.

        The URL is derived from ``DATA_API_URL`` by swapping the ``/candles`` suffix,
        so the two endpoints cannot drift onto different hosts.
        """
        s = self._settings
        response = await self._client.get(
            _tick_url(s.data_api_url),
            params={s.data_quote_param: quote},
            headers=self._headers(),
            timeout=s.data_timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        return Tick(
            symbol=str(body.get("symbol", quote)),
            bid=float(body["bid"]),
            ask=float(body["ask"]),
        )


def _tick_url(candles_url: str) -> str:
    base = candles_url.rstrip("/")
    if base.endswith("/candles"):
        return f"{base[: -len('/candles')]}/tick"
    return f"{base}/tick"
