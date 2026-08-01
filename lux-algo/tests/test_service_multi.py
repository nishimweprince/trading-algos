from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from lux_algo.config import Settings
from lux_algo.instruments import InstrumentConfig
from lux_algo.logging_config import RuntimeLogs
from lux_algo.models import build_signal_payload
from lux_algo.mt5_client import SubmitOutcome
from lux_algo.service import SignalService, _PendingSignal
from lux_algo.strategy import Decision


def _settings_with_instruments(instruments: list[InstrumentConfig]) -> Settings:
    return Settings(
        data_api_url="https://data.example.com/candles",
        quote="EURUSD",
        mt5_symbol="EURUSD",
        volume=Decimal("0.10"),
        mt5_signal_api_key="unit-test-key",
        instruments=instruments,
        require_ready=False,
    )  # type: ignore[call-arg]


def _decision() -> Decision:
    return Decision(
        direction="buy",
        bucket_start=datetime(2026, 1, 1, 0, 3, tzinfo=UTC),
        entry=1.2,
        stop_loss=1.19,
        take_profit=1.22,
        supertrend=1.19,
    )


def _pending(
    instrument: InstrumentConfig, settings: Settings
) -> _PendingSignal:
    decision = _decision()
    return _PendingSignal(
        instrument=instrument,
        decision=decision,
        bucket_start=decision.bucket_start,
        payload=build_signal_payload(decision, instrument, settings),
    )


@pytest.mark.asyncio
async def test_two_instruments_submit_two_signals(tmp_path) -> None:
    instruments = [
        InstrumentConfig(quote="XAUUSD", mt5_symbol="XAUUSD"),
        InstrumentConfig(quote="BTCUSD", mt5_symbol="BTCUSD"),
    ]
    settings = _settings_with_instruments(instruments)
    logs = RuntimeLogs(tmp_path / "logs")

    data_client = AsyncMock()
    mt5_client = AsyncMock()
    mt5_client.submit = AsyncMock(return_value=SubmitOutcome(kind="success", status_code=200))

    service = SignalService(settings, data_client, mt5_client, logs)

    with (
        patch.object(service, "_fetch_for_pipeline", new=AsyncMock(return_value=object())),
        patch.object(
            service._pipelines[0],
            "evaluate",
            return_value=_pending(instruments[0], settings),
        ),
        patch.object(
            service._pipelines[1],
            "evaluate",
            return_value=_pending(instruments[1], settings),
        ),
    ):
        await service.tick()

    assert mt5_client.submit.await_count == 2
    assert len(logs.signals) == 2
    symbols = {entry["symbol"] for entry in logs.signals}
    assert symbols == {"XAUUSD", "BTCUSD"}


@pytest.mark.asyncio
async def test_fetch_failure_for_one_instrument_does_not_block_other(tmp_path) -> None:
    instruments = [
        InstrumentConfig(quote="XAUUSD", mt5_symbol="XAUUSD"),
        InstrumentConfig(quote="BTCUSD", mt5_symbol="BTCUSD"),
    ]
    settings = _settings_with_instruments(instruments)
    logs = RuntimeLogs(tmp_path / "logs")

    data_client = AsyncMock()
    mt5_client = AsyncMock()
    mt5_client.submit = AsyncMock(return_value=SubmitOutcome(kind="success", status_code=200))

    service = SignalService(settings, data_client, mt5_client, logs)

    async def fetch_side_effect(pipeline):
        if pipeline.instrument.quote == "XAUUSD":
            raise RuntimeError("feed down")
        return object()

    with (
        patch.object(service, "_fetch_for_pipeline", side_effect=fetch_side_effect),
        patch.object(service._pipelines[0], "evaluate", return_value=None),
        patch.object(
            service._pipelines[1],
            "evaluate",
            return_value=_pending(instruments[1], settings),
        ),
    ):
        await service.tick()

    assert len(logs.errors) == 1
    assert logs.errors[0]["quote"] == "XAUUSD"
    assert mt5_client.submit.await_count == 1
    assert logs.signals[0]["quote"] == "BTCUSD"


@pytest.mark.asyncio
async def test_gate_isolation_per_symbol(tmp_path) -> None:
    instruments = [
        InstrumentConfig(quote="XAUUSD", mt5_symbol="XAUUSD"),
        InstrumentConfig(quote="BTCUSD", mt5_symbol="BTCUSD"),
    ]
    settings = _settings_with_instruments(instruments)
    logs = RuntimeLogs(tmp_path / "logs")
    service = SignalService(settings, AsyncMock(), AsyncMock(), logs)

    bucket = datetime(2026, 1, 1, 0, 3, tzinfo=UTC)
    service._pipelines[0]._gate.lock("XAUUSD", bucket)

    assert service._pipelines[0]._gate.is_locked("XAUUSD", bucket) is True
    assert service._pipelines[1]._gate.is_locked("BTCUSD", bucket) is False
