"""FuturesStreams end to end against scripted frames and the fake fapi."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from ta_core import ServiceError

from ta_plugin_binance_futures.depth import SyncState
from ta_plugin_binance_futures.rest import FapiRest
from ta_plugin_binance_futures.streams import (
    AggTrade,
    BookStatus,
    BookTick,
    DepthUpdate,
    FuturesStreams,
    Liquidation,
    MarkPrice,
    StreamStatus,
    parse_frame,
)
from ta_plugin_binance_futures.testing import (
    FakeBinanceFutures,
    FakeFuturesStream,
    agg_trade_frame,
    book_ticker_frame,
    depth_frame,
    force_order_frame,
    mark_price_frame,
    settings,
    shutdown_frame,
)


async def collect(streams: FuturesStreams, until, limit: int = 200) -> list:
    seen: list = []

    async def run() -> None:
        async for event in streams.events():
            seen.append(event)
            if until(seen) or len(seen) >= limit:
                return

    await asyncio.wait_for(run(), 2.0)
    return seen


@pytest.fixture
def fake() -> FakeBinanceFutures:
    return FakeBinanceFutures()


@pytest.fixture
async def rest(fake: FakeBinanceFutures) -> AsyncIterator[FapiRest]:
    config = settings()
    http = httpx.AsyncClient(
        base_url=config.binance_futures_rest_url, transport=httpx.MockTransport(fake.handler)
    )
    yield FapiRest(config, http=http)
    await http.aclose()


def make(stream: FakeFuturesStream, rest: FapiRest, raw: list | None = None, **overrides):
    clock = iter(range(1_000_000_000, 10**12, 1000))
    sink = (lambda channel, ns, text: raw.append((channel, ns, text))) if raw is not None else None
    return FuturesStreams(
        settings(**overrides),
        rest=rest,
        ws_connect=stream,
        clock_ns=lambda: next(clock),
        on_raw=sink,
    )


def test_parse_every_kind() -> None:
    assert isinstance(parse_frame(depth_frame("BTCUSDT", 1, 2, 0), 5), DepthUpdate)
    tick = parse_frame(book_ticker_frame("BTCUSDT", "1", "2", "3", "4"), 5)
    assert isinstance(tick, BookTick) and tick.bid_qty == 2.0 and tick.recv_ns == 5
    trade = parse_frame(agg_trade_frame("BTCUSDT", 7, "100", "0.5", True), 5)
    assert isinstance(trade, AggTrade) and trade.buyer_is_maker
    mark = parse_frame(mark_price_frame("BTCUSDT", "100", "0.0001", 123), 5)
    assert isinstance(mark, MarkPrice) and mark.next_funding_ms == 123
    liq = parse_frame(force_order_frame("BTCUSDT", "SELL", "99", "2"), 5)
    assert isinstance(liq, Liquidation) and liq.qty == 2.0
    assert parse_frame(shutdown_frame(), 5) == "shutdown"
    assert parse_frame("not json", 5) is None
    assert parse_frame('{"data": {"e": "depthUpdate"}}', 5) is None


def test_routes_streams_to_public_and_market(rest: FapiRest) -> None:
    streams = make(FakeFuturesStream({}), rest)
    public = streams.stream_url("public")
    market = streams.stream_url("market")
    assert public.startswith("wss://fstream.binance.test/public/stream?streams=")
    assert "btcusdt@depth@0ms" in public and "ethusdt@bookTicker" in public
    assert market.startswith("wss://fstream.binance.test/market/stream?streams=")
    assert "btcusdt@aggTrade" in market and "ethusdt@markPrice@1s" in market
    assert "btcusdt@forceOrder" in market
    assert "aggTrade" not in public


async def test_books_sync_from_snapshot_and_go_live(rest: FapiRest) -> None:
    stream = FakeFuturesStream(
        {
            "public": [
                [
                    depth_frame("BTCUSDT", 95, 99, 94),
                    depth_frame("BTCUSDT", 100, 105, 99, bids=[("60000.00", "1.2")]),
                    depth_frame("ETHUSDT", 499, 501, 498),
                ]
            ],
            "market": [[agg_trade_frame("BTCUSDT", 1, "60000.1", "0.2", False)]],
        }
    )
    raw: list = []
    streams = make(stream, rest, raw)
    await streams.start()
    seen = await collect(
        streams,
        lambda events: all(sync.verified for sync in streams.syncs.values()),
    )
    statuses = [e for e in seen if isinstance(e, BookStatus)]
    assert {s.symbol for s in statuses if s.state is SyncState.LIVE} == {"BTCUSDT", "ETHUSDT"}
    assert streams.syncs["BTCUSDT"].book.best_bid == (60000.0, 1.2)
    assert streams.syncs["BTCUSDT"].last_final_id == 105
    # The recorder saw the raw frames and both snapshots.
    channels = {channel for channel, _, _ in raw}
    assert {"public", "snapshot"} <= channels
    assert any("btcusdt@depthSnapshot" in text for _, _, text in raw)
    stream.release.set()
    await streams.close()


async def test_gap_triggers_resync(fake: FakeBinanceFutures, rest: FapiRest) -> None:
    fake.snapshot_ids["BTCUSDT"] = [100, 200]
    stream = FakeFuturesStream(
        {
            "public": [
                [
                    depth_frame("BTCUSDT", 100, 105, 99),
                    depth_frame("BTCUSDT", 150, 160, 149),  # pu != 105: gap
                    depth_frame("BTCUSDT", 161, 201, 160),  # bridges the second snapshot
                ]
            ],
        }
    )
    streams = make(stream, rest)
    streams.syncs.pop("ETHUSDT")
    await streams.start()

    def resynced(events: list) -> bool:
        sync = streams.syncs["BTCUSDT"]
        return sync.gaps == 1 and sync.verified

    seen = await collect(streams, resynced)
    # Depending on when the first snapshot lands, the break is seen either on
    # a live diff ("gap") or while replaying the buffer ("snapshot_gap").
    breaks = [e for e in seen if isinstance(e, BookStatus) and "gap" in e.reason]
    assert breaks and breaks[0].state is SyncState.SYNCING
    assert streams.syncs["BTCUSDT"].last_final_id == 201
    assert streams.syncs["BTCUSDT"].gaps == 1
    stream.release.set()
    await streams.close()


async def test_server_shutdown_reconnects_and_resets_books(rest: FapiRest) -> None:
    stream = FakeFuturesStream(
        {
            "public": [
                [depth_frame("BTCUSDT", 100, 105, 99), shutdown_frame()],
                [depth_frame("BTCUSDT", 100, 110, 99)],
            ],
        }
    )
    streams = make(stream, rest)
    streams.syncs.pop("ETHUSDT")
    await streams.start()

    def reconnected(events: list) -> bool:
        return (
            sum(
                1
                for e in events
                if isinstance(e, StreamStatus) and e.channel == "public" and e.state == "connected"
            )
            >= 2
            and streams.syncs["BTCUSDT"].verified
        )

    seen = await collect(streams, reconnected)
    states = [e.state for e in seen if isinstance(e, StreamStatus) and e.channel == "public"]
    assert "server_shutdown" in states and "reconnecting" in states
    assert any(isinstance(e, BookStatus) and e.reason == "reconnected" for e in seen)
    assert streams.reconnects["public"] >= 1
    assert sum(1 for url in stream.urls if "/public/" in url) >= 2
    stream.release.set()
    await streams.close()


async def test_readiness_needs_both_channels_and_verified_books(rest: FapiRest) -> None:
    streams = make(FakeFuturesStream({}), rest)
    ready, details = streams.readiness()
    assert not ready
    assert details["books"]["BTCUSDT"]["state"] == "syncing"


async def test_bad_symbol_refused_at_start(rest: FapiRest) -> None:
    streams = FuturesStreams(
        settings(binance_futures_symbols=("BTCUSDT_261225",)),
        rest=rest,
        ws_connect=FakeFuturesStream({}),
    )
    with pytest.raises(ServiceError):
        await streams.start()


def test_partial_frames_parse_as_snapshots_not_diffs() -> None:
    from ta_plugin_binance_futures.streams import BookSnapshot
    from ta_plugin_binance_futures.testing import partial_depth_frame

    event = parse_frame(
        partial_depth_frame("BTCUSDT", 20, 19, [("100.0", "1")], [("100.1", "2")], levels=5), 7
    )
    assert isinstance(event, BookSnapshot)
    assert (event.final_id, event.prev_final_id, event.recv_ns) == (20, 19, 7)
    # The diff stream keeps parsing as diffs.
    assert isinstance(parse_frame(depth_frame("BTCUSDT", 1, 2, 0), 5), DepthUpdate)


def test_partial_mode_subscriptions(rest: FapiRest) -> None:
    streams = make(FakeFuturesStream({}), rest, binance_futures_book_mode="partial")
    public = streams.stream_names("public")
    assert public == ["btcusdt@depth10@100ms", "ethusdt@depth10@100ms"]  # no bookTicker
    assert "btcusdt@aggTrade" in streams.stream_names("market")
    on = make(
        FakeFuturesStream({}),
        rest,
        binance_futures_book_mode="partial",
        binance_futures_book_ticker=True,
        binance_futures_agg_trades=False,
        binance_futures_partial_levels=5,
        binance_futures_partial_speed="500ms",
    )
    assert on.stream_names("public")[:2] == ["btcusdt@depth5@500ms", "btcusdt@bookTicker"]
    assert not any("aggTrade" in name for name in on.stream_names("market"))


async def test_partial_mode_goes_live_without_rest_snapshots(
    fake: FakeBinanceFutures, rest: FapiRest
) -> None:
    from ta_plugin_binance_futures.streams import BookSnapshot
    from ta_plugin_binance_futures.testing import partial_depth_frame

    bids, asks = [("60000.0", "1.2")], [("60000.1", "1.5")]
    stream = FakeFuturesStream(
        {
            "public": [
                [
                    partial_depth_frame("BTCUSDT", 10, 9, bids, asks),
                    partial_depth_frame("ETHUSDT", 50, 49, [("2500.00", "3")], [("2500.01", "4")]),
                    partial_depth_frame("BTCUSDT", 30, 25, [("60000.0", "2.0")], asks),  # skipped
                ]
            ]
        }
    )
    streams = make(stream, rest, binance_futures_book_mode="partial")
    await streams.start()
    seen = await collect(streams, lambda events: streams.syncs["BTCUSDT"].last_final_id == 30)
    assert streams.ready  # both routes connected, books live, no REST snapshot needed
    assert all(sync.verified for sync in streams.syncs.values())
    assert streams.syncs["BTCUSDT"].book.best_bid == (60000.0, 2.0)
    assert streams.syncs["BTCUSDT"].skipped == 1
    assert sum(isinstance(e, BookSnapshot) for e in seen) == 3
    assert not any(r.url.path == "/fapi/v1/depth" for r in fake.requests)
    _, details = streams.readiness()
    assert details["book_mode"] == "partial" and details["books"]["BTCUSDT"]["skipped"] == 1
    stream.release.set()
    await streams.close()
