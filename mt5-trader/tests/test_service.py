from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mt5_signal_service.errors import ServiceError
from mt5_signal_service.models import SignalState
from mt5_signal_service.service import SignalExecutionService


@pytest.mark.asyncio
async def test_market_order_is_preflighted_and_sent(service, adapter, signal_factory) -> None:
    signal = signal_factory(stop_loss="1.09900", take_profit="1.10200", deviation_points=0)

    response = await service.execute(signal)

    assert response.outcome is SignalState.FILLED
    assert response.order_ticket == 1001
    assert len(adapter.check_requests) == len(adapter.send_requests) == 1
    request = adapter.send_requests[0]
    assert request["action"] == adapter.constants.trade_action_deal
    assert request["type"] == adapter.constants.order_type_buy
    assert request["price"] == adapter.tick.ask
    assert request["magic"] == 234000
    assert request["deviation"] == 0
    assert request["comment"].startswith("sig:") and len(request["comment"]) == 20
    assert request["comment"].isascii()
    assert service.repository.get(str(signal.signal_id)).result["retcode"] == 10009


@pytest.mark.asyncio
async def test_market_order_quantizes_imprecise_risk_levels(service, adapter, signal_factory) -> None:
    signal = signal_factory(
        stop_loss="1.0990049999999999",
        take_profit="1.1020000000000001",
    )

    response = await service.execute(signal)

    assert response.outcome is SignalState.FILLED
    request = adapter.send_requests[0]
    assert request["price"] == adapter.tick.ask
    assert request["sl"] == 1.099
    assert request["tp"] == 1.102


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_type", "direction", "price", "expected_type"),
    [
        ("limit", "buy", "1.09900", 2),
        ("limit", "sell", "1.10100", 3),
        ("stop", "buy", "1.10100", 4),
        ("stop", "sell", "1.09900", 5),
    ],
)
async def test_pending_order_mapping(
    service, adapter, signal_factory, execution_type, direction, price, expected_type
) -> None:
    adapter.send_result = {
        "retcode": 10008,
        "order": 321,
        "deal": 0,
        "volume": 0,
        "price": 0,
        "comment": "Order placed",
    }
    signal = signal_factory(
        execution_type=execution_type,
        direction=direction,
        entry_price=price,
    )
    response = await service.execute(signal)
    assert response.outcome is SignalState.PLACED
    assert adapter.send_requests[0]["type"] == expected_type
    assert adapter.send_requests[0]["action"] == adapter.constants.trade_action_pending


@pytest.mark.asyncio
async def test_partial_fill_is_normalized(service, adapter, signal_factory) -> None:
    adapter.send_result = {
        "retcode": 10010,
        "order": 1,
        "deal": 2,
        "volume": 0.05,
        "price": 1.1,
        "comment": "Partial",
    }
    response = await service.execute(signal_factory())
    assert response.outcome is SignalState.PARTIALLY_FILLED
    assert response.executed_volume == Decimal("0.05")


@pytest.mark.asyncio
async def test_identical_replay_returns_result_without_resending(
    service, adapter, signal_factory
) -> None:
    signal = signal_factory()
    first = await service.execute(signal)
    second = await service.execute(signal)
    assert first == second
    assert len(adapter.send_requests) == 1


@pytest.mark.asyncio
async def test_changed_replay_conflicts(service, signal_factory) -> None:
    signal = signal_factory()
    await service.execute(signal)
    changed = signal.model_copy(update={"volume": Decimal("0.20")})
    with pytest.raises(ServiceError) as raised:
        await service.execute(changed)
    assert raised.value.status_code == 409
    assert raised.value.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_stale_signal_is_rejected_and_replayed(service, adapter, signal_factory) -> None:
    signal = signal_factory(occurred_at=(datetime.now(UTC) - timedelta(minutes=2)).isoformat())
    for _ in range(2):
        with pytest.raises(ServiceError) as raised:
            await service.execute(signal)
        assert raised.value.code == "stale_signal"
    assert adapter.send_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"symbol": "XAUUSD"}, "symbol_not_allowed"),
        ({"volume": "2.01"}, "volume_cap_exceeded"),
        ({"volume": "0.015"}, "invalid_volume_step"),
        ({"deviation_points": 21}, "deviation_cap_exceeded"),
        ({"execution_type": "limit", "entry_price": "1.10100"}, "invalid_entry_price"),
        ({"execution_type": "stop", "entry_price": "1.09900"}, "invalid_entry_price"),
        ({"execution_type": "limit", "entry_price": "1.10015"}, "entry_price_too_close"),
        ({"entry_price": None, "stop_loss": "1.10030"}, "invalid_stop_loss"),
        ({"entry_price": None, "take_profit": "1.10010"}, "invalid_take_profit"),
        ({"entry_price": None, "stop_loss": "1.10015"}, "stop_loss_too_close"),
    ],
)
async def test_guardrail_rejections(service, adapter, signal_factory, overrides, code) -> None:
    with pytest.raises(ServiceError) as raised:
        await service.execute(signal_factory(**overrides))
    assert raised.value.code == code
    assert adapter.send_requests == []


@pytest.mark.asyncio
async def test_preflight_rejection_never_sends(service, adapter, signal_factory) -> None:
    adapter.check_result = {"retcode": 10019, "comment": "No money"}
    signal = signal_factory()
    with pytest.raises(ServiceError) as raised:
        await service.execute(signal)
    assert raised.value.code == "preflight_rejected"
    assert adapter.send_requests == []
    stored = service.repository.get(str(signal.signal_id))
    assert stored.request["symbol"] == "EURUSD"
    assert stored.check["retcode"] == 10019


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["none", "exception"])
async def test_ambiguous_send_is_unknown_and_never_retried(
    service, adapter, repository, signal_factory, mode
) -> None:
    signal = signal_factory()
    if mode == "none":
        adapter.send_result = None
    else:
        adapter.send_exception = RuntimeError("transport failed")
    with pytest.raises(ServiceError) as raised:
        await service.execute(signal)
    assert raised.value.code == "execution_outcome_unknown"
    assert repository.get(str(signal.signal_id)).state is SignalState.UNKNOWN
    with pytest.raises(ServiceError):
        await service.execute(signal)
    assert len(adapter.send_requests) == 1


@pytest.mark.asyncio
async def test_terminal_calls_are_serialized(service, adapter, signal_factory) -> None:
    adapter.send_delay = 0.05
    signals = [signal_factory(), signal_factory()]
    await asyncio.gather(*(service.execute(signal) for signal in signals))
    assert adapter.max_active_sends == 1


def test_startup_reconciles_interrupted_execution(
    settings, adapter, repository, signal_factory
) -> None:
    signal = signal_factory()
    payload = signal.canonical_json()
    record, _ = repository.reserve(str(signal.signal_id), "hash", payload, "sig:reconcile")
    repository.mark_executing(record.signal_id, {"symbol": "EURUSD"}, {"retcode": 0})
    adapter.deals = [
        {
            "ticket": 999,
            "order": 888,
            "volume": 0.1,
            "price": 1.1,
            "comment": "sig:reconcile",
        }
    ]
    service = SignalExecutionService(settings, adapter, repository)
    service.reconcile_startup()
    stored = repository.get(str(signal.signal_id))
    assert stored.state is SignalState.FILLED
    assert stored.response["reconciled"] is True


def test_unmatched_interrupted_execution_becomes_unknown(
    settings, adapter, repository, signal_factory
) -> None:
    signal = signal_factory()
    record, _ = repository.reserve(
        str(signal.signal_id), "hash", signal.canonical_json(), "sig:none"
    )
    repository.mark_executing(record.signal_id, {}, {"retcode": 0})
    SignalExecutionService(settings, adapter, repository).reconcile_startup()
    assert repository.get(record.signal_id).state is SignalState.UNKNOWN
