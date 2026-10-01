from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from ta_contracts import (
    AmendOrderRequest,
    CancelOrderRequest,
    ClosePositionRequest,
    OperationAction,
    OrderRequest,
    PositionProtectionRequest,
    TargetState,
)
from ta_core import ServiceError
from ta_plugin_api import ExecutionProvider

from ta_plugin_mt5 import FACTORY
from ta_plugin_mt5.execution import MT5Execution
from ta_plugin_mt5.testing import FakeMT5Adapter

OPERATION = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "profile": "hfm",
        "login": 123456,
        "magic_number": 234000,
        "default_deviation_points": 10,
        "maximum_deviation_points": 20,
        "trading_enabled": True,
        "allowed_symbols": frozenset({"EURUSD", "Volatility 75 Index"}),
        "maximum_volume": Decimal("2.0"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def adapter() -> FakeMT5Adapter:
    return FakeMT5Adapter()


@pytest.fixture
def provider(adapter: FakeMT5Adapter) -> MT5Execution:
    return FACTORY.execution(_settings(), terminal=adapter)


def _common() -> dict[str, Any]:
    return {
        "operation_id": str(OPERATION),
        "occurred_at": datetime.now(UTC).isoformat(),
        "source": "strategy_a",
    }


def _order(**overrides: Any) -> OrderRequest:
    payload = {
        **_common(),
        "instrument": "eurusd",
        "execution_type": "market",
        "direction": "buy",
        "targets": [{"account": "hfm", "volume_lots": "0.10"}],
        "stop_loss_distance": "0.00100",
    }
    payload.update(overrides)
    return OrderRequest.model_validate(payload)


async def _run(provider: MT5Execution, action: OperationAction, request: Any) -> Any:
    target = request.targets[0]
    client_order_id = provider.client_order_id(request.operation_id, target.account)
    prepared = await provider.prepare(action, request, target, client_order_id)
    return await provider.dispatch(
        request.operation_id, target.account, action, prepared, client_order_id
    )


def test_satisfies_the_execution_protocol(provider: MT5Execution) -> None:
    assert isinstance(provider, ExecutionProvider)
    assert provider.accounts() == ("hfm",)


def test_client_order_id_fits_the_mt5_comment_limit(provider: MT5Execution) -> None:
    client_order_id = provider.client_order_id(uuid4(), "hfm")
    assert client_order_id.startswith("o-")
    assert len(client_order_id) <= 31


async def test_market_order_is_preflighted_sent_and_filled(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    outcome = await _run(provider, OperationAction.PLACE_ORDER, _order())

    assert outcome.state is TargetState.FILLED
    assert outcome.values["deal_id"] == 2001
    assert len(adapter.check_requests) == len(adapter.send_requests) == 1
    sent = adapter.send_requests[0]
    assert sent["symbol"] == "EURUSD"
    assert sent["comment"] == provider.client_order_id(OPERATION, "hfm")
    assert sent["price"] - sent["sl"] == pytest.approx(0.00100)
    assert outcome.details["request"] == sent


async def test_instrument_matches_case_preserved_mt5_names(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    await _run(provider, OperationAction.PLACE_ORDER, _order(instrument="Volatility 75 Index"))

    assert adapter.send_requests[0]["symbol"] == "Volatility 75 Index"


async def test_unknown_instrument_is_rejected_before_any_terminal_order_call(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    with pytest.raises(ServiceError) as raised:
        await _run(provider, OperationAction.PLACE_ORDER, _order(instrument="XAUUSD"))

    assert raised.value.code == "symbol_not_allowed"
    assert adapter.check_requests == adapter.send_requests == []


async def test_partial_fill_is_final(provider: MT5Execution, adapter: FakeMT5Adapter) -> None:
    adapter.send_result = {"retcode": 10010, "order": 1, "deal": 2, "volume": 0.05, "price": 1.1}

    outcome = await _run(provider, OperationAction.PLACE_ORDER, _order())

    assert outcome.state is TargetState.PARTIALLY_FILLED_FINAL
    assert outcome.values["executed_volume_lots"] == Decimal("0.05")


async def test_preflight_rejection_never_sends(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    adapter.check_result = {"retcode": 10019, "comment": "No money"}

    outcome = await _run(provider, OperationAction.PLACE_ORDER, _order())

    assert outcome.state is TargetState.REJECTED
    assert outcome.values["error_code"] == "preflight_rejected"
    assert adapter.send_requests == []


@pytest.mark.parametrize("mode", ["none", "exception"])
async def test_ambiguous_send_is_unknown(
    provider: MT5Execution, adapter: FakeMT5Adapter, mode: str
) -> None:
    if mode == "none":
        adapter.send_result = None
    else:
        adapter.send_exception = RuntimeError("transport failed")

    outcome = await _run(provider, OperationAction.PLACE_ORDER, _order())

    assert outcome.state is TargetState.UNKNOWN
    assert outcome.values["error_code"] == "execution_outcome_unknown"


async def test_broker_rejection_carries_the_retcode(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    adapter.send_result = {"retcode": 10006, "comment": "Request rejected"}

    outcome = await _run(provider, OperationAction.PLACE_ORDER, _order())

    assert outcome.state is TargetState.REJECTED
    assert outcome.values["error_code"] == "10006"


async def test_trading_disabled_refuses_to_prepare(adapter: FakeMT5Adapter) -> None:
    provider = FACTORY.execution(_settings(trading_enabled=False), terminal=adapter)

    with pytest.raises(ServiceError) as raised:
        await _run(provider, OperationAction.PLACE_ORDER, _order())

    assert raised.value.code == "trading_disabled"


async def test_cancel_removes_the_pending_order(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    adapter.live_orders = [{"ticket": 55, "symbol": "EURUSD", "volume_current": 0.1}]
    request = CancelOrderRequest.model_validate(
        {**_common(), "targets": [{"account": "hfm", "order_id": 55}]}
    )

    outcome = await _run(provider, OperationAction.CANCEL_ORDER, request)

    assert outcome.state is TargetState.CANCELLED
    sent = adapter.send_requests[0]
    assert sent["action"] == adapter.constants.trade_action_remove
    assert sent["order"] == 55
    assert adapter.check_requests == []


async def test_cancel_of_an_unknown_order_is_rejected_up_front(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    request = CancelOrderRequest.model_validate(
        {**_common(), "targets": [{"account": "hfm", "order_id": 55}]}
    )

    with pytest.raises(ServiceError) as raised:
        await _run(provider, OperationAction.CANCEL_ORDER, request)

    assert raised.value.code == "order_not_found"
    assert adapter.send_requests == []


async def test_amend_order_keeps_unmentioned_levels(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    adapter.live_orders = [
        {"ticket": 55, "symbol": "EURUSD", "price_open": 1.099, "sl": 1.098, "tp": 1.102}
    ]
    request = AmendOrderRequest.model_validate(
        {**_common(), "targets": [{"account": "hfm", "order_id": 55, "take_profit": "1.10300"}]}
    )

    outcome = await _run(provider, OperationAction.AMEND_ORDER, request)

    assert outcome.state is TargetState.AMENDED
    sent = adapter.send_requests[0]
    assert sent["action"] == adapter.constants.trade_action_modify
    assert (sent["price"], sent["sl"], sent["tp"]) == (1.099, 1.098, 1.103)


async def test_amend_order_volume_is_not_supported(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    adapter.live_orders = [{"ticket": 55, "symbol": "EURUSD"}]
    request = AmendOrderRequest.model_validate(
        {**_common(), "targets": [{"account": "hfm", "order_id": 55, "volume_lots": "0.2"}]}
    )

    with pytest.raises(ServiceError) as raised:
        await _run(provider, OperationAction.AMEND_ORDER, request)

    assert raised.value.code == "amend_volume_not_supported"


async def test_position_protection_keeps_the_other_level(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    adapter.positions = [
        {"ticket": 77, "symbol": "EURUSD", "type": 0, "volume": 0.1, "sl": 1.098, "tp": 1.104}
    ]
    request = PositionProtectionRequest.model_validate(
        {**_common(), "targets": [{"account": "hfm", "position_id": 77, "stop_loss": "1.09900"}]}
    )

    outcome = await _run(provider, OperationAction.AMEND_POSITION, request)

    assert outcome.state is TargetState.AMENDED
    sent = adapter.send_requests[0]
    assert sent["action"] == adapter.constants.trade_action_sltp
    assert (sent["position"], sent["sl"], sent["tp"]) == (77, 1.099, 1.104)


async def test_close_sends_the_opposite_deal_at_the_closing_side(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    adapter.positions = [{"ticket": 77, "symbol": "EURUSD", "type": 0, "volume": 0.3}]
    request = ClosePositionRequest.model_validate(
        {**_common(), "targets": [{"account": "hfm", "position_id": 77, "volume_lots": "0.10"}]}
    )

    outcome = await _run(provider, OperationAction.CLOSE_POSITION, request)

    assert outcome.state is TargetState.CLOSED
    assert outcome.values["deal_id"] == 2001
    sent = adapter.send_requests[0]
    assert sent["type"] == adapter.constants.order_type_sell
    assert sent["price"] == adapter.tick.bid
    assert (sent["position"], sent["volume"]) == (77, 0.1)


async def test_close_more_than_is_open_is_rejected(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    adapter.positions = [{"ticket": 77, "symbol": "EURUSD", "type": 1, "volume": 0.1}]
    request = ClosePositionRequest.model_validate(
        {**_common(), "targets": [{"account": "hfm", "position_id": 77, "volume_lots": "0.20"}]}
    )

    with pytest.raises(ServiceError) as raised:
        await _run(provider, OperationAction.CLOSE_POSITION, request)

    assert raised.value.code == "close_volume_too_large"


def test_inventory_is_normalized(provider: MT5Execution, adapter: FakeMT5Adapter) -> None:
    adapter.live_orders = [{"ticket": 55, "symbol": "EURUSD", "volume_current": 0.1}]
    adapter.positions = [
        {"ticket": 77, "symbol": "EURUSD", "type": 1, "volume": 0.2, "price_open": 1.1, "sl": 0}
    ]

    [order] = provider.orders("hfm")
    [position] = provider.positions("hfm")

    assert (order.order_id, order.volume_lots) == (55, Decimal("0.1"))
    assert (position.position_id, position.direction.value) == (77, "sell")
    assert position.stop_loss is None
    with pytest.raises(KeyError):
        provider.orders("ftmo")


def test_readiness_follows_the_terminal(provider: MT5Execution, adapter: FakeMT5Adapter) -> None:
    assert provider.readiness()[0] is True
    adapter.connection = replace(adapter.connection, login=999)
    ready, details = provider.readiness()
    assert ready is False
    assert details["account_matches"] is False


@dataclass
class _Unresolved:
    operation_id: str
    account: str
    client_order_id: str | None
    state: TargetState
    broker_tag: str | None
    details: dict[str, Any] | None
    created_at: datetime


class _Ledger:
    def __init__(self, targets: list[_Unresolved]) -> None:
        self.targets = targets
        self.updates: list[tuple[str, TargetState, dict[str, Any]]] = []

    def unresolved_targets(self, accounts: Any) -> list[_Unresolved]:
        return [target for target in self.targets if target.account in set(accounts)]

    def update_target(self, operation_id: Any, account: str, state: TargetState, **values: Any):
        self.updates.append((str(operation_id), state, values))


async def test_reconcile_settles_interrupted_orders_and_leaves_signals_alone(
    provider: MT5Execution, adapter: FakeMT5Adapter
) -> None:
    created = datetime.now(UTC) - timedelta(minutes=1)
    tag = provider.client_order_id(OPERATION, "hfm")
    ledger = _Ledger(
        [
            _Unresolved("reserved", "hfm", "o-1", TargetState.RESERVED, None, None, created),
            _Unresolved(
                "dispatched",
                "hfm",
                tag,
                TargetState.DISPATCHED,
                None,
                {"request": {"symbol": "EURUSD", "volume": 0.1}},
                created,
            ),
            _Unresolved("lost", "hfm", "o-2", TargetState.DISPATCHED, None, None, created),
            # A /v1/signals row: its comment is the source; the signal path owns it.
            _Unresolved("signal", "hfm", None, TargetState.DISPATCHED, "ipda", None, created),
        ]
    )
    adapter.deals = [{"ticket": 9, "order": 8, "symbol": "EURUSD", "volume": 0.1, "comment": tag}]
    provider.attach_ledger(ledger)  # type: ignore[arg-type]

    await provider.reconcile()

    states = {operation_id: state for operation_id, state, _ in ledger.updates}
    assert states == {
        "reserved": TargetState.REJECTED,
        "dispatched": TargetState.FILLED,
        "lost": TargetState.UNKNOWN,
    }
