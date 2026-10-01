"""CandleStore against a settings object that is not backtesting-service's.

The point of the Protocol is that any service can supply its own configuration.
These use a plain dataclass with no pydantic and no service imports, which is
what proves the client actually came free of backtesting-service rather than
merely changing address.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from ta_contracts import Candle, Timeframe

from ta_clients import CandleStore


@dataclass
class FakeSettings:
    data_dir: Path
    ctrader_markets_url: str = "http://127.0.0.1:8010"
    ctrader_api_key: object = None

    def local_candles_path(self, symbol: str, timeframe: Timeframe | str) -> Path:
        tf = timeframe.value if isinstance(timeframe, Timeframe) else timeframe
        return self.data_dir / "candles" / symbol.upper() / f"{tf}.jsonl"


def _candles(n: int, start: datetime) -> list[Candle]:
    return [
        Candle(
            ts=start + timedelta(minutes=15 * i),
            open=1.0 + i,
            high=2.0 + i,
            low=0.5 + i,
            close=1.5 + i,
            volume=100 + i,
            source_instrument="XAUUSD",
        )
        for i in range(n)
    ]


@pytest.fixture
def store(tmp_path: Path) -> CandleStore:
    return CandleStore(FakeSettings(data_dir=tmp_path / "data"), httpx.AsyncClient())


def test_round_trips_through_the_jsonl_cache(store: CandleStore) -> None:
    written = _candles(5, datetime(2026, 1, 5, tzinfo=UTC))
    path = store.write_local("XAUUSD", Timeframe.M15, written)

    assert path.is_file()
    assert store.local_exists("XAUUSD", Timeframe.M15)
    assert [c.ts for c in store.load_local("XAUUSD", Timeframe.M15)] == [c.ts for c in written]


def test_write_local_sorts_by_timestamp(store: CandleStore) -> None:
    written = _candles(4, datetime(2026, 1, 5, tzinfo=UTC))
    store.write_local("XAUUSD", Timeframe.M15, list(reversed(written)))

    loaded = store.load_local("XAUUSD", Timeframe.M15)
    assert [c.ts for c in loaded] == sorted(c.ts for c in written)


def test_load_local_is_empty_when_nothing_was_seeded(store: CandleStore) -> None:
    assert store.load_local("XAUUSD", Timeframe.H1) == []
    assert store.local_exists("XAUUSD", Timeframe.H1) is False


def test_load_local_filters_by_date_range(store: CandleStore) -> None:
    start = datetime(2026, 1, 5, tzinfo=UTC)
    store.write_local("XAUUSD", Timeframe.M15, _candles(8, start))

    loaded = store.load_local(
        "XAUUSD",
        Timeframe.M15,
        date_from=start + timedelta(minutes=30),
        date_to=start + timedelta(minutes=75),
    )
    assert [c.ts for c in loaded] == [
        start + timedelta(minutes=30),
        start + timedelta(minutes=45),
        start + timedelta(minutes=60),
        start + timedelta(minutes=75),
    ]


def test_count_keeps_the_most_recent_bars(store: CandleStore) -> None:
    start = datetime(2026, 1, 5, tzinfo=UTC)
    store.write_local("XAUUSD", Timeframe.M15, _candles(10, start))

    loaded = store.load_local("XAUUSD", Timeframe.M15, count=3)
    assert len(loaded) == 3
    assert loaded[-1].ts == start + timedelta(minutes=135)


def test_symbol_is_upper_cased_in_the_cache_path(store: CandleStore) -> None:
    assert store.local_path("xauusd", Timeframe.M15).parent.name == "XAUUSD"


async def test_gateway_ready_reports_transport_failure_rather_than_raising(
    tmp_path: Path,
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    store = CandleStore(FakeSettings(data_dir=tmp_path / "data"), client)

    ready, reason = await store.gateway_ready()
    assert ready is False
    assert "refused" in reason
    await client.aclose()


async def test_gateway_ready_is_true_on_200(tmp_path: Path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    store = CandleStore(FakeSettings(data_dir=tmp_path / "data"), client)

    assert await store.gateway_ready() == (True, "ok")
    await client.aclose()
