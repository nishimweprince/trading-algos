"""Capital.com REST market-data client with no trading surface."""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.providers.base import Candle

CAPITAL_HOSTS = {
    "demo": "https://demo-api-capital.backend-capital.com",
    "live": "https://api-capital.backend-capital.com",
}
HOUR = timedelta(hours=1)
MAX_PRICES = 1000


class CapitalError(RuntimeError):
    """A redacted authentication or market-data failure."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    payload: dict[str, Any]


Transport = Callable[
    [str, str, dict[str, str], dict[str, str], dict[str, Any] | None], HttpResponse
]


def _default_transport(
    method: str,
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
    body: dict[str, Any] | None,
) -> HttpResponse:
    target = f"{url}?{urlencode(params)}" if params else url
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {**headers, "Accept": "application/json"}
    if encoded is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(target, data=encoded, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed hosts
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
            return HttpResponse(
                int(response.status),
                {key.lower(): value for key, value in response.headers.items()},
                payload if isinstance(payload, dict) else {},
            )
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            payload = {}
        return HttpResponse(
            int(exc.code),
            {key.lower(): value for key, value in exc.headers.items()},
            payload if isinstance(payload, dict) else {},
        )
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CapitalError(f"Capital.com request failed: {type(exc).__name__}") from exc


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Capital.com timestamps must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _ohlc_side(raw: dict[str, Any], field: str, side: str) -> float:
    value = raw.get(field)
    if not isinstance(value, dict) or side not in value:
        raise CapitalError(f"Capital.com price is missing {field}.{side}")
    number = float(value[side])
    if not math.isfinite(number) or number <= 0:
        raise CapitalError(f"Capital.com price contains invalid {field}.{side}")
    return number


def _spread_observation(raw: dict[str, Any], bids: dict[str, float]) -> tuple[float | None, str]:
    """Prefer close spread, but tolerate a bad auxiliary close ask.

    Capital occasionally returns one inverted close ask on an otherwise valid
    bid candle. Bid OHLC is the durable price contract; ask is auxiliary spread
    evidence. In that case use the median of valid same-bar ask/bid differences
    and preserve how the value was obtained in provenance.
    """
    valid: dict[str, float] = {}
    for field, bid in bids.items():
        value = raw.get(field)
        ask_raw = value.get("ask") if isinstance(value, dict) else None
        if ask_raw is None:
            continue
        try:
            ask = float(ask_raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(ask) and ask > 0 and ask >= bid:
            valid[field] = ask - bid
    if "closePrice" in valid:
        return valid["closePrice"], "close"
    if valid:
        return float(statistics.median(valid.values())), "intrabar_median_fallback"
    return None, "unavailable"


class CapitalMarketDataClient:
    """Allowlisted Capital.com session and market-data operations only."""

    name = "capital"

    def __init__(
        self,
        *,
        api_key: str,
        identifier: str,
        api_password: str,
        environment: str = "demo",
        price_side: str = "bid",
        settlement_seconds: int = 90,
        transport: Transport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if environment not in CAPITAL_HOSTS:
            raise ValueError("Capital.com environment must be 'demo' or 'live'")
        if price_side != "bid":
            raise ValueError("The v1 Capital.com contract requires bid OHLC")
        if not api_key or not identifier or not api_password:
            raise ValueError("Capital.com API key, identifier, and API password are required")
        if settlement_seconds < 0:
            raise ValueError("settlement_seconds cannot be negative")
        self.host = CAPITAL_HOSTS[environment]
        self.environment = environment
        self.price_side = price_side
        self.settlement_seconds = settlement_seconds
        self._api_key = api_key
        self._identifier = identifier
        self._api_password = api_password
        self._transport = transport or _default_transport
        self._monotonic = monotonic
        self._sleep = sleep
        self._requests: deque[float] = deque()
        self._last_login: float | None = None
        self._last_authenticated_request: float | None = None
        self._cst: str | None = None
        self._security_token: str | None = None

    def _rate_limit(self, *, login: bool = False) -> None:
        now = self._monotonic()
        while self._requests and now - self._requests[0] >= 1.0:
            self._requests.popleft()
        if len(self._requests) >= 9:
            self._sleep(max(0.0, 1.0 - (now - self._requests[0])))
            now = self._monotonic()
            while self._requests and now - self._requests[0] >= 1.0:
                self._requests.popleft()
        if login and self._last_login is not None and now - self._last_login < 1.0:
            self._sleep(1.0 - (now - self._last_login))
            now = self._monotonic()
        self._requests.append(now)
        if login:
            self._last_login = now

    def _execute(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        login: bool = False,
    ) -> HttpResponse:
        for attempt in range(3):
            self._rate_limit(login=login)
            response = self._transport(
                method, f"{self.host}{path}", params or {}, headers or {}, body
            )
            if response.status not in {429, 500, 502, 503, 504}:
                return response
            if attempt < 2:
                self._sleep(0.25 * (2**attempt))
        raise CapitalError(
            f"Capital.com market-data request failed with status {response.status}",
            status=response.status,
        )

    def start_session(self) -> None:
        response = self._execute(
            "POST",
            "/api/v1/session",
            headers={"X-CAP-API-KEY": self._api_key},
            body={
                "identifier": self._identifier,
                "password": self._api_password,
                "encryptedPassword": False,
            },
            login=True,
        )
        if response.status != 200:
            raise CapitalError(
                f"Capital.com session authentication failed with status {response.status}",
                status=response.status,
            )
        self._cst = response.headers.get("cst")
        self._security_token = response.headers.get("x-security-token")
        if not self._cst or not self._security_token:
            raise CapitalError("Capital.com session response omitted authorization headers")
        self._last_authenticated_request = self._monotonic()

    def _auth_headers(self) -> dict[str, str]:
        now = self._monotonic()
        if (
            not self._cst
            or not self._security_token
            or self._last_authenticated_request is None
            or now - self._last_authenticated_request >= 9 * 60
        ):
            self.start_session()
        return {"CST": self._cst or "", "X-SECURITY-TOKEN": self._security_token or ""}

    def _get_authenticated(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = self._execute("GET", path, params=params, headers=self._auth_headers())
        if response.status == 401:
            self._cst = self._security_token = None
            response = self._execute("GET", path, params=params, headers=self._auth_headers())
        if response.status != 200:
            raise CapitalError(
                f"Capital.com market-data request failed with status {response.status}",
                status=response.status,
            )
        self._last_authenticated_request = self._monotonic()
        return response.payload

    def check_session(self) -> dict[str, str]:
        payload = self._get_authenticated("/api/v1/ping")
        return {
            "status": str(payload.get("status", "OK")),
            "environment": self.environment,
        }

    def server_time(self) -> datetime:
        response = self._execute("GET", "/api/v1/time")
        if response.status != 200 or "serverTime" not in response.payload:
            raise CapitalError("Capital.com server-time request failed", status=response.status)
        return datetime.fromtimestamp(float(response.payload["serverTime"]) / 1000.0, tz=UTC)

    def search_markets(self, term: str) -> list[dict[str, Any]]:
        payload = self._get_authenticated("/api/v1/markets", {"searchTerm": term})
        markets = payload.get("markets", [])
        if not isinstance(markets, list):
            raise CapitalError("Capital.com markets response is malformed")
        return [
            {
                "epic": str(item.get("epic", "")),
                "instrument_name": str(item.get("instrumentName", "")),
                "market_status": str(item.get("marketStatus", "")),
            }
            for item in markets
            if isinstance(item, dict) and item.get("epic")
        ]

    def validate_market(self, epic: str) -> dict[str, Any]:
        matches = [item for item in self.search_markets(epic) if item["epic"] == epic]
        if not matches:
            raise CapitalError(f"Capital.com epic {epic!r} is unavailable to this account")
        return matches[0]

    def fetch_closed_hourly(
        self,
        epic: str,
        start: datetime,
        end: datetime,
        *,
        server_time: datetime | None = None,
        max_bars: int = MAX_PRICES,
    ) -> tuple[Candle, ...]:
        if not 1 <= max_bars <= MAX_PRICES:
            raise ValueError(f"max_bars must be between 1 and {MAX_PRICES}")
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("Capital.com price boundaries must be ordered and timezone-aware")
        payload = self._get_authenticated(
            f"/api/v1/prices/{epic}",
            {
                "resolution": "HOUR",
                "max": str(max_bars),
                "from": _format_utc(start),
                "to": _format_utc(end),
            },
        )
        cutoff = (server_time or self.server_time()).astimezone(UTC) - timedelta(
            seconds=self.settlement_seconds
        )
        prices = payload.get("prices", [])
        if not isinstance(prices, list):
            raise CapitalError("Capital.com prices response is malformed")
        candles: list[Candle] = []
        seen: set[datetime] = set()
        for raw in prices:
            if not isinstance(raw, dict) or "snapshotTimeUTC" not in raw:
                raise CapitalError("Capital.com price omitted snapshotTimeUTC")
            ts = _parse_utc(str(raw["snapshotTimeUTC"])) + HOUR
            if ts >= cutoff:
                continue
            if ts in seen:
                raise CapitalError(f"Capital.com returned duplicate candle {ts.isoformat()}")
            seen.add(ts)
            price_fields = ("openPrice", "highPrice", "lowPrice", "closePrice")
            values = tuple(_ohlc_side(raw, name, self.price_side) for name in price_fields)
            open_, high, low, close = values
            if high < low or not (low <= open_ <= high and low <= close <= high):
                raise CapitalError(f"Capital.com returned invalid OHLC at {ts.isoformat()}")
            spread, spread_source = _spread_observation(
                raw, dict(zip(price_fields, values, strict=True))
            )
            volume = float(raw.get("lastTradedVolume") or 0.0)
            if not math.isfinite(volume) or volume < 0:
                raise CapitalError(f"Capital.com returned invalid volume at {ts.isoformat()}")
            candles.append(
                Candle(
                    ts,
                    open_,
                    high,
                    low,
                    close,
                    volume,
                    self.name,
                    epic,
                    spread,
                    spread_source,
                )
            )
        return tuple(sorted(candles, key=lambda candle: candle.ts))
