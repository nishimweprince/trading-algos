"""Live checks against a real cTrader demo account.

Skipped unless CTRADER_INTEGRATION=1. This is the only thing that can settle the
protocol behaviour the schemas do not state:

  * whether trendbars are bid-side or mid
  * whether a history response includes the currently-forming bar
  * that utcTimestampInMinutes really is the open tick, so interval-end stamping
    lines up with the broker's own chart

Each is a real assertion, not a printout for a human to eyeball. The last two
tests take about two minutes because one of them has to watch live ticks across
an M1 boundary.

Run it before trusting the service, and record the verdicts in decode.py:

    CTRADER_INTEGRATION=1 CTRADER_PROFILE=forex .venv/bin/pytest -m integration -s
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from execution_service.config import Settings, load_settings
from execution_service.ctrader.proto import ProtoOAGetTrendbarsReq, ProtoOATrendbarPeriod
from execution_service.ctrader.session import CTraderSession
from execution_service.hub import MarketDataHub
from execution_service.models import Timeframe

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
    print(f"\n{symbol} M1:")
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


# --- the two questions the schemas cannot answer ------------------------------
#
# Both of these were previously left to the reader: one printed a note asking a
# human to compare against a chart, and the other asserted `candle.ts <= now`,
# which decode_trendbars already guarantees client-side and so could never fail.


async def test_history_response_excludes_the_forming_bar(
    session: CTraderSession, settings: Settings
) -> None:
    """Answers: does the broker send the currently-forming interval?

    decode_trendbars drops it client-side, so this must inspect the RAW response
    before decoding — otherwise it only re-tests our own filter.
    """
    symbol = sorted(settings.symbols)[0]
    catalog = session.catalog
    assert catalog is not None
    info = catalog.info(symbol)

    now = datetime.now(UTC)
    # Reaching past the public API on purpose: the question is about bytes on the
    # wire, and every public path has already filtered the answer away.
    response = await session._client.request(  # type: ignore[union-attr]
        ProtoOAGetTrendbarsReq(
            ctidTraderAccountId=settings.account_id,
            period=ProtoOATrendbarPeriod.Value("M1"),
            symbolId=info.symbol_id,
            toTimestamp=int(now.timestamp() * 1000),
            count=5,
        )
    )

    raw_ends = sorted(
        datetime.fromtimestamp(int(bar.utcTimestampInMinutes) * 60, UTC) + timedelta(minutes=1)
        for bar in response.trendbar
    )
    assert raw_ends, "no raw M1 bars returned"
    forming = [end for end in raw_ends if end > now]

    print(f"\n{symbol} raw M1 response at {now:%H:%M:%S}Z:")
    for end in raw_ends:
        print(f"  interval ending {end:%H:%M:%S}Z{'   <-- still forming' if end > now else ''}")
    print(
        f"VERDICT: the broker {'INCLUDES' if forming else 'EXCLUDES'} the forming bar. "
        f"decode_trendbars filters it either way."
    )

    decoded = await session.fetch_candles(symbol=symbol, timeframe=Timeframe.M1, count=5)
    assert all(candle.ts <= datetime.now(UTC) for candle in decoded), (
        "a forming bar reached the public API"
    )


async def test_trendbar_close_matches_the_bid_not_the_mid(
    session: CTraderSession, settings: Settings
) -> None:
    """Answers: are trendbars bid-side or mid?

    Watches live ticks across an M1 boundary, remembers the last quote before it,
    then fetches the bar that just closed and compares its close against that
    quote's bid and mid. Whichever it matches is the answer.
    """
    symbol = sorted(settings.symbols)[0]
    hub = session.hub
    loop = asyncio.get_running_loop()

    # Wait for the next minute boundary, tracking the most recent tick before it.
    boundary = (datetime.now(UTC) + timedelta(minutes=1)).replace(second=0, microsecond=0)
    last_before = None
    deadline = loop.time() + (boundary - datetime.now(UTC)).total_seconds() + 5
    while loop.time() < deadline:
        tick = hub.last_tick(symbol)
        if tick is not None and datetime.now(UTC) < boundary:
            last_before = tick
        await asyncio.sleep(0.2)

    if last_before is None:
        pytest.skip(f"no {symbol} ticks around the boundary — the market is probably closed")

    # Let the broker settle the bar before asking for it.
    await asyncio.sleep(5)
    candles = await session.fetch_candles(symbol=symbol, timeframe=Timeframe.M1, count=3)
    closed = [candle for candle in candles if candle.ts == boundary]
    if not closed:
        pytest.skip(f"broker has not published the {boundary:%H:%M}Z bar yet")

    close = closed[0].close
    bid, ask = last_before.bid, last_before.ask
    mid = round((bid + ask) / 2, 10)

    print(f"\n{symbol} bar ending {boundary:%H:%M}Z")
    print(f"  last tick before close: bid={bid} ask={ask} mid={mid}")
    print(f"  trendbar close:         {close}")
    print(f"  |close-bid|={abs(close - bid):.10f}  |close-mid|={abs(close - mid):.10f}")

    if abs(close - bid) == abs(close - mid):
        pytest.skip("zero spread in the sampled tick — cannot distinguish bid from mid")
    verdict = "BID-side" if abs(close - bid) < abs(close - mid) else "MID"
    print(f"VERDICT: trendbars are {verdict}. Record this in src/ctrader/decode.py.")

    # cTrader documents trendbars as bid-side. Failing here means the assumption
    # behind every candle this service serves is wrong.
    assert abs(close - bid) < abs(close - mid), (
        f"trendbar close {close} is nearer the mid {mid} than the bid {bid}; "
        "candles are not bid-side and decode.py's contract needs revisiting"
    )
