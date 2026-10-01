from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ta_contracts import MarketQuote

from ta_plugin_api.hub import MarketDataHub


def _quote(symbol: str = "EURUSD", bid: float = 1.0, ts: datetime | None = None) -> MarketQuote:
    return MarketQuote(
        symbol=symbol,
        source_instrument=symbol,
        provider="ctrader",
        bid=bid,
        ask=bid + 0.0001,
        ts=ts or datetime.now(UTC),
    )


def _drain(subscriber) -> list[dict]:
    events = []
    while not subscriber.queue.empty():
        events.append(subscriber.queue.get_nowait())
    return events


async def test_fan_out_to_every_subscriber() -> None:
    hub = MarketDataHub(queue_size=8)
    first = hub.register()
    second = hub.register()

    hub.publish_quote(_quote())

    assert len(_drain(first)) == 1
    assert len(_drain(second)) == 1


async def test_symbol_filter_is_honoured() -> None:
    hub = MarketDataHub(queue_size=8)
    filtered = hub.register(frozenset({"XAUUSD"}))
    everything = hub.register()

    hub.publish_quote(_quote("EURUSD"))

    assert _drain(filtered) == []
    assert len(_drain(everything)) == 1


async def test_overflow_drops_the_oldest_and_keeps_the_newest() -> None:
    """A lagging consumer should catch up to now, not replay a backlog."""
    hub = MarketDataHub(queue_size=4)
    subscriber = hub.register()

    for i in range(10):
        hub.publish_quote(_quote(bid=float(i)))

    events = _drain(subscriber)
    assert len(events) == 4
    assert [e.payload["bid"] for e in events] == [6.0, 7.0, 8.0, 9.0]
    assert subscriber.dropped == 6


async def test_publish_never_blocks_or_raises_on_a_full_queue() -> None:
    hub = MarketDataHub(queue_size=1)
    hub.register()

    for _ in range(100):
        hub.publish_quote(_quote())  # would raise or await if the policy were wrong


async def test_unregistered_subscriber_stops_receiving() -> None:
    hub = MarketDataHub(queue_size=4)
    subscriber = hub.register()

    hub.unregister(subscriber)
    hub.publish_quote(_quote())

    assert _drain(subscriber) == []
    assert hub.subscriber_count == 0


async def test_unregister_during_publish_is_safe() -> None:
    """The SSE generator's finally can fire mid-publish."""
    hub = MarketDataHub(queue_size=4)
    first = hub.register()
    hub.register()

    hub.unregister(first)
    hub.publish_quote(_quote())


async def test_last_quote_tracks_the_most_recent_per_symbol() -> None:
    hub = MarketDataHub(queue_size=4)

    hub.publish_quote(_quote("EURUSD", bid=1.0))
    hub.publish_quote(_quote("XAUUSD", bid=2.0))
    hub.publish_quote(_quote("EURUSD", bid=3.0))

    assert hub.last_quote("EURUSD").bid == 3.0
    assert hub.last_quote("XAUUSD").bid == 2.0
    assert hub.last_quote("GBPUSD") is None
    assert hub.known_symbols() == frozenset({"EURUSD", "XAUUSD"})


async def test_status_event_reports_state_and_drop_count() -> None:
    hub = MarketDataHub(queue_size=1)
    subscriber = hub.register()
    for _ in range(5):
        hub.publish_quote(_quote())

    hub.publish_status("reconnecting", error="ConnectionResetError")
    events = _drain(subscriber)

    status = [e for e in events if e.name == "status"][-1]
    assert status.payload["state"] == "reconnecting"
    assert status.payload["error"] == "ConnectionResetError"
    assert status.payload["dropped"] > 0


async def test_snapshot_reports_newest_quote_age() -> None:
    hub = MarketDataHub(queue_size=4)
    now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    hub.publish_quote(_quote(ts=now - timedelta(seconds=30)))

    snapshot = hub.snapshot(now)

    assert snapshot["newest_quote_age_seconds"] == 30.0
    assert snapshot["symbols_with_quotes"] == 1


async def test_snapshot_without_quotes_reports_no_age() -> None:
    snapshot = MarketDataHub(queue_size=4).snapshot(datetime.now(UTC))
    assert snapshot["newest_quote_age_seconds"] is None


async def test_a_stalled_consumer_does_not_block_the_publisher() -> None:
    """The load-bearing property: publish is called from the reader loop."""
    hub = MarketDataHub(queue_size=2)
    hub.register()  # never drained

    await asyncio.wait_for(
        asyncio.to_thread(lambda: [hub.publish_quote(_quote()) for _ in range(1000)]),
        timeout=5.0,
    )
