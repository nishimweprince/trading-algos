"""Window identity: which Kalshi ticker and which Polymarket tokens are window *n*.

Both venues list one 15-minute up/down binary per asset per window, aligned on
the same 900-second grid (checked 2026-10-06):

- Polymarket: Gamma event slug ``<asset>-updown-15m-<open_unix>``; the market's
  ``clobTokenIds`` are ``[Up, Down]`` = ``[YES, NO]``; ``feeSchedule`` gives
  the taker fee. Resolves on Chainlink's 60 s TWAP stream (Up if >= start).
- Kalshi: series ``KX<ASSET>15M``; the market whose ``close_time`` is
  ``open_unix + 900``. Listed ahead of time with status ``initialized``.
  Resolves on the 60 s average of CF Benchmarks' RTI. Kalshi closes every
  Thursday 03:00-05:00 ET (``/exchange/schedule``): no 15-minute markets
  exist for those eight windows and ``/exchange/status`` says
  ``trading_active: false`` (seen 2026-10-08).

``Discovery`` does the HTTP. The resulting ``Window`` is written to the tape as
a control line, so a replay knows exactly which ids were live when.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from ta_core.logging_config import log_event

from .decision import WINDOW_S
from .fees import DEFAULT_EXPONENT, DEFAULT_RATE

__all__ = ["USER_AGENT", "Discovery", "Window", "poly_slug", "window_open"]

# Gamma sits behind Cloudflare, which answers Python's default agent with 403.
USER_AGENT = "poly-kalshi/0.1 (+trading-algos)"
# Kalshi answered HTTP 429 three times during restarts (epoch 3): retry with
# jittered backoff, and query each series once per roll (current + next window).
RETRIES_429 = 4
BACKOFF_429_S = 0.5
KALSHI_CACHE_S = 6 * 3600


def window_open(unix_s: float) -> int:
    return int(unix_s // WINDOW_S) * WINDOW_S


def poly_slug(asset: str, open_unix: int) -> str:
    return f"{asset.lower()}-updown-15m-{open_unix}"


def _iso_to_unix(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).timestamp())


@dataclass(frozen=True, slots=True)
class Window:
    asset: str
    open_unix: int
    kalshi_ticker: str
    yes_token: str
    no_token: str
    condition_id: str
    fee_rate: float = DEFAULT_RATE
    fee_exponent: float = DEFAULT_EXPONENT

    @property
    def close_unix(self) -> int:
        return self.open_unix + WINDOW_S

    @property
    def ids(self) -> tuple[str, str, str]:
        return (self.kalshi_ticker, self.yes_token, self.no_token)

    def control(self) -> dict[str, Any]:
        return {"type": "window", **asdict(self)}

    @classmethod
    def from_control(cls, body: dict[str, Any]) -> Window:
        fields = {k: body[k] for k in cls.__dataclass_fields__ if k in body}
        return cls(**fields)


class DiscoveryError(RuntimeError):
    pass


class Discovery:
    def __init__(self, http: httpx.AsyncClient, *, gamma_url: str, kalshi_rest_url: str) -> None:
        self._http = http
        self._gamma = gamma_url.rstrip("/")
        self._kalshi = kalshi_rest_url.rstrip("/")
        # (asset, close_unix) -> market, from one series query per asset per roll.
        self._kalshi_markets: dict[tuple[str, int], dict[str, Any]] = {}
        self.retries_429 = 0

    async def _get(self, url: str, **params: Any) -> Any:
        for attempt in range(RETRIES_429 + 1):
            response = await self._http.get(
                url, params=params, headers={"User-Agent": USER_AGENT}, timeout=10.0
            )
            if response.status_code != 429 or attempt == RETRIES_429:
                break
            self.retries_429 += 1
            await asyncio.sleep(BACKOFF_429_S * 2**attempt * (1 + random.random()))
        response.raise_for_status()
        return response.json()

    async def poly_market(self, asset: str, open_unix: int) -> dict[str, Any] | None:
        events = await self._get(f"{self._gamma}/events", slug=poly_slug(asset, open_unix))
        if not events or not events[0].get("markets"):
            return None
        market = events[0]["markets"][0]
        start = market.get("eventStartTime")
        if start and _iso_to_unix(start) != open_unix:
            raise DiscoveryError(f"{asset} {open_unix}: Polymarket eventStartTime is {start}")
        return market

    async def kalshi_market(self, asset: str, open_unix: int) -> dict[str, Any] | None:
        close = open_unix + WINDOW_S
        market = self._kalshi_markets.get((asset, close))
        if market is None:
            # One query for this window and the next: the roll's second lookup is cached.
            body = await self._get(
                f"{self._kalshi}/markets",
                series_ticker=f"KX{asset}15M",
                min_close_ts=close - 1,
                max_close_ts=close + WINDOW_S + 1,
            )
            by_close: dict[int, list[dict[str, Any]]] = {}
            for m in body.get("markets") or []:
                if m.get("close_time"):
                    close_unix = _iso_to_unix(m["close_time"])
                elif m.get("open_time"):
                    close_unix = _iso_to_unix(m["open_time"]) + WINDOW_S
                else:
                    continue
                by_close.setdefault(close_unix, []).append(m)
            for close_unix, found in by_close.items():
                if len(found) > 1:
                    raise DiscoveryError(f"{asset} {close_unix}: {len(found)} Kalshi markets")
                self._kalshi_markets[(asset, close_unix)] = found[0]
            cutoff = close - KALSHI_CACHE_S
            for key in [k for k in self._kalshi_markets if k[1] < cutoff]:
                del self._kalshi_markets[key]
            market = self._kalshi_markets.get((asset, close))
        if market is None:
            return None
        if _iso_to_unix(market["open_time"]) != open_unix:
            raise DiscoveryError(f"{asset} {open_unix}: Kalshi open_time {market['open_time']}")
        return market

    async def discover(self, asset: str, open_unix: int) -> Window | None:
        """The window's identity, or None if a venue has not listed it yet."""
        poly = await self.poly_market(asset, open_unix)
        kalshi = await self.kalshi_market(asset, open_unix)
        if poly is None or kalshi is None:
            return None
        outcomes = [o.lower() for o in json.loads(poly["outcomes"])]
        tokens = json.loads(poly["clobTokenIds"])
        if outcomes != ["up", "down"] or len(tokens) != 2:
            raise DiscoveryError(f"{asset} {open_unix}: outcomes {outcomes}")
        fees = poly.get("feeSchedule") or {}
        if not poly.get("feesEnabled", True):
            fees = {"rate": 0.0}
        return Window(
            asset=asset,
            open_unix=open_unix,
            kalshi_ticker=kalshi["ticker"],
            yes_token=str(tokens[0]),
            no_token=str(tokens[1]),
            condition_id=poly["conditionId"],
            fee_rate=float(fees.get("rate", DEFAULT_RATE)),
            fee_exponent=float(fees.get("exponent", DEFAULT_EXPONENT)),
        )

    async def poly_outcome(self, window: Window) -> str | None:
        """``"yes"`` / ``"no"`` once Polymarket has resolved the window, else None."""
        market = await self.poly_market(window.asset, window.open_unix)
        if market is None or not market.get("closed"):
            return None
        prices = [float(p) for p in json.loads(market.get("outcomePrices") or "[]")]
        if prices == [1.0, 0.0]:
            return "yes"
        if prices == [0.0, 1.0]:
            return "no"
        if prices:
            log_event("poly_outcome_unclear", level=logging.WARNING, prices=prices)
        return None

    async def kalshi_trading_active(self) -> bool:
        """False while Kalshi is closed (its weekly Thursday 03:00-05:00 ET closure)."""
        body = await self._get(f"{self._kalshi}/exchange/status")
        return bool(body.get("trading_active", True))

    async def kalshi_outcome(self, window: Window) -> str | None:
        body = await self._get(f"{self._kalshi}/markets/{window.kalshi_ticker}")
        result = (body.get("market") or {}).get("result")
        return result if result in ("yes", "no") else None
