"""HTTP client for the 1-minute market-data endpoint.

The endpoint is polled by passing the ``quote`` and a lookback ``count``; it returns a
window of 1-minute OHLC candles. Because the exact wire schema was not fixed up front,
``parse_candles`` accepts the common shapes (list of objects or list of arrays) and the
usual field aliases / timestamp encodings. Adjust the alias tuples below if your feed
uses different keys.
"""

from __future__ import annotations

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
_VOLUME_KEYS = ("volume", "vol", "v")


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    raise KeyError(f"none of {keys} present in candle payload")


def _parse_time(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        # Heuristic: treat >1e12 as milliseconds, otherwise seconds.
        seconds = value / 1000.0 if value > 1_000_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _parse_time(int(text))
        # Accept trailing "Z" and naive ISO strings (assume UTC).
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
            # Array shape [time, open, high, low, close, (volume)].
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


class MarketDataClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def fetch_minute_candles(self, quote: str) -> list[Candle]:
        s = self._settings
        params = {
            s.data_quote_param: quote,
            s.data_count_param: str(s.data_lookback),
        }
        headers = {}
        if s.data_api_key is not None:
            headers["X-API-Key"] = s.data_api_key.get_secret_value()
        response = await self._client.get(
            s.data_api_url,
            params=params,
            headers=headers,
            timeout=s.data_timeout_seconds,
        )
        response.raise_for_status()
        return parse_candles(response.json())
