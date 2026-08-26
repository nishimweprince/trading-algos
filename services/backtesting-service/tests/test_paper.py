from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from ta_clients import CandleStore

from backtesting_service.config import Settings
from backtesting_service.engine import ClosedBarEngine
from backtesting_service.models import Candle
from backtesting_service.notifier import Notifier
from backtesting_service.paper import PaperTrader
from backtesting_service.sessions import build_windows


def _bar(
    ts: datetime, o: float = 2000.0, h: float = 2010.0, low: float = 2000.0, c: float = 2008.0
) -> Candle:
    return Candle(
        ts=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1.0,
        provider="test",
        source_instrument="XAUUSD",
    )


def _trader(tmp_path: Path) -> PaperTrader:
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        paper_enabled=True,
        orb_minutes=15,
        entry_delay_minutes=15,
    )
    engine = ClosedBarEngine(build_windows(["new_york"], {}), settings.engine_params())
    store = CandleStore(settings, client=None)  # type: ignore[arg-type]
    notifier = Notifier(settings, client=None)  # type: ignore[arg-type]
    return PaperTrader(settings, store, engine, notifier, settings.paper_state_path)


PRE = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC), o=2000, h=2001, low=1999, c=2000)
SIGNAL = _bar(datetime(2026, 1, 14, 13, 15, tzinfo=UTC), o=2000, h=2010, low=2000, c=2008)
FILL = _bar(datetime(2026, 1, 14, 13, 30, tzinfo=UTC), o=2009, h=2011, low=2008, c=2010)


@pytest.mark.asyncio
async def test_paper_first_tick_warms_without_entering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trader = _trader(tmp_path)

    async def fake_fetch(*_args: object, **_kwargs: object) -> list[Candle]:
        return [PRE, SIGNAL]

    monkeypatch.setattr(trader._store, "fetch_ctrader", fake_fetch)
    await trader.tick()
    assert trader.last_ts == SIGNAL.ts
    assert trader.engine.pairs == []
    assert trader.engine.pending == {}


@pytest.mark.asyncio
async def test_paper_new_bar_opens_at_most_one_pair_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trader = _trader(tmp_path)
    series = {1: [PRE], 2: [PRE, SIGNAL], 3: [PRE, SIGNAL, FILL], 4: [PRE, SIGNAL, FILL]}
    calls = {"n": 0}

    async def fetch(*_a: object, **_k: object) -> list[Candle]:
        calls["n"] += 1
        return series[min(calls["n"], 4)]

    monkeypatch.setattr(trader._store, "fetch_ctrader", fetch)
    await trader.tick()
    await trader.tick()
    assert "new_york" in trader.engine.pending
    await trader.tick()
    assert len(trader.engine.pairs) == 1
    await trader.tick()
    assert len(trader.engine.pairs) == 1
    assert len(trader.engine.pending) == 0


@pytest.mark.asyncio
async def test_paper_reload_skips_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trader = _trader(tmp_path)
    calls = {"n": 0}

    async def fetch(*_a: object, **_k: object) -> list[Candle]:
        calls["n"] += 1
        if calls["n"] == 1:
            return [PRE]
        if calls["n"] == 2:
            return [PRE, SIGNAL]
        return [PRE, SIGNAL, FILL]

    monkeypatch.setattr(trader._store, "fetch_ctrader", fetch)
    await trader.tick()
    await trader.tick()
    await trader.tick()
    assert len(trader.engine.pairs) == 1

    restored = PaperTrader(
        trader._s,
        trader._store,
        ClosedBarEngine(build_windows(["new_york"], {}), trader._s.engine_params()),
        trader._notifier,
        trader._state_path,
    )
    restored.load()
    assert restored.last_ts == FILL.ts
    await restored.tick()
    assert len(restored.engine.pairs) == 1
