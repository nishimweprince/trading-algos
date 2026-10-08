"""Live venue checks: ``pytest -m integration``. Public endpoints only; no key needed."""

from __future__ import annotations

import time

import httpx
import pytest

from poly_kalshi_service.windows import Discovery, window_open

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("asset", ["BTC", "ETH", "SOL", "XRP", "BNB"])
async def test_current_and_next_windows_are_identified_on_both_venues(asset: str) -> None:
    async with httpx.AsyncClient() as http:
        discovery = Discovery(
            http,
            gamma_url="https://gamma-api.polymarket.com",
            kalshi_rest_url="https://api.elections.kalshi.com/trade-api/v2",
        )
        now = window_open(time.time())
        for open_unix in (now, now + 900):
            w = await discovery.discover(asset, open_unix)
            assert w is not None, f"{asset} {open_unix} not listed on both venues"
            assert w.kalshi_ticker.startswith(f"KX{asset}15M-")
            assert w.yes_token != w.no_token
            assert w.fee_rate == pytest.approx(0.07)
        previous = await discovery.discover(asset, now - 900)
        assert previous is not None
        assert await discovery.kalshi_outcome(previous) in ("yes", "no", None)
