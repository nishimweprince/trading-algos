"""The venue touch stream used to price demo-trading orders."""

from __future__ import annotations

import asyncio

from ta_plugin_binance_futures.factory import FACTORY
from ta_plugin_binance_futures.quotes import TouchQuotes
from ta_plugin_binance_futures.testing import (
    FakeFuturesStream,
    book_ticker_frame,
    settings,
    shutdown_frame,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_790_000_000_000_000_000

    def __call__(self) -> int:
        return self.now


async def no_sleep(_: float) -> None:
    await asyncio.sleep(0)


async def wait(predicate, seconds: float = 2.0) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.005)


async def test_latest_touch_per_symbol_and_staleness() -> None:
    stream = FakeFuturesStream(
        {
            "public": [
                [
                    book_ticker_frame("BTCUSDT", "60000.0", "1", "60000.1", "2"),
                    book_ticker_frame("ETHUSDT", "3000.00", "5", "3000.01", "5"),
                    book_ticker_frame("BTCUSDT", "60001.0", "1", "60001.1", "2"),
                ]
            ]
        }
    )
    clock = Clock()
    quotes = TouchQuotes(
        "wss://demo-fstream.binance.test/",
        ["BTCUSDT", "ETHUSDT"],
        ws_connect=stream,
        clock_ns=clock,
    )
    assert quotes.url == (
        "wss://demo-fstream.binance.test/public/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker"
    )
    await quotes.start()
    try:
        await wait(
            lambda: (
                quotes.quotes.get("BTCUSDT", None) is not None
                and quotes.quotes["BTCUSDT"].bid == 60001.0
            )
        )
        quote = quotes.get("BTCUSDT", max_age_ms=1000)
        assert quote is not None and quote.ask == 60001.1 and quote.mid == 60001.05
        assert quotes.get("ETHUSDT", max_age_ms=1000) is not None
        clock.now += 2_000_000_000
        assert quotes.get("BTCUSDT", max_age_ms=1000) is None  # stale
        assert quotes.get("SOLUSDT", max_age_ms=1000) is None
    finally:
        stream.release.set()
        await quotes.close()
    assert quotes.get("ETHUSDT", max_age_ms=1e12) is None  # not connected


async def test_reconnects_after_shutdown() -> None:
    stream = FakeFuturesStream(
        {
            "public": [
                [shutdown_frame()],
                [book_ticker_frame("BTCUSDT", "1.0", "1", "1.1", "1")],
            ]
        }
    )
    quotes = TouchQuotes("wss://x.test", ["BTCUSDT"], ws_connect=stream, sleep=no_sleep)
    await quotes.start()
    try:
        await wait(lambda: "BTCUSDT" in quotes.quotes)
        assert quotes.reconnects == 1 and quotes.connected
    finally:
        stream.release.set()
        await quotes.close()


def test_factory_builds_quotes_for_the_configured_symbols() -> None:
    quotes = FACTORY.quotes(settings(), "wss://demo-fstream.binance.test")
    assert quotes.symbols == ["BTCUSDT", "ETHUSDT"]
