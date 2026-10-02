"""The Binance futures execution adapter against a stateful fake of the fapi."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from ta_contracts import ClosePositionTarget, OperationAction, OrderRequest, TargetState
from ta_core import ServiceError
from ta_plugin_api import ControlResult
from ta_plugin_api.testing import AccountControlConformance, ExecutionConformance

from ta_plugin_binance_futures.execution import BinanceFuturesExecution, position_id_for
from ta_plugin_binance_futures.rest import FapiRest
from ta_plugin_binance_futures.testing import (
    FakeBinanceTrading,
    FakeUserStream,
    trading_settings,
)

ACCOUNT = "binance_testnet"


class FakeLedger:
    """The LedgerPort slice the adapter uses, in memory."""

    def __init__(self) -> None:
        self.targets: dict[str, dict[str, Any]] = {}  # operation id -> target
        self.events: list[dict[str, Any]] = []

    def add(self, operation_id: str, client_order_id: str, state: TargetState) -> None:
        self.targets[operation_id] = {
            "account": ACCOUNT,
            "client_order_id": client_order_id,
            "state": state,
            "values": {},
        }

    def find_by_client_order_id(self, client_order_id: str) -> tuple[str, str] | None:
        for operation_id, target in self.targets.items():
            if target["client_order_id"] == client_order_id:
                return operation_id, target["account"]
        return None

    def get(self, operation_id: str) -> Any:
        target = self.targets.get(str(operation_id))
        if target is None:
            return None
        return SimpleNamespace(targets=[SimpleNamespace(account=ACCOUNT, state=target["state"])])

    def update_target(self, operation_id, account, state, *, details=None, **values) -> Any:
        target = self.targets[str(operation_id)]
        target["state"] = state
        target["values"].update(values)
        return None

    def unresolved_targets(self, accounts) -> list[Any]:
        open_states = {
            TargetState.RESERVED,
            TargetState.DISPATCHED,
            TargetState.ACCEPTED,
            TargetState.UNKNOWN,
        }
        return [
            SimpleNamespace(
                operation_id=op,
                account=t["account"],
                client_order_id=t["client_order_id"],
                state=t["state"],
            )
            for op, t in self.targets.items()
            if t["state"] in open_states
        ]

    def append_event(self, *, account, event_type, payload, operation_id=None) -> None:
        self.events.append({"type": event_type, "operation_id": operation_id, **payload})


async def no_sleep(_: float) -> None:
    await asyncio.sleep(0)


def build(
    fake: FakeBinanceTrading, **overrides: Any
) -> tuple[BinanceFuturesExecution, httpx.AsyncClient, FakeUserStream]:
    config = trading_settings(**overrides)
    http = httpx.AsyncClient(
        base_url=config.binance_futures_order_rest_url, transport=httpx.MockTransport(fake.handler)
    )
    rest = FapiRest(
        config,
        http=http,
        api_key=config.binance_futures_trading_api_key,
        api_secret=config.binance_futures_trading_api_secret,
    )
    stream = FakeUserStream(fake)
    provider = BinanceFuturesExecution(config, rest=rest, ws_connect=stream, sleep=no_sleep)
    return provider, http, stream


async def settle(predicate, seconds: float = 2.0) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():  # noqa: ASYNC110 - polling test state
            await asyncio.sleep(0.002)


@pytest.fixture
def fake() -> FakeBinanceTrading:
    return FakeBinanceTrading()


@pytest.fixture
async def venue(fake: FakeBinanceTrading) -> AsyncIterator[BinanceFuturesExecution]:
    provider, http, _ = build(fake)
    provider.attach_ledger(FakeLedger())
    await provider.start()
    assert await provider.wait_ready(2.0), provider.readiness()
    yield provider
    await provider.close()
    await http.aclose()


def order(**fields: Any) -> OrderRequest:
    values: dict[str, Any] = {
        "operation_id": str(uuid4()),
        "occurred_at": "2026-10-02T10:00:00+00:00",
        "source": "ofi_scalper",
        "instrument": "BTCUSDT",
        "execution_type": "limit",
        "direction": "buy",
        "targets": [{"account": ACCOUNT, "volume_lots": "0.002"}],
        "entry_price": "59999.9",
    }
    values.update(fields)
    if values["execution_type"] == "market":
        values.pop("entry_price", None)
    return OrderRequest.model_validate(values)


async def send(provider: BinanceFuturesExecution, request: Any, action=OperationAction.PLACE_ORDER):
    target = request.targets[0]
    op = UUID(str(request.operation_id))
    coid = provider.client_order_id(op, target.account)
    ledger: FakeLedger = provider._ledger  # type: ignore[assignment]
    ledger.add(str(op), coid, TargetState.DISPATCHED)
    prepared = await provider.prepare(action, request, target, coid)
    outcome = await provider.dispatch(op, target.account, action, prepared, coid)
    ledger.update_target(str(op), target.account, outcome.state, **dict(outcome.values))
    return outcome, ledger.targets[str(op)], coid


# --- conformance -----------------------------------------------------------------


class TestExecutionConformance(ExecutionConformance):
    @pytest.fixture
    async def provider(self, fake: FakeBinanceTrading) -> AsyncIterator[BinanceFuturesExecution]:
        provider, http, _ = build(fake)
        yield provider
        await http.aclose()

    @pytest.fixture
    def account(self) -> str:
        return ACCOUNT

    @pytest.fixture
    def client_order_id_limit(self) -> int:
        return 36


class TestAccountControlConformance(AccountControlConformance):
    @pytest.fixture
    async def venue(self, venue: BinanceFuturesExecution) -> BinanceFuturesExecution:
        return venue

    @pytest.fixture
    def account(self) -> str:
        return ACCOUNT

    @pytest.fixture
    def instrument(self) -> str:
        return "BTCUSDT"


# --- preflight ---------------------------------------------------------------------


async def test_preflight_blocks_until_the_account_is_safe(fake: FakeBinanceTrading) -> None:
    fake.account_flags["dualSidePosition"] = True
    fake.symbol_config["BTCUSDT"] = {"leverage": 20, "marginType": "CROSSED"}
    provider, http, _ = build(fake)
    errors = await provider.preflight()
    assert any("One-way" in e for e in errors)
    assert any("BTCUSDT leverage is 20x" in e for e in errors)
    assert any("BTCUSDT margin is CROSSED" in e for e in errors)
    provider.preflight_errors = errors
    ready, details = provider.readiness()
    assert not ready and details["preflight"] == errors
    with pytest.raises(ServiceError) as caught:
        await provider.prepare(OperationAction.PLACE_ORDER, order(), order().targets[0], "x")
    assert caught.value.code == "account_not_ready"
    await http.aclose()


async def test_user_stream_uses_the_private_route(venue, fake) -> None:
    url = venue.user_stream._root + "/private/ws?listenKey=lk-test"
    assert venue.user_stream.url("lk-test").startswith(url)
    assert "events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE" in venue.user_stream.url("lk-test")
    assert fake.listen_key_calls[0] == "POST"
    statuses = venue.account_statuses()[0]
    assert statuses["environment"] == "testnet" and statuses["is_live"] is False
    assert statuses["order_entry_enabled"] is True


# --- orders ----------------------------------------------------------------------------


async def test_post_only_limit_rests_then_the_stream_settles_its_fill(venue, fake) -> None:
    outcome, target, coid = await send(venue, order(post_only=True))
    assert outcome.state is TargetState.PLACED
    order_id = outcome.values["order_id"]
    sent = next(r for r in fake.requests if r.method == "POST" and r.url.path == "/fapi/v1/order")
    assert sent.url.params["timeInForce"] == "GTX"
    assert sent.url.params["newClientOrderId"] == coid and len(coid) == 32
    assert [o.order_id for o in venue.orders(ACCOUNT)] == [order_id]

    fake.fill(order_id)
    await settle(lambda: target["state"] is TargetState.FILLED)
    assert target["values"]["executed_volume_lots"] == Decimal("0.002")
    assert target["values"]["execution_price"] == Decimal("59999.9")
    assert venue.orders(ACCOUNT) == []
    await settle(lambda: bool(venue.positions(ACCOUNT)))
    assert venue.positions(ACCOUNT)[0].position_id == position_id_for("BTCUSDT")
    assert any(e["type"] == "binance_order_update" for e in venue._ledger.events)


async def test_post_only_that_would_take_is_rejected(venue) -> None:
    outcome, _, _ = await send(venue, order(post_only=True, entry_price="60000.1"))
    assert outcome.state is TargetState.REJECTED
    assert outcome.values["error_code"] == "post_only_would_take"


async def test_market_entry_fills_and_close_position_is_reduce_only(venue, fake) -> None:
    outcome, _, _ = await send(venue, order(execution_type="market"))
    assert outcome.state is TargetState.FILLED
    assert outcome.values["execution_price"] == Decimal("60000.10")
    await settle(lambda: bool(venue.positions(ACCOUNT)))

    close = SimpleNamespace(
        operation_id=uuid4(),
        targets=[
            ClosePositionTarget(
                account=ACCOUNT,
                position_id=position_id_for("BTCUSDT"),
                volume_lots=Decimal("0.002"),
            )
        ],
    )
    closed, _, _ = await send(venue, close, OperationAction.CLOSE_POSITION)
    assert closed.state is TargetState.CLOSED
    sent = [r for r in fake.requests if r.method == "POST" and r.url.path == "/fapi/v1/order"][-1]
    assert sent.url.params["reduceOnly"] == "true" and sent.url.params["side"] == "SELL"
    assert fake.positions["BTCUSDT"] == 0


async def test_reduce_only_without_a_position_is_rejected(venue) -> None:
    outcome, _, _ = await send(
        venue, order(execution_type="market", direction="sell", reduce_only=True)
    )
    assert outcome.state is TargetState.REJECTED
    assert outcome.values["error_code"] == "reduce_only_rejected"


@pytest.mark.parametrize("failure", ["transport", "500", "-1007"])
async def test_ambiguous_outcomes_are_unknown_then_reconciled_not_resent(
    venue, fake, failure
) -> None:
    fake.fail_next = failure
    outcome, target, coid = await send(venue, order())
    assert outcome.state is TargetState.UNKNOWN
    assert outcome.values["error_code"] == "execution_outcome_unknown"
    posts = sum(1 for r in fake.requests if r.method == "POST" and r.url.path == "/fapi/v1/order")
    # The stream may already have settled it; force the reconcile path either way.
    target["state"] = TargetState.UNKNOWN
    await venue.reconcile_unknown()
    assert target["state"] is TargetState.PLACED  # found by client order id
    assert (
        sum(1 for r in fake.requests if r.method == "POST" and r.url.path == "/fapi/v1/order")
        == posts
    )  # never resubmitted


async def test_unknown_that_never_reached_binance_stays_unknown(venue) -> None:
    ledger: FakeLedger = venue._ledger
    ledger.add("op-lost", "f" * 32, TargetState.UNKNOWN)
    await venue.reconcile_unknown()
    assert ledger.targets["op-lost"]["state"] is TargetState.UNKNOWN  # never flipped to rejected


async def test_restart_rejects_reserved_and_finds_dispatched(venue, fake) -> None:
    request = order()
    await send(venue, request)
    ledger: FakeLedger = venue._ledger
    # As if the gateway died after sending, before recording the outcome.
    ledger.targets[str(request.operation_id)]["state"] = TargetState.DISPATCHED
    ledger.targets["op-dispatched"] = ledger.targets.pop(str(request.operation_id))
    ledger.add("op-reserved", "r" * 32, TargetState.RESERVED)
    await venue.reconcile()
    assert ledger.targets["op-reserved"]["state"] is TargetState.REJECTED
    assert ledger.targets["op-dispatched"]["state"] is TargetState.PLACED


async def test_a_fill_missed_while_disconnected_is_found_on_resync(venue, fake) -> None:
    outcome, target, _ = await send(venue, order())
    stream_queue = fake.user_queue
    fake.user_queue = None  # the stream is down: this fill's events are lost
    fake.fill(outcome.values["order_id"])
    assert target["state"] is TargetState.PLACED
    fake.user_queue = stream_queue
    venue.user_stream._ws_connect.drop.set()  # drop; the adapter reconnects and resyncs
    await settle(lambda: target["state"] is TargetState.FILLED)
    assert venue.user_stream.reconnects >= 1


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"targets": [{"account": ACCOUNT, "volume_lots": "0.0015"}]}, "invalid_volume_step"),
        ({"entry_price": "59999.95"}, "invalid_price_step"),
        (
            {"targets": [{"account": ACCOUNT, "volume_lots": "0.001"}], "entry_price": "40000.0"},
            None,
        ),
        ({"stop_loss": "59000.0"}, "protection_not_supported"),
    ],
)
async def test_prepare_validates_before_anything_is_sent(venue, fields, code) -> None:
    request = order(**fields)
    if code is None:
        await venue.prepare(OperationAction.PLACE_ORDER, request, request.targets[0], "c" * 32)
        return
    with pytest.raises(ServiceError) as caught:
        await venue.prepare(OperationAction.PLACE_ORDER, request, request.targets[0], "c" * 32)
    assert caught.value.code == code


async def test_gates_trading_disabled_mainnet_and_volume_cap(fake) -> None:
    for overrides, code in (
        ({"trading_enabled": False}, "trading_disabled"),
        ({"binance_futures_env": "mainnet"}, "live_trading_disabled"),
        ({"max_volume_lots": Decimal("0.001")}, "volume_exceeds_limit"),
    ):
        provider, http, _ = build(fake, **overrides)
        provider.attach_ledger(FakeLedger())
        await provider.start()
        assert await provider.wait_ready(2.0)
        request = order()
        with pytest.raises(ServiceError) as caught:
            await provider.prepare(
                OperationAction.PLACE_ORDER, request, request.targets[0], "c" * 32
            )
        assert caught.value.code == code
        await provider.close()
        await http.aclose()


async def test_amend_is_refused_explicitly(venue) -> None:
    with pytest.raises(ServiceError) as caught:
        await venue.prepare(
            OperationAction.AMEND_ORDER, None, SimpleNamespace(account=ACCOUNT), "c"
        )
    assert caught.value.status_code == 501


# --- account controls ------------------------------------------------------------


async def test_kill_controls_cancel_flatten_and_arm_the_dead_man(venue, fake) -> None:
    await send(venue, order())
    await send(venue, order(execution_type="market"))
    assert any(o["status"] == "NEW" for o in fake.orders.values())

    result = await venue.cancel_all(ACCOUNT)
    assert isinstance(result, ControlResult) and result.ok
    assert not any(o["status"] == "NEW" for o in fake.orders.values())

    flat = await venue.flatten(ACCOUNT)
    assert flat.ok and fake.positions["BTCUSDT"] == 0
    flat_order = [
        r for r in fake.requests if r.url.path == "/fapi/v1/order" and r.method == "POST"
    ][-1]
    assert flat_order.url.params["reduceOnly"] == "true"

    armed = await venue.dead_man(ACCOUNT, ["BTCUSDT"], 15_000)
    assert armed.ok and fake.countdowns == {"BTCUSDT": 15_000}
    await venue.dead_man(ACCOUNT, ["BTCUSDT"], 0)
    assert fake.countdowns["BTCUSDT"] == 0
    with pytest.raises(KeyError):
        await venue.cancel_all(ACCOUNT, "DOGEUSDT")
