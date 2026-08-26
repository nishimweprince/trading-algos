"""Live-readiness: bounded paper state, snapshot migration, observability, execution gating."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from backtesting_service.candles import CandleStore
from backtesting_service.config import Settings
from backtesting_service.engine import ClosedBarEngine, Pair, infer_primary_side
from backtesting_service.models import Candle, EngineParams, ExecutionMode, Timeframe
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


def _engine() -> ClosedBarEngine:
    return ClosedBarEngine(build_windows(["new_york"], {}), EngineParams())


def test_infer_primary_side_from_the_closed_leg() -> None:
    assert infer_primary_side(None, long_open=False, short_open=True) == "long"
    assert infer_primary_side("short", long_open=True, short_open=False) == "short"
    assert infer_primary_side(None, long_open=True, short_open=True) is None


def test_restore_infers_unknown_primary_when_one_leg_is_closed() -> None:
    engine = _engine()
    engine.pairs.append(
        Pair(
            id="new_york:survivor",
            session="new_york",
            entry=2000,
            sl_dist=4,
            long_sl=1996,
            long_tp=2012,
            short_sl=2004,
            short_tp=1988,
            primary_side="long",
            long_open=False,
            short_open=True,
            locked=True,
            entry_ts=datetime(2026, 1, 14, 13, 30, tzinfo=UTC),
        )
    )
    snapshot = engine.snapshot()
    assert snapshot["schema_version"] == 1
    del snapshot["pairs"][0]["primary_side"]  # type: ignore[index]
    restored = _engine()
    restored.restore(snapshot)
    assert restored.pairs[0].primary_side == "long"


def test_non_positive_stop_emits_an_event_and_counter() -> None:
    engine = _engine()
    ts = datetime(2026, 1, 14, 13, 30, tzinfo=UTC)
    assert engine._sized_stop(0.0, "new_york", ts) is None
    assert engine.non_positive_stop_count == 1
    assert engine.events[-1].kind == "signal_skipped_non_positive_stop"
    report = engine.report("XAUUSD", Timeframe.M15, "local")
    assert report.non_positive_stop_count == 1


def test_prune_closed_history_keeps_open_pairs() -> None:
    engine = _engine()
    ts = datetime(2026, 1, 14, 13, 30, tzinfo=UTC)
    for index in range(3):
        engine.pairs.append(
            Pair(
                id=f"new_york:{index}",
                session="new_york",
                entry=2000,
                sl_dist=4,
                long_sl=1996,
                long_tp=2012,
                short_sl=2004,
                short_tp=1988,
                long_open=False,
                short_open=False,
                entry_ts=ts,
            )
        )
    engine.pairs.append(
        Pair(
            id="new_york:open",
            session="new_york",
            entry=2000,
            sl_dist=4,
            long_sl=1996,
            long_tp=2012,
            short_sl=2004,
            short_tp=1988,
            entry_ts=ts,
        )
    )
    engine.prune_closed_history(max_closed_pairs=1, max_events=10, max_trades=10, max_bars=10)
    assert [pair.id for pair in engine.pairs] == ["new_york:2", "new_york:open"]


@pytest.mark.asyncio
async def test_paper_warns_on_a_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        paper_enabled=True,
        orb_minutes=15,
        entry_delay_minutes=15,
        paper_closed_pair_retention=2,
    )
    engine = ClosedBarEngine(build_windows(["new_york"], {}), settings.engine_params())
    store = CandleStore(settings, client=None)  # type: ignore[arg-type]
    trader = PaperTrader(
        settings, store, engine, Notifier(settings, client=None), settings.paper_state_path
    )  # type: ignore[arg-type]
    first = _bar(datetime(2026, 1, 14, 13, 0, tzinfo=UTC))
    later = _bar(datetime(2026, 1, 14, 13, 45, tzinfo=UTC))
    calls = {"n": 0}
    seen: list[str] = []

    async def fetch(*_a: object, **_k: object) -> list[Candle]:
        calls["n"] += 1
        if calls["n"] == 1:
            return [first]
        return [first, later]

    def capture(kind: str, **_fields: object) -> None:
        seen.append(kind)

    monkeypatch.setattr(trader._store, "fetch_ctrader", fetch)
    monkeypatch.setattr("backtesting_service.paper.log_event", capture)
    await trader.tick()
    await trader.tick()
    assert "paper_gap_detected" in seen
    # These were Literal[False] while no execution bridge existed. They are now real state,
    # so the guarantee worth pinning changed shape: a trader built without a bridge, under
    # the default configuration, still sends nothing.
    status = trader.status()
    assert status.sends_broker_orders is False
    assert status.execution_mode is ExecutionMode.OFF
    assert trader.bridge is None


def test_execution_is_off_unless_deliberately_configured() -> None:
    """Fail-closed: a service nobody configured for execution cannot reach a broker."""
    settings = Settings()
    assert settings.market_execution_mode is ExecutionMode.OFF
    assert settings.market_execution_mode.sends_orders is False
    assert settings.market_execution_mode.builds_payloads is False


def test_execution_mode_requires_an_account() -> None:
    with pytest.raises(ValidationError, match="EXECUTION_CTRADER_ACCOUNT"):
        Settings(market_execution_mode="live", execution_account="")


def test_live_mode_requires_a_gateway_key() -> None:
    with pytest.raises(ValidationError, match="CTRADER_API_KEY"):
        Settings(
            market_execution_mode="live",
            execution_account="forex_demo",
            ctrader_api_key=None,
        )


def test_shadow_mode_builds_payloads_but_sends_nothing() -> None:
    mode = Settings(
        market_execution_mode="shadow", execution_account="forex_demo"
    ).market_execution_mode
    assert mode.builds_payloads is True
    assert mode.sends_orders is False
