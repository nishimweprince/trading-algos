"""The fapi REST client every part of the plugin shares.

One client means one weight budget: the market-data provider, the depth
resync and the signed account reads all spend from the same limiter, so a
snapshot storm during a resync cannot push the IP over Binance's limit.

Signed calls are GETs only. Nothing in this plugin can place, amend or cancel
an order; the key it is given should be read-only, and the code never needs
more.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr
from ta_core import ServiceError
from ta_core.logging_config import log_event

from .limiter import WeightLimiter

__all__ = ["DEPTH_WEIGHTS", "FapiRest", "depth_weight", "kline_weight"]

# GET /fapi/v1/depth weight by limit.
DEPTH_WEIGHTS = {5: 2, 10: 2, 20: 2, 50: 2, 100: 5, 500: 10, 1000: 20}


def depth_weight(limit: int) -> int:
    return DEPTH_WEIGHTS[limit]


def kline_weight(limit: int) -> int:
    """GET /fapi/v1/klines weight by limit."""
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


class FapiRest:
    def __init__(
        self,
        settings: Any,
        *,
        http: httpx.AsyncClient | None = None,
        limiter: WeightLimiter | None = None,
        wall_clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self._http = http or httpx.AsyncClient(
            base_url=settings.binance_futures_rest_url,
            timeout=settings.binance_futures_timeout_seconds,
        )
        self._owns_http = http is None
        self.limiter = limiter or WeightLimiter(settings.binance_futures_request_weight_per_minute)
        self._api_key: SecretStr | None = getattr(settings, "binance_futures_api_key", None)
        self._api_secret: SecretStr | None = getattr(settings, "binance_futures_api_secret", None)
        self._recv_window = getattr(settings, "binance_futures_recv_window_ms", 5000)
        self._wall_clock_ms = wall_clock_ms

    @property
    def can_sign(self) -> bool:
        return self._api_key is not None and self._api_secret is not None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def get(self, path: str, params: dict[str, Any], weight: int, *, unavailable: str) -> Any:
        return await self._send(path, params, weight, unavailable=unavailable, headers=None)

    async def signed_get(
        self, path: str, params: dict[str, Any], weight: int, *, unavailable: str
    ) -> Any:
        if not self.can_sign:
            raise ServiceError(
                503,
                unavailable,
                "BINANCE_FUTURES_API_KEY and BINANCE_FUTURES_API_SECRET are not configured",
            )
        assert self._api_key is not None and self._api_secret is not None
        signed = {**params, "recvWindow": self._recv_window, "timestamp": self._wall_clock_ms()}
        query = urlencode(signed)
        signature = hmac.new(
            self._api_secret.get_secret_value().encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        signed["signature"] = signature
        headers = {"X-MBX-APIKEY": self._api_key.get_secret_value()}
        return await self._send(path, signed, weight, unavailable=unavailable, headers=headers)

    async def _send(
        self,
        path: str,
        params: dict[str, Any],
        weight: int,
        *,
        unavailable: str,
        headers: dict[str, str] | None,
    ) -> Any:
        blocked = self.limiter.blocked_for()
        if blocked:
            raise ServiceError(
                503,
                unavailable,
                "Binance rate limit in force",
                {"retry_after_seconds": round(blocked, 1)},
            )
        await self.limiter.acquire(weight)
        try:
            response = await self._http.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            # The type only: a transport error's message can echo the request,
            # and a signed request carries the key in a header.
            raise ServiceError(
                503, unavailable, "Binance did not answer", {"reason": type(exc).__name__}
            ) from None
        used = response.headers.get("x-mbx-used-weight-1m")
        self.limiter.observe(int(used) if used and used.isdigit() else None)
        if response.status_code in {418, 429}:
            retry_after = float(response.headers.get("retry-after") or 60)
            self.limiter.block(retry_after)
            log_event(
                "binance_futures_rate_limited",
                level=logging.ERROR,
                status=response.status_code,
                retry_after_seconds=retry_after,
            )
            raise ServiceError(
                503,
                unavailable,
                "Binance rate limit exceeded",
                {"status": response.status_code, "retry_after_seconds": retry_after},
            )
        if response.status_code >= 400:
            raise ServiceError(
                503,
                unavailable,
                "Binance rejected the request",
                {"status": response.status_code, "path": path, **_error_body(response)},
            )
        return response.json()


def _error_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    if isinstance(body, dict):
        return {key: body[key] for key in ("code", "msg") if key in body}
    return {}
