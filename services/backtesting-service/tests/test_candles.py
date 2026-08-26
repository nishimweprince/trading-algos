from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from backtesting_service.candles import CandleStore
from backtesting_service.config import Settings
from backtesting_service.models import Candle, Timeframe

FIXTURE = Path(__file__).parent / "fixtures" / "xauusd_m15.jsonl"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", logs_dir=tmp_path / "logs")


def test_local_jsonl_round_trip(settings: Settings) -> None:
    store = CandleStore(settings, httpx.AsyncClient())
    candles = [
        Candle.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    path = store.write_local("XAUUSD", Timeframe.M15, candles)
    assert path.is_file()
    loaded = store.load_local("XAUUSD", Timeframe.M15)
    assert len(loaded) == len(candles)
    assert loaded[0].ts == candles[0].ts
    assert loaded[-1].close == candles[-1].close


@pytest.mark.asyncio
async def test_fetch_ctrader_pages_on_to(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = [
        [
            Candle(
                ts=datetime(2026, 1, 14, 13, 15, tzinfo=UTC),
                open=1,
                high=2,
                low=0.5,
                close=1.5,
                volume=1,
                source_instrument="XAUUSD",
            )
        ],
        [
            Candle(
                ts=datetime(2026, 1, 14, 13, 0, tzinfo=UTC),
                open=1,
                high=2,
                low=0.5,
                close=1.2,
                volume=1,
                source_instrument="XAUUSD",
            )
        ],
        [],
    ]

    async def fake_page(
        self: CandleStore,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        to: datetime | None,
    ) -> list[Candle]:
        return pages.pop(0) if pages else []

    monkeypatch.setattr(CandleStore, "_fetch_page", fake_page)
    store = CandleStore(settings, httpx.AsyncClient())
    candles = await store.fetch_ctrader("XAUUSD", Timeframe.M15, count=2)
    assert [c.ts for c in candles] == [
        datetime(2026, 1, 14, 13, 0, tzinfo=UTC),
        datetime(2026, 1, 14, 13, 15, tzinfo=UTC),
    ]
