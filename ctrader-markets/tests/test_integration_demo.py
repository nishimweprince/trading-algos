"""Live checks against a real cTrader demo account.

Skipped unless CTRADER_INTEGRATION=1. This is the only thing that validates the
protocol behaviour that could not be settled from the schemas alone:

  * whether trendbars are bid-side or mid (compare the printed OHLC to a chart)
  * whether a history response includes the currently-forming bar
  * that utcTimestampInMinutes really is the open tick, so interval-end stamping
    lines up with the broker's own chart

Run it before trusting the service:

    CTRADER_INTEGRATION=1 CTRADER_PROFILE=forex .venv/bin/pytest -m integration -s
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from config import Settings, load_settings
from ctrader.session import CTraderSession
from hub import MarketDataHub
from models import Timeframe

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CTRADER_INTEGRATION") != "1",
        reason="set CTRADER_INTEGRATION=1 to run against a real demo account",
    ),
]

PROFILE = os.environ.get("CTRADER_PROFILE", "forex")


@pytest.fixture(scope="module")
def settings() -> Settings:
    loaded = load_settings(PROFILE)
    if loaded.environment != "demo":
        pytest.skip("refusing to run the integration suite against a live account")
    return loaded


@pytest.fixture
async def session(settings: Settings):
    session = CTraderSession(settings, MarketDataHub(queue_size=256))
    await session.start()
    connected = await session.wait_ready(timeout_seconds=45)
    if not connected:
        await session.close()
        pytest.fail("handshake did not complete in 45s — check credentials and CTRADER_ACCOUNT_ID")
    try:
        yield session
    finally:
        await session.close()


async def test_handshake_resolves_every_configured_symbol(
    session: CTraderSession, settings: Settings
) -> None:
    catalog = session.catalog
    assert catalog is not None
    assert set(catalog.names()) == set(settings.symbols)

    print("\nresolved catalog:")
    for entry in catalog.entries():
        print(f"  {entry.symbol:<24} id={entry.symbol_id:<8} digits={entry.digits}")


async def test_live_ticks_arrive(session: CTraderSession, settings: Settings) -> None:
    """Prices are printed so they can be compared against a cTrader chart."""
    hub = session.hub
    deadline = asyncio.get_running_loop().time() + 60

    while asyncio.get_running_loop().time() < deadline:
        if hub.known_symbols():
            break
        await asyncio.sleep(0.5)

    quotes = {symbol: hub.last_tick(symbol) for symbol in sorted(hub.known_symbols())}
    if not quotes:
        pytest.skip("no ticks in 60s — the market is probably closed")

    print("\nlive quotes:")
    for symbol, tick in quotes.items():
        assert tick is not None
        print(f"  {symbol:<24} bid={tick.bid:<12} ask={tick.ask:<12} spread={tick.spread}")
        assert tick.ask >= tick.bid, "ask must not be below bid"
        assert tick.ts.tzinfo is not None
        assert tick.ts < datetime.now(UTC) + timedelta(seconds=5), "server time is in the future"


async def test_trendbars_are_closed_and_stamped_at_the_interval_end(
    session: CTraderSession, settings: Settings
) -> None:
    symbol = sorted(settings.symbols)[0]
    now = datetime.now(UTC)

    candles = await session.fetch_candles(symbol=symbol, timeframe=Timeframe.M1, count=10)

    assert candles, "no M1 history returned"
    print(f"\n{symbol} M1 (compare against a cTrader chart to settle bid-vs-mid):")
    for candle in candles:
        print(
            f"  {candle.ts:%Y-%m-%d %H:%M}Z  O={candle.open} H={candle.high} "
            f"L={candle.low} C={candle.close} V={candle.volume}"
        )

    for candle in candles:
        assert candle.low <= candle.open <= candle.high
        assert candle.low <= candle.close <= candle.high
        assert candle.volume >= 0
        assert candle.provider == "ctrader"
        assert candle.source_instrument == symbol
        # Interval END: a closed M1 bar's stamp is never in the future.
        assert candle.ts <= now, f"{candle.ts} is not a closed bar"

    stamps = [candle.ts for candle in candles]
    assert stamps == sorted(stamps), "candles must be ascending"
    assert len(set(stamps)) == len(stamps), "duplicate timestamps"
    # M1 bars stamped at interval end land exactly on the minute.
    assert all(stamp.second == 0 and stamp.microsecond == 0 for stamp in stamps)


async def test_higher_timeframe_history_paginates(
    session: CTraderSession, settings: Settings
) -> None:
    """Exercises the hasMore loop, and the 5 req/s historical throttle with it."""
    symbol = sorted(settings.symbols)[0]

    candles = await session.fetch_candles(symbol=symbol, timeframe=Timeframe.H1, count=300)

    print(f"\n{symbol} H1: {len(candles)} bars, {candles[0].ts} .. {candles[-1].ts}")
    assert len(candles) > 1
    stamps = [candle.ts for candle in candles]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
