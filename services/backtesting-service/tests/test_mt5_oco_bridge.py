"""Broker OCO dispatch and reconciliation use gateway truth, not paper fills."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

import httpx
import pytest

from backtesting_service.config import Settings
from backtesting_service.engine import ClosedBarEngine
from backtesting_service.execution_bridge import ExecutionBridge
from backtesting_service.models import Candle, EngineEvent
from backtesting_service.mt5_execution import Mt5ExecutionClient
from backtesting_service.paper import PaperTrader


def settings(**overrides: Any) -> Settings:
    return Settings(
        **(
            {
                "execution_provider": "mt5",
                "market_execution_mode": "live",
                "mt5_signal_api_key": "test-key",
                "entry_mode": "oco_bracket",
                "mt5_oco_execution": "broker_pending",
                "time_exit_mode": "none",
            }
            | overrides
        )
    )


def stage() -> tuple[EngineEvent, Candle]:
    now = datetime.now(UTC)
    bar = Candle(
        ts=now,
        open=2600,
        high=2610,
        low=2590,
        close=2605,
        volume=1,
        provider="test",
        source_instrument="XAUUSD",
    )
    event = EngineEvent(
        kind="entry_order_staged",
        session="london",
        ts=now,
        detail={
            "pair_id": "london:example",
            "entry_mode": "oco_bracket",
            "upper_trigger": 2611,
            "lower_trigger": 2589,
            "sl_dist": 10,
            "target_r": 2,
            "expiry_bars": 1,
        },
    )
    return event, bar


class Gateway:
    def __init__(self) -> None:
        self.group: dict[str, Any] | None = None
        self.calls: list[httpx.Request] = []
        self.ready = True
        self.lost_response = False
        self.profile = "hfm"

    def handle(self, request: httpx.Request) -> httpx.Response:
        import json

        self.calls.append(request)
        if request.url.path == "/v1/mt5/capabilities":
            return httpx.Response(
                200,
                json={
                    "version": 1,
                    "profile": self.profile,
                    "ready": self.ready,
                    "reason": "ready",
                    "capabilities": {
                        "pending_entry": True,
                        "cancellation": True,
                        "inventory": True,
                        "protection_amendment": True,
                        "oco_coordination": True,
                    },
                },
            )
        if request.url.path == "/v1/mt5/oco" and request.method == "POST":
            payload = json.loads(request.content)
            self.group = {
                **payload,
                "state": "placed",
                "winner": None,
                "legs": {
                    side: {
                        "state": "placed",
                        "order_id": ticket,
                        "resting": True,
                        "signal_id": str(uuid5(UUID(payload["group_id"]), side)),
                        "executed_volume": "0",
                        "fill_price": None,
                        "position_ids": [],
                    }
                    for side, ticket in (("long", 71), ("short", 72))
                },
            }
            if self.lost_response:
                raise httpx.ReadTimeout("response lost", request=request)
            return httpx.Response(200, json=self.group)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json=self.group)
        if request.url.path.startswith("/v1/mt5/oco/"):
            return httpx.Response(200, json=self.group) if self.group else httpx.Response(404)
        raise AssertionError(request.url)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [False, True])
async def test_group_staging_is_durable_and_replay_does_not_dispatch_again(
    lost_response: bool,
) -> None:
    gateway = Gateway()
    gateway.lost_response = lost_response
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway.handle)) as http:
        bridge = ExecutionBridge(settings(), Mt5ExecutionClient(settings(), http))
        intents: list[dict[str, Any]] = []
        bridge.persist = lambda: intents.append(copy.deepcopy(bridge.snapshot()))
        event, bar = stage()
        await bridge.handle(event, bar)
        assert any(
            snapshot["groups"]["london:example"]["state"] == "dispatching" for snapshot in intents
        )
        restored = ExecutionBridge(settings(), Mt5ExecutionClient(settings(), http))
        restored.restore(bridge.snapshot())
        await restored.reconcile()
        await restored.handle(event, bar)
        assert (
            len(
                [
                    call
                    for call in gateway.calls
                    if call.method == "POST" and call.url.path == "/v1/mt5/oco"
                ]
            )
            == 1
        )
        assert len(restored.resting_orders()) == 2


@pytest.mark.asyncio
async def test_gateway_winner_survives_opposite_paper_prediction_and_strategy_exit() -> None:
    gateway = Gateway()
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway.handle)) as http:
        bridge = ExecutionBridge(settings(), Mt5ExecutionClient(settings(), http))
        event, bar = stage()
        await bridge.handle(event, bar)
        assert gateway.group is not None
        gateway.group["state"] = "filled"
        gateway.group["winner"] = "short"
        gateway.group["legs"]["short"].update(
            state="filled",
            resting=False,
            fill_price="2588.5",
            executed_volume="0.01",
            position_ids=[101],
        )
        gateway.group["legs"]["long"].update(state="cancelled", resting=False)
        cancel = event.model_copy(
            update={
                "kind": "entry_order_cancelled",
                "detail": {
                    "pair_id": "london:example",
                    "cancelled_side": "short",
                    "reason": "oco_sibling",
                },
            }
        )
        await bridge.handle(cancel, bar)
        assert not any(call.url.path.endswith("/cancel") for call in gateway.calls)
        entry = event.model_copy(
            update={
                "kind": "entry",
                "detail": {
                    "pair_id": "london:example",
                    "primary_side": "long",
                    "entry": 2611,
                    "sl_dist": 10,
                },
            }
        )
        await bridge.handle(entry, bar)
        await bridge.handle(entry.model_copy(update={"kind": "exit"}), bar)
        assert not any(call.url.path == "/v1/signals" for call in gateway.calls)
        assert bridge.groups["london:example"].response["winner"] == "short"
        assert bridge.groups["london:example"].engine_prediction["side"] == "long"
        assert all(order.engine_closed for order in bridge.tracked())
        assert not bridge.resting_orders()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_profile", [False, True])
async def test_gateway_readiness_or_capability_mismatch_never_falls_back(
    wrong_profile: bool,
) -> None:
    gateway = Gateway()
    gateway.profile = "other" if wrong_profile else "hfm"
    gateway.ready = wrong_profile
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway.handle)) as http:
        bridge = ExecutionBridge(settings(), Mt5ExecutionClient(settings(), http))
        event, bar = stage()
        await bridge.handle(event, bar)
        assert bridge.groups["london:example"].state == "skipped"
        assert not any(call.method == "POST" for call in gateway.calls)


@pytest.mark.asyncio
async def test_group_missing_after_restart_blocks_entries_and_is_not_resubmitted() -> None:
    gateway = Gateway()
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway.handle)) as http:
        bridge = ExecutionBridge(settings(), Mt5ExecutionClient(settings(), http))
        event, bar = stage()
        await bridge.handle(event, bar)
        gateway.group = None
        await bridge.reconcile()
        assert bridge.halted_reason
        assert bridge.groups["london:example"].state == "unknown"
        await bridge.handle(event, bar)
        assert len([call for call in gateway.calls if call.url.path == "/v1/mt5/oco"]) == 1


@pytest.mark.asyncio
async def test_broker_pending_shadow_records_bracket_without_gateway_calls() -> None:
    gateway = Gateway()
    config = settings(market_execution_mode="shadow")
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway.handle)) as http:
        bridge = ExecutionBridge(config, Mt5ExecutionClient(config, http))
        event, bar = stage()
        await bridge.handle(event, bar)
        await bridge.reconcile()
        group = bridge.groups["london:example"]
        assert group.state == "shadow"
        assert group.payload["upper_trigger"] == "2611"
        assert group.payload["lower_trigger"] == "2589"
        assert not gateway.calls


@pytest.mark.asyncio
async def test_reconciliation_persists_broker_changes_without_new_candles(tmp_path: Any) -> None:
    class EmptyStore:
        async def fetch_ctrader(self, *args: Any, **kwargs: Any) -> list[Candle]:
            return []

    gateway = Gateway()
    config = settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway.handle)) as http:
        bridge = ExecutionBridge(config, Mt5ExecutionClient(config, http))
        path = tmp_path / "paper.json"
        trader = PaperTrader(
            config,
            EmptyStore(),
            ClosedBarEngine(config.session_windows(), config.engine_params()),
            None,
            path,
            bridge,
        )
        event, bar = stage()
        await bridge.handle(event, bar)
        assert gateway.group is not None
        gateway.group["state"] = "expired"
        for leg in gateway.group["legs"].values():
            leg.update(state="expired", resting=False)
        await trader.tick()
        restored = ExecutionBridge(config, Mt5ExecutionClient(config, http))
        restored_trader = PaperTrader(
            config,
            EmptyStore(),
            ClosedBarEngine(config.session_windows(), config.engine_params()),
            None,
            path,
            restored,
        )
        restored_trader.load()
        assert restored.groups["london:example"].state == "expired"
        assert not restored.resting_orders()
