"""Adapt the MT5 gateway's legacy candles to closed UTC interval-end bars."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import httpx
from ta_clients import CandleStore
from ta_contracts import TIMEFRAME_MINUTES, Candle, LegacyCandlesResponse, Timeframe

from .config import Settings

MT5_LOOKBACK_LIMIT = 5000


class Mt5CandleError(ValueError):
    """Invalid or unsupported MT5 candle data/history request."""


class Mt5CandleStore(CandleStore):
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        super().__init__(settings, client)
        self.settings = settings

    def broker_symbol(self, symbol: str) -> str:
        if symbol == self.settings.symbol:
            return (
                self.settings.mt5_market_data_symbol or self.settings.mt5_execution_symbol or symbol
            )
        return symbol

    async def fetch_ctrader(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        count: int,
        to: datetime | None = None,
    ) -> list[Candle]:
        # Retain the shared store interface; this implementation uses MT5 only.
        if not 1 <= count <= MT5_LOOKBACK_LIMIT:
            raise Mt5CandleError("MT5 candle lookback must be between 1 and 5000 bars")
        if to is not None and (to.tzinfo is None or to.utcoffset() is None):
            raise Mt5CandleError("Candle history boundary must include a timezone")
        take = MT5_LOOKBACK_LIMIT if to is not None else min(count + 1, MT5_LOOKBACK_LIMIT)
        broker_symbol = self.broker_symbol(symbol)
        headers = {}
        if self.settings.mt5_signal_api_key is not None:
            headers["X-API-Key"] = self.settings.mt5_signal_api_key.get_secret_value()
        response = await self._client.get(
            f"{self.settings.mt5_signal_api_url.rstrip('/')}/v1/market-data/candles",
            params={"quote": broker_symbol, "timeframe": timeframe.value, "count": take},
            headers=headers,
            timeout=30.0,
        )
        response.raise_for_status()
        body = LegacyCandlesResponse.model_validate(response.json())
        if body.symbol != broker_symbol or body.timeframe != timeframe:
            raise Mt5CandleError("MT5 candle response does not match requested symbol/timeframe")
        now = datetime.now(UTC)
        duration = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        offset = self.settings.mt5_market_data_server_utc_offset_seconds
        closed: dict[datetime, Candle] = {}
        for bar in body.candles:
            if not all(
                math.isfinite(value) and value > 0
                for value in (bar.open, bar.high, bar.low, bar.close)
            ):
                raise Mt5CandleError("MT5 returned invalid candle prices")
            ts = datetime.fromtimestamp(bar.time - offset, UTC) + duration
            if ts > now:
                continue  # The legacy MT5 endpoint includes the current forming bar.
            closed[ts] = Candle(
                ts=ts,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                provider="mt5",
                source_instrument=broker_symbol,
            )
        if to is not None and closed and to < min(closed):
            raise Mt5CandleError(
                "MT5 history boundary is older than the latest 5000-bar window; "
                "use a local candle dataset for older history"
            )
        return sorted(
            (bar for ts, bar in closed.items() if to is None or ts <= to), key=lambda bar: bar.ts
        )[-count:]

    async def gateway_ready(self) -> tuple[bool, str]:
        try:
            bars = await self.fetch_ctrader(self.settings.symbol, self.settings.timeframe, count=1)
        except (httpx.HTTPError, ValueError) as exc:
            return False, f"HFM/MT5 candle feed unavailable: {exc}"
        return (True, "ok") if bars else (False, "MT5 returned no closed candles")


def create_candle_store(settings: Settings, client: httpx.AsyncClient) -> CandleStore:
    if settings.market_data_provider == "mt5":
        return Mt5CandleStore(settings, client)
    return CandleStore(settings, client)
