from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from ta_contracts import Timeframe
from ta_core import ServiceError
from ta_plugin_api import MarketDataProvider
from ta_plugin_api.testing import assert_closed_utc_candles

from ta_plugin_mt5.market_data import MT5MarketData
from ta_plugin_mt5.terminal import ConnectionSnapshot, TickSnapshot
from ta_plugin_mt5.testing import FakeMT5Adapter

# Broker server runs at UTC+3, the usual EET-with-DST setting.
OFFSET = 3 * 3600
NOW = datetime(2026, 3, 2, 10, 7, 30, tzinfo=UTC)


def _settings(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    manifest = tmp_path / "symbols.json"
    manifest.write_text(
        json.dumps(
            [
                {"quote": "XAUUSD", "mt5_symbol": "XAUUSDb"},
                {"quote": "Volatility 75 Index"},
            ]
        ),
        encoding="utf-8",
    )
    values: dict[str, object] = {
        "symbols_file": manifest,
        "mt5_server_utc_offset_seconds": OFFSET,
        "mt5_quote_poll_seconds": 3600.0,
        "subscriber_queue_size": 16,
        "terminal_path": Path("C:/MT5/terminal64.exe"),
        "login": 1,
        "password": None,
        "server": "Broker-Demo",
        "mt5_timeout_ms": 1000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _server_epoch(utc: datetime) -> int:
    return int(utc.timestamp()) + OFFSET


def _rates(*bar_starts_utc: datetime) -> list[dict[str, object]]:
    return [
        {
            "time": _server_epoch(start),
            "open": 2000.0 + i,
            "high": 2001.0 + i,
            "low": 1999.0 + i,
            "close": 2000.5 + i,
            "volume": 10 + i,
        }
        for i, start in enumerate(bar_starts_utc)
    ]


@pytest.fixture
def adapter() -> FakeMT5Adapter:
    fake = FakeMT5Adapter()
    fake.symbol = fake.symbol.__class__(**{**fake.symbol.__dict__, "name": "XAUUSDb"})
    return fake


@pytest.fixture
async def provider(tmp_path: Path, adapter: FakeMT5Adapter):
    instance = MT5MarketData(adapter, _settings(tmp_path), clock=lambda: NOW)
    await instance.start()
    yield instance
    await instance.close()


def test_is_a_market_data_provider(tmp_path: Path, adapter: FakeMT5Adapter) -> None:
    assert isinstance(MT5MarketData(adapter, _settings(tmp_path)), MarketDataProvider)


def test_requires_a_symbols_manifest(tmp_path: Path, adapter: FakeMT5Adapter) -> None:
    with pytest.raises(ValueError, match="SYMBOLS_FILE"):
        MT5MarketData(adapter, _settings(tmp_path, symbols_file=None))


async def test_candles_are_utc_interval_ends_without_the_forming_bar(
    provider: MT5MarketData, adapter: FakeMT5Adapter
) -> None:
    starts = [datetime(2026, 3, 2, 9, 45, tzinfo=UTC) + timedelta(minutes=5 * i) for i in range(5)]
    adapter.rates = _rates(*starts)  # 09:45 … 10:05; the 10:05 bar ends 10:10 > NOW

    candles = await provider.candles(None, "XAUUSD", Timeframe.M5, count=3)

    assert [c.ts for c in candles] == [
        datetime(2026, 3, 2, 9, 55, tzinfo=UTC),
        datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
        datetime(2026, 3, 2, 10, 5, tzinfo=UTC),
    ]
    assert {c.provider for c in candles} == {"mt5"}
    assert {c.source_instrument for c in candles} == {"XAUUSDb"}
    assert_closed_utc_candles(candles, Timeframe.M5, now=NOW)
    # One extra row is requested so the dropped forming bar does not cost a bar.
    assert adapter.copy_rates_calls[-1] == ("XAUUSDb", 5, 4, None)


async def test_candles_before_to_pass_server_time_and_stop_at_to(
    provider: MT5MarketData, adapter: FakeMT5Adapter
) -> None:
    starts = [datetime(2026, 3, 2, 9, 0, tzinfo=UTC) + timedelta(minutes=5 * i) for i in range(12)]
    adapter.rates = _rates(*starts)
    to = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)

    candles = await provider.candles(None, "XAUUSD", Timeframe.M5, count=2, to=to)

    assert [c.ts for c in candles] == [
        datetime(2026, 3, 2, 9, 25, tzinfo=UTC),
        datetime(2026, 3, 2, 9, 30, tzinfo=UTC),
    ]
    assert adapter.copy_rates_calls[-1][3] == _server_epoch(to)


async def test_unsupported_timeframe_is_a_structured_422(provider: MT5MarketData) -> None:
    with pytest.raises(ServiceError) as raised:
        await provider.candles(None, "XAUUSD", Timeframe.M2, count=1)

    assert (raised.value.status_code, raised.value.code) == (422, "timeframe_not_supported")


async def test_unknown_symbol_is_rejected(provider: MT5MarketData) -> None:
    with pytest.raises(ServiceError) as raised:
        await provider.candles(None, "EURUSD", Timeframe.M5, count=1)

    assert raised.value.code == "symbol_not_allowed"


async def test_missing_rates_are_a_503(provider: MT5MarketData, adapter: FakeMT5Adapter) -> None:
    adapter.rates = None

    with pytest.raises(ServiceError) as raised:
        await provider.candles(None, "XAUUSD", Timeframe.M5, count=1)

    assert (raised.value.status_code, raised.value.code) == (503, "candles_unavailable")


async def test_nothing_is_served_before_the_terminal_initializes(
    tmp_path: Path, adapter: FakeMT5Adapter
) -> None:
    provider = MT5MarketData(adapter, _settings(tmp_path))

    with pytest.raises(ServiceError) as raised:
        await provider.quote(None, "XAUUSD")

    assert (raised.value.status_code, raised.value.code) == (503, "terminal_not_ready")


async def test_quote_maps_canonical_to_broker_and_shifts_server_time(
    provider: MT5MarketData, adapter: FakeMT5Adapter
) -> None:
    adapter.tick = TickSnapshot(bid=2000.0, ask=2000.4, time=_server_epoch(NOW))

    quote = await provider.quote(None, "XAUUSD")

    assert (quote.symbol, quote.source_instrument, quote.provider) == ("XAUUSD", "XAUUSDb", "mt5")
    assert quote.ts == NOW
    assert quote.price == pytest.approx(2000.2)


async def test_a_crossed_tick_is_unavailable(
    provider: MT5MarketData, adapter: FakeMT5Adapter
) -> None:
    adapter.tick = TickSnapshot(bid=2000.5, ask=2000.0, time=_server_epoch(NOW))

    with pytest.raises(ServiceError) as raised:
        await provider.quote(None, "XAUUSD")

    assert raised.value.code == "tick_unavailable"


async def test_poll_publishes_changed_quotes_only(
    provider: MT5MarketData, adapter: FakeMT5Adapter
) -> None:
    adapter.tick = TickSnapshot(bid=2000.0, ask=2000.4, time=_server_epoch(NOW))
    subscriber = provider.hub(None).register(None)

    provider._poll_once()
    provider._poll_once()

    events = []
    while not subscriber.queue.empty():
        events.append(subscriber.queue.get_nowait())
    ticks = [event for event in events if event.name == "tick"]
    # Both manifest symbols quote (the fake returns one tick for any symbol),
    # each exactly once despite two polls.
    assert sorted(event.payload["symbol"] for event in ticks) == ["Volatility 75 Index", "XAUUSD"]
    ready, details = provider.readiness()
    assert ready and details["connected"]


async def test_a_disconnected_terminal_is_not_ready(
    provider: MT5MarketData, adapter: FakeMT5Adapter
) -> None:
    adapter.connection = ConnectionSnapshot(False, None, False, False, reason="no connection")

    provider._poll_once()

    ready, details = provider.readiness()
    assert not ready
    assert details["reason"] == "no connection"
    assert provider.hub(None).state == "reconnecting"


async def test_warns_once_when_the_offset_looks_wrong(
    tmp_path: Path, adapter: FakeMT5Adapter, caplog: pytest.LogCaptureFixture
) -> None:
    # The terminal really runs at UTC+3, but the profile says UTC.
    adapter.tick = TickSnapshot(bid=1.0, ask=1.1, time=_server_epoch(NOW))
    provider = MT5MarketData(
        adapter, _settings(tmp_path, mt5_server_utc_offset_seconds=0), clock=lambda: NOW
    )
    await provider.start()
    try:
        with caplog.at_level("WARNING"):
            provider._poll_once()
            provider._poll_once()
    finally:
        await provider.close()

    assert [r.getMessage() for r in caplog.records].count("mt5_server_offset_suspect") == 1


async def test_instruments_describe_increments_and_limits(provider: MT5MarketData) -> None:
    provider._poll_once()

    instruments = {i.symbol: i for i in provider.instruments(None)}

    gold = instruments["XAUUSD"]
    assert (gold.source_instrument, gold.digits) == ("XAUUSDb", 5)
    assert gold.quantity_increment == 0.01 and gold.min_quantity == 0.01


def test_a_named_feed_is_rejected(tmp_path: Path, adapter: FakeMT5Adapter) -> None:
    provider = MT5MarketData(adapter, _settings(tmp_path))

    with pytest.raises(ServiceError, match="single feed"):
        provider.hub("forex_demo")


async def test_the_background_poller_publishes_on_the_event_loop(
    tmp_path: Path, adapter: FakeMT5Adapter
) -> None:
    """Terminal reads run in a thread; the hub's asyncio queues must still be
    fed from the loop thread, or a subscriber's queue is touched unsafely."""
    adapter.tick = TickSnapshot(bid=2000.0, ask=2000.4, time=_server_epoch(NOW))
    provider = MT5MarketData(
        adapter, _settings(tmp_path, mt5_quote_poll_seconds=0.01), clock=lambda: NOW
    )
    subscriber = provider.hub(None).register(frozenset({"XAUUSD"}))
    await provider.start()
    try:
        while True:
            event = await asyncio.wait_for(subscriber.queue.get(), timeout=2.0)
            if event.name == "tick":
                break
    finally:
        await provider.close()

    assert event.payload["symbol"] == "XAUUSD"
    assert event.payload["source_instrument"] == "XAUUSDb"
