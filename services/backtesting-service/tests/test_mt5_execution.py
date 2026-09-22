"""HFM MT5 signal payloads, outcomes, and bridge integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from ta_contracts import SignalRequest

from backtesting_service.config import Settings
from backtesting_service.execution import ExecutionResult, ExecutionState
from backtesting_service.execution_bridge import ExecutionBridge
from backtesting_service.models import Candle, EngineEvent
from backtesting_service.mt5_execution import Mt5ExecutionClient

NOW = datetime.now(tz=UTC)
BAR = Candle(
    ts=NOW,
    open=2600,
    high=2610,
    low=2590,
    close=2605,
    volume=1,
    provider="test",
    source_instrument="XAUUSD",
)


def _settings(**over: object) -> Settings:
    values: dict[str, object] = {
        "market_execution_mode": "live",
        "execution_provider": "mt5",
        "entry_mode": "synthetic_breakout",
        "tp_mode": "fixed_r",
        "be_trigger_r": 0,
        "time_exit_mode": "none",
        "mt5_signal_api_key": "hfm-key",
        "execution_mt5_profile": "hfm",
        "execution_volume_lots": 0.01,
        "mt5_deviation_points": 10,
    }
    values.update(over)
    return Settings(**values)


def _entry(pair_id: str = "london:2026-09-14T09:00:00+00:00") -> EngineEvent:
    return EngineEvent(
        kind="entry",
        session="london",
        ts=NOW,
        detail={
            "entry": 2605.0,
            "sl_dist": 10.0,
            "primary_side": "long",
            "pair_id": pair_id,
            "qty": 1.0,
        },
    )


def test_market_payload_matches_legacy_mt5_signal_contract() -> None:
    client = Mt5ExecutionClient(_settings(), httpx.AsyncClient())
    payload = client.build_market_entry(
        operation_id=UUID("11111111-1111-1111-1111-111111111111"),
        occurred_at=NOW,
        symbol="xauusd",
        direction="buy",
        stop_distance=10,
        target_distance=30,
        note="test",
    )

    parsed = SignalRequest.model_validate(payload)
    assert parsed.symbol == "XAUUSD"
    assert str(parsed.volume) == "0.01"
    assert parsed.stop_loss_distance == 10
    assert parsed.take_profit_distance == 30
    assert parsed.source == "session_hedging"
    assert parsed.ignore_signal_age is False
    assert parsed.deviation_points == 10


@pytest.mark.asyncio
async def test_client_normalises_a_filled_mt5_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/signals"
        return httpx.Response(
            200,
            json={
                "signal_id": "11111111-1111-1111-1111-111111111111",
                "outcome": "filled",
                "order_ticket": 71,
                "deal_ticket": 72,
                "executed_volume": "0.01",
                "execution_price": "2605.25",
                "broker_retcode": 10009,
                "processed_at": NOW.isoformat(),
                "reconciled": False,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = Mt5ExecutionClient(_settings(), http)
        payload = client.build_market_entry(
            operation_id=UUID("11111111-1111-1111-1111-111111111111"),
            occurred_at=NOW,
            symbol="XAUUSD",
            direction="buy",
            stop_distance=10,
            target_distance=30,
        )
        result = await client.submit(payload)

    assert result.state is ExecutionState.SUCCEEDED
    assert result.order_ids == {"hfm": 71}
    assert result.fill_price("hfm") == pytest.approx(2605.25)


class FakeMt5Client:
    account = "hfm"
    source = "session_hedging"

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.ready = True
        self.result = ExecutionResult(
            ExecutionState.SUCCEEDED,
            response={
                "targets": [{"account": "hfm", "order_id": 71, "execution_price": "2605.25"}]
            },
        )

    async def trading_ready(self) -> tuple[bool, str]:
        return self.ready, "ready" if self.ready else "offline"

    async def get_operation(self, operation_id: UUID) -> ExecutionResult:
        return self.result

    def build_market_entry(self, **values: Any) -> dict[str, Any]:
        return {
            "signal_id": str(values["operation_id"]),
            "execution_type": "market",
            "symbol": values["symbol"],
            "direction": values["direction"],
            "stop_loss_distance": values["stop_distance"],
            "take_profit_distance": values["target_distance"],
        }

    async def submit(self, payload: dict[str, Any]) -> ExecutionResult:
        self.payloads.append(payload)
        return self.result


@pytest.mark.asyncio
async def test_bridge_sends_synthetic_entry_to_hfm_once() -> None:
    client = FakeMt5Client()
    bridge = ExecutionBridge(_settings(), client)  # type: ignore[arg-type]

    await bridge.handle(_entry(), BAR)
    await bridge.handle(_entry(), BAR)

    assert len(client.payloads) == 1
    assert client.payloads[0]["direction"] == "buy"
    assert client.payloads[0]["stop_loss_distance"] == 10
    assert client.payloads[0]["take_profit_distance"] == 30
    tracked = bridge.tracked()[0]
    assert tracked.state == "succeeded"
    assert tracked.fill_price == pytest.approx(2605.25)


def _local_settings() -> Settings:
    return _settings(entry_mode="oco_bracket", mt5_oco_execution="local_market")


def _staged() -> EngineEvent:
    event = _entry()
    return event.model_copy(
        update={
            "kind": "entry_order_staged",
            "detail": {
                "pair_id": event.detail["pair_id"],
                "entry_mode": "oco_bracket",
                "upper_trigger": 2610,
                "lower_trigger": 2590,
                "sl_dist": 10,
                "target_r": 2,
                "expiry_bars": 1,
            },
        }
    )


@pytest.mark.asyncio
async def test_local_oco_uses_staged_target_and_cancel_before_entry() -> None:
    client = FakeMt5Client()
    bridge = ExecutionBridge(_local_settings(), client)  # type: ignore[arg-type]
    await bridge.handle(_staged(), BAR)
    assert not client.payloads
    cancelled = _staged().model_copy(
        update={
            "kind": "entry_order_cancelled",
            "detail": {
                "pair_id": _entry().detail["pair_id"],
                "reason": "oco_sibling",
                "cancelled_side": "short",
            },
        }
    )
    await bridge.handle(cancelled, BAR)
    await bridge.handle(_entry(), BAR)
    assert len(client.payloads) == 1
    assert client.payloads[0]["take_profit_distance"] == 20


@pytest.mark.asyncio
async def test_local_oco_expiry_never_dispatches() -> None:
    client = FakeMt5Client()
    bridge = ExecutionBridge(_local_settings(), client)  # type: ignore[arg-type]
    await bridge.handle(_staged(), BAR)
    expired = _staged().model_copy(
        update={
            "kind": "entry_order_cancelled",
            "detail": {
                "pair_id": _entry().detail["pair_id"],
                "reason": "expired",
            },
        }
    )
    await bridge.handle(expired, BAR)
    assert not bridge.local_brackets
    assert not client.payloads


@pytest.mark.asyncio
async def test_local_market_dispatch_intent_is_persisted_before_request() -> None:
    client = FakeMt5Client()
    bridge = ExecutionBridge(_local_settings(), client)  # type: ignore[arg-type]
    writes: list[dict[str, Any]] = []

    def persist() -> None:
        snapshot = bridge.snapshot()
        if not client.payloads:
            writes.append(snapshot)

    bridge.persist = persist
    await bridge.handle(_entry(), BAR)
    assert any(
        next(iter(legs.values()))["broker_state"] == "dispatching"
        for snapshot in writes
        for legs in snapshot["orders"].values()
    )


@pytest.mark.asyncio
async def test_local_market_restart_does_not_retry_ambiguous_entry_or_erase_on_exit() -> None:
    client = FakeMt5Client()
    client.result = ExecutionResult(ExecutionState.UNKNOWN, reason="timeout")
    bridge = ExecutionBridge(_local_settings(), client)  # type: ignore[arg-type]
    await bridge.handle(_entry(), BAR)
    restored = ExecutionBridge(_local_settings(), client)  # type: ignore[arg-type]
    restored.restore(bridge.snapshot())
    await restored.reconcile()
    await restored.handle(_entry().model_copy(update={"kind": "exit"}), BAR)
    await restored.handle(_entry(), BAR)
    assert len(client.payloads) == 1
    assert restored.tracked()[0].engine_closed
    assert restored.tracked()[0].state == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("offline", [False, True])
async def test_local_market_skips_stale_or_offline_entries(offline: bool) -> None:
    client = FakeMt5Client()
    client.ready = not offline
    bridge = ExecutionBridge(_local_settings(), client)  # type: ignore[arg-type]
    bar = BAR if offline else BAR.model_copy(update={"ts": NOW - timedelta(hours=2)})
    await bridge.handle(_entry(), bar)
    await bridge.handle(_entry(), bar)
    assert not client.payloads
    assert bridge.tracked()[0].state == "skipped"
    assert bridge.tracked()[0].broker_state == "not_submitted"


@pytest.mark.asyncio
async def test_paper_outbox_survives_crash_after_dispatch_intent(tmp_path: Path) -> None:
    from backtesting_service.engine import ClosedBarEngine
    from backtesting_service.paper import PaperTrader

    settings = _local_settings()
    client = FakeMt5Client()
    bridge = ExecutionBridge(settings, client)  # type: ignore[arg-type]
    engine = ClosedBarEngine(settings.session_windows(), settings.engine_params())
    state_path = tmp_path / "paper.json"
    trader = PaperTrader(settings, None, engine, None, state_path, bridge)  # type: ignore[arg-type]
    trader.execution_outbox = [(_entry(), BAR)]
    trader.save()

    async def interrupted_submit(payload: dict[str, Any]) -> ExecutionResult:
        client.payloads.append(payload)
        raise RuntimeError("simulated process interruption")

    client.submit = interrupted_submit  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="interruption"):
        await trader._drain_execution_outbox()
    restored_bridge = ExecutionBridge(settings, client)  # type: ignore[arg-type]
    restored = PaperTrader(settings, None, engine, None, state_path, restored_bridge)  # type: ignore[arg-type]
    restored.load()
    assert len(restored.execution_outbox) == 1
    assert restored_bridge.tracked()[0].broker_state == "dispatching"
    await restored_bridge.reconcile()
    await restored_bridge.handle(*restored.execution_outbox[0])
    assert len(client.payloads) == 1
