"""Read-only OANDA v20 midpoint candle adapter."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.providers.base import Candle, CandlePage

OANDA_HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}
TIMEFRAME_DELTA = {"H1": timedelta(hours=1), "H4": timedelta(hours=4)}
MAX_CANDLES = 5000

JsonTransport = Callable[[str, dict[str, str], dict[str, str]], dict[str, Any]]


class OandaError(RuntimeError):
    """Safe, order-free OANDA market-data failure."""


def _default_transport(
    url: str, params: dict[str, str], headers: dict[str, str]
) -> dict[str, Any]:
    request = Request(f"{url}?{urlencode(params)}", headers=headers, method="GET")
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS hosts only
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise OandaError(f"OANDA GET failed ({exc.code}): {detail[:500]}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise OandaError(f"OANDA GET failed: {exc}") from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("OANDA request timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    # OANDA commonly emits nine fractional digits; datetime supports six.
    if "." in value:
        prefix, suffix = value.split(".", 1)
        fraction, zone = suffix.rstrip("Z"), "Z" if value.endswith("Z") else ""
        value = f"{prefix}.{fraction[:6]}{zone}"
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _validate_prices(open_: float, high: float, low: float, close: float) -> None:
    values = (open_, high, low, close)
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise OandaError(f"Invalid non-positive or non-finite OHLC: {values}")
    if high < low or not (low <= open_ <= high and low <= close <= high):
        raise OandaError(f"Invalid OHLC ordering: {values}")


class OandaV20Provider:
    """The adapter exposes market-data GETs only; it has no order methods."""

    name = "oanda_v20"

    def __init__(
        self,
        *,
        token: str,
        account_id: str,
        environment: str = "practice",
        transport: JsonTransport | None = None,
    ) -> None:
        if environment not in OANDA_HOSTS:
            raise ValueError("OANDA environment must be 'practice' or 'live'")
        if not token or not account_id:
            raise ValueError("OANDA token and account id are required")
        self.account_id = account_id
        self.host = OANDA_HOSTS[environment]
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._transport = transport or _default_transport

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        return self._transport(f"{self.host}{path}", params or {}, self._headers)

    def validate_instrument(self, instrument: str) -> None:
        payload = self._get(f"/v3/accounts/{self.account_id}/instruments")
        available = {
            str(item.get("name"))
            for item in payload.get("instruments", [])
            if isinstance(item, dict)
        }
        if instrument not in available:
            raise OandaError(
                f"Instrument {instrument!r} is unavailable to account {self.account_id!r}"
            )

    def fetch_candles_page(
        self,
        instrument: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> CandlePage:
        timeframe = timeframe.upper()
        if timeframe not in TIMEFRAME_DELTA:
            raise ValueError("OANDA sync supports H1 and H4 only")
        start, end = _utc(start), _utc(end)
        if start >= end:
            return CandlePage((), None)

        delta = TIMEFRAME_DELTA[timeframe]
        page_end = min(end, start + delta * MAX_CANDLES)
        payload = self._get(
            f"/v3/instruments/{instrument}/candles",
            {
                "price": "M",
                "granularity": timeframe,
                "from": _format_time(start),
                "to": _format_time(page_end),
                "includeFirst": "true",
                "alignmentTimezone": "UTC",
                "dailyAlignment": "0",
                "weeklyAlignment": "Monday",
            },
        )

        candles: list[Candle] = []
        raw_starts: list[datetime] = []
        for raw in payload.get("candles", []):
            if not raw.get("complete", False):
                continue
            midpoint = raw.get("mid")
            if not isinstance(midpoint, dict):
                raise OandaError("OANDA midpoint candle is missing 'mid' prices")
            raw_start = _parse_time(str(raw["time"]))
            values = tuple(float(midpoint[key]) for key in ("o", "h", "l", "c"))
            _validate_prices(*values)
            volume = float(raw.get("volume", 0))
            if not math.isfinite(volume) or volume < 0:
                raise OandaError(f"Invalid candle volume: {volume}")
            candles.append(
                Candle(
                    ts=raw_start + delta,
                    open=values[0],
                    high=values[1],
                    low=values[2],
                    close=values[3],
                    volume=volume,
                    provider=self.name,
                    source_instrument=instrument,
                )
            )
            raw_starts.append(raw_start)

        if raw_starts:
            candidate = max(raw_starts) + delta
        else:
            candidate = page_end
        next_start = candidate if candidate < end else None
        return CandlePage(tuple(candles), next_start)
