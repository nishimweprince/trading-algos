"""Turns engine events into broker orders.

The engine already decides everything: by the time ``entry_order_staged`` fires, the prop
guard, session and concurrency caps, and risk percentages have all passed in
``_accept_structure``, and a resting bracket already consumes concurrency and risk budget.
So this bridge places orders, it does not re-judge them.

Ordering note that shapes the code below: on a fill the engine emits
``entry_order_cancelled(reason="oco_sibling")`` *before* the ``entry``. The cancel means
"the other side is dead", never "the structure was abandoned".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from .config import Settings
from .execution import ExecutionResult, ExecutionState, operation_id_for
from .execution_protocols import (
    BrokerLifecycle,
    CancellationClient,
    ExecutionCapabilities,
    ExecutionTransport,
    MarketEntryClient,
    OcoGroupClient,
    PendingEntryClient,
    ProtectionClient,
)
from .logging_config import log_event
from .models import Candle, EngineEvent, EntryMode, ExecutionProvider, Mt5OcoExecution

logger = logging.getLogger(__name__)

Side = Literal["long", "short"]
_DIRECTION: dict[str, str] = {"long": "buy", "short": "sell"}


@dataclass
class TrackedOrder:
    """One leg of one structure, as far as the broker is concerned."""

    pair_id: str
    side: Side
    operation_id: str
    submitted_at: str
    state: str = ExecutionState.PENDING.value
    order_id: int | None = None
    position_id: int | None = None
    fill_price: float | None = None
    entry_price: float | None = None
    reason: str | None = None
    shadow: bool = False
    payload: dict[str, Any] = field(default_factory=dict)
    broker_state: str = BrokerLifecycle.UNAVAILABLE.value
    decision_at: str | None = None
    observed_at: str | None = None
    observation_delay_seconds: float | None = None
    executed_volume: str | None = None
    applied_protection: dict[str, Any] = field(default_factory=dict)
    engine_closed: bool = False
    broker_resting: bool | None = None

    @property
    def resting(self) -> bool:
        """Still potentially live at the broker: submitted, not filled, not cancelled."""
        if self.payload.get("group_id") is not None:
            return self.broker_resting is not False
        if (
            self.payload.get("execution_type") == "market"
            and self.state == ExecutionState.SUCCEEDED
        ):
            return False
        return (
            self.order_id is not None
            and self.position_id is None
            and self.state
            not in {
                ExecutionState.REJECTED.value,
                "cancelled",
            }
        )


@dataclass
class TrackedOcoGroup:
    pair_id: str
    group_id: str
    payload: dict[str, Any]
    state: str = "dispatching"
    response: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    shadow: bool = False
    engine_prediction: dict[str, Any] = field(default_factory=dict)
    cancel_reason: str | None = None


class ExecutionBridge:
    """Places, cancels and amends broker orders in response to engine events.

    Constructed only when ``MARKET_EXECUTION_MODE`` is not ``off``. In ``shadow`` the exact
    payload is built, recorded and surfaced, but no request is made — so payload shape and
    the live view can be checked against real sessions before anything is sent.
    """

    def __init__(self, settings: Settings, client: ExecutionTransport) -> None:
        self._s = settings
        self._client = client
        self.mode = settings.market_execution_mode
        self.orders: dict[str, dict[str, TrackedOrder]] = {}
        self.consecutive_failures = 0
        self.halted_reason: str | None = None
        self.persist: Callable[[], None] | None = None
        self.local_brackets: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, TrackedOcoGroup] = {}

    @property
    def execution_path(self) -> str:
        if self._s.execution_provider is ExecutionProvider.CTRADER:
            return "broker_pending"
        if self._s.entry_mode is EntryMode.OCO_BRACKET:
            return self._s.mt5_oco_execution.value
        return "market"

    @property
    def capabilities(self) -> ExecutionCapabilities:
        if self._s.execution_provider is ExecutionProvider.MT5:
            return getattr(self._client, "capabilities", ExecutionCapabilities(market_entry=True))
        return ExecutionCapabilities(
            pending_entry=True, cancellation=True, inventory=True, protection_amendment=True
        )

    def _persist(self) -> None:
        if self.persist is not None:
            self.persist()

    # ---- state -----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "mode": self.mode.value,
            "execution_path": self.execution_path,
            "local_brackets": self.local_brackets,
            "groups": {key: asdict(group) for key, group in self.groups.items()},
            "halted_reason": self.halted_reason,
            "orders": {
                pair_id: {side: asdict(order) for side, order in legs.items()}
                for pair_id, legs in self.orders.items()
            },
        }

    def restore(self, payload: dict[str, Any]) -> None:
        self.halted_reason = payload.get("halted_reason")
        restored: dict[str, dict[str, TrackedOrder]] = {}
        for pair_id, legs in (payload.get("orders") or {}).items():
            if not isinstance(legs, dict):
                continue
            restored[pair_id] = {
                side: TrackedOrder(**raw) for side, raw in legs.items() if isinstance(raw, dict)
            }
        self.orders = restored
        self.local_brackets = payload.get("local_brackets") or {}
        self.groups = {
            key: TrackedOcoGroup(**raw) for key, raw in (payload.get("groups") or {}).items()
        }
        saved_path = payload.get("execution_path")
        if saved_path is not None and saved_path != self.execution_path and self.orders:
            self.halted_reason = (
                "execution path changed with tracked orders; reconcile before rollout"
            )

    def tracked(self) -> list[TrackedOrder]:
        return [order for legs in self.orders.values() for order in legs.values()]

    def resting_orders(self) -> list[TrackedOrder]:
        return [order for order in self.tracked() if order.resting]

    # ---- event handling --------------------------------------------------------

    async def handle(self, event: EngineEvent, bar: Candle) -> None:
        if self.halted_reason is not None and (
            event.kind == "entry_order_staged"
            or (event.kind == "entry" and str(event.detail.get("pair_id")) not in self.groups)
        ):
            return
        handler = {
            "entry_order_staged": self._on_staged,
            "entry_order_cancelled": self._on_cancelled,
            "entry": self._on_entry,
            "be_ratchet_armed": self._on_stop_moved,
            "exit": self._on_exit,
            "prop_guard_breached": self._on_breach,
        }.get(event.kind)
        if handler is not None:
            await handler(event, bar)

    async def _on_staged(self, event: EngineEvent, bar: Candle) -> None:
        detail = event.detail
        if detail.get("entry_mode") != EntryMode.OCO_BRACKET.value:
            # Only the bracket modes rest orders at the broker. hedge_pair opens both legs
            # immediately and would need market orders on `entry` instead.
            return
        pair_id = str(detail.get("pair_id"))
        if self._s.execution_provider is ExecutionProvider.MT5:
            if self._s.mt5_oco_execution is Mt5OcoExecution.LOCAL_MARKET:
                self.local_brackets.setdefault(pair_id, dict(detail))
                self._persist()
                return
            if self._s.mt5_oco_execution is Mt5OcoExecution.BROKER_PENDING:
                await self._stage_oco_group(event, bar)
                return
        if pair_id in self.orders:
            # Replayed staging never replaces a dispatch intent or accepted leg.
            return
        if not isinstance(self._client, PendingEntryClient):
            await self.halt("execution client does not support pending entries")
            return
        sl_dist = _as_float(detail.get("sl_dist"))
        target_r = _as_float(detail.get("target_r"))
        upper = _as_float(detail.get("upper_trigger"))
        lower = _as_float(detail.get("lower_trigger"))
        if None in (sl_dist, target_r, upper, lower) or not sl_dist:
            log_event("execution_stage_incomplete", level=logging.ERROR, pair_id=pair_id)
            return

        expires_at = self._expiry(bar, detail.get("expiry_bars"))
        if expires_at is not None and expires_at <= _now():
            # The bracket would already have expired before it could rest. That means the
            # bar this was derived from is stale, so its trigger levels are stale too —
            # placing the order would be trading on a price that has already moved.
            log_event(
                "execution_stale_bar_skipped",
                level=logging.WARNING,
                pair_id=pair_id,
                bar_ts=bar.ts.isoformat(),
                expires_at=expires_at.isoformat(),
            )
            return
        legs: dict[str, TrackedOrder] = {}
        for side, entry_price in (("long", upper), ("short", lower)):
            operation_id = operation_id_for(symbol=self._s.symbol, pair_id=pair_id, side=side)
            payload = self._client.build_stop_entry(
                operation_id=operation_id,
                # NOT the bar timestamp: the gateway rejects anything older than
                # SIGNAL_MAX_AGE_SECONDS (60s), and an H1 bar close is already an hour old.
                occurred_at=_now(),
                symbol=self._s.symbol,
                direction=_DIRECTION[side],  # type: ignore[arg-type]
                entry_price=entry_price,  # type: ignore[arg-type]
                stop_distance=sl_dist,
                target_distance=sl_dist * target_r,  # type: ignore[operator]
                expires_at=expires_at,
                note=f"{pair_id}|{side}",
            )
            legs[side] = TrackedOrder(
                pair_id=pair_id,
                side=side,  # type: ignore[arg-type]
                operation_id=str(operation_id),
                submitted_at=_now().isoformat(),
                entry_price=entry_price,
                shadow=not self.mode.sends_orders,
                payload=payload,
            )
        self.orders[pair_id] = legs
        self._persist()

        for order in legs.values():
            await self._submit(order)
        log_event(
            "execution_bracket_staged",
            session=event.session,
            pair_id=pair_id,
            mode=self.mode.value,
            upper=upper,
            lower=lower,
            expires_at=expires_at.isoformat() if expires_at else None,
        )

    async def _submit(self, order: TrackedOrder) -> None:
        if not self.mode.sends_orders:
            order.state = "shadow"
            order.broker_state = BrokerLifecycle.NOT_SUBMITTED.value
            self._persist()
            return
        order.broker_state = BrokerLifecycle.DISPATCHING.value
        self._persist()
        result = await self._client.submit(order.payload)
        self._absorb(order, result)
        self._persist()

    async def _on_cancelled(self, event: EngineEvent, _bar: Candle) -> None:
        detail = event.detail
        pair_id = str(detail.get("pair_id"))
        if self.groups.get(pair_id) is not None:
            if detail.get("reason") != "oco_sibling":
                await self._cancel_oco_group(self.groups[pair_id], str(detail.get("reason")))
            # Gateway broker fills own sibling cancellation, independently of paper predictions.
            return
        if self.execution_path == "local_market":
            if detail.get("reason") != "oco_sibling":
                self.local_brackets.pop(pair_id, None)
                self._persist()
            return
        legs = self.orders.get(pair_id)
        if not legs:
            return
        reason = detail.get("reason")
        if reason == "oco_sibling":
            # The engine names the side it cancelled; the other side is the one that filled.
            side = str(detail.get("cancelled_side", ""))
            targets = [legs[side]] if side in legs else []
        else:
            targets = list(legs.values())
        for order in targets:
            await self._cancel(order, reason=str(reason))

    async def _cancel(self, order: TrackedOrder, *, reason: str) -> None:
        if not self.mode.sends_orders or order.order_id is None:
            order.state = "cancelled"
            order.reason = reason
            return
        if not order.resting:
            return
        if not isinstance(self._client, CancellationClient):
            order.reason = "broker cancellation unavailable"
            self._persist()
            return
        self._persist()
        result = await self._client.cancel_order(
            operation_id=operation_id_for(
                symbol=self._s.symbol, pair_id=order.pair_id, side=f"{order.side}-cancel"
            ),
            occurred_at=_now(),
            order_id=order.order_id,
        )
        # A GTD order the broker already expired is gone, and cancelling it returns
        # order_not_found. That is the intended end state, not a failure.
        if result.state is ExecutionState.REJECTED and "not_found" in (result.reason or ""):
            order.state = "cancelled"
            order.reason = "already_gone"
            return
        self._absorb(order, result)
        if result.state is not ExecutionState.UNKNOWN:
            order.state = "cancelled"
            order.reason = reason

    async def _on_entry(self, event: EngineEvent, _bar: Candle) -> None:
        """Record the fill and capture the position id that stop amendments will need."""
        pair_id = str(event.detail.get("pair_id"))
        side = str(event.detail.get("primary_side") or "")
        group = self.groups.get(pair_id)
        if group is not None:
            group.engine_prediction = {
                "side": side,
                "entry": event.detail.get("entry"),
                "decision_at": event.ts.isoformat(),
            }
            self._persist()
            await self._refresh_oco_group(group)
            return
        if self._s.mt5_oco_execution is Mt5OcoExecution.BROKER_PENDING:
            # Never turn an unexecuted broker bracket into a market entry on a paper fill.
            return
        order = (self.orders.get(pair_id) or {}).get(side)
        if order is not None and order.payload.get("execution_type") == "market":
            # The engine may replay its persisted terminal event after a restart.
            # The restored order is already reconciled during startup.
            return
        if order is None and self._s.execution_provider is ExecutionProvider.MT5:
            await self._submit_mt5_market_entry(event, bar=_bar, pair_id=pair_id, side=side)
            return
        if order is None:
            return
        order.state = ExecutionState.SUCCEEDED.value
        if not self.mode.sends_orders:
            order.fill_price = _as_float(event.detail.get("entry"))
            return
        result = await self._client.get_operation(UUID(order.operation_id))
        self._absorb(order, result)

    async def _submit_mt5_market_entry(
        self, event: EngineEvent, *, bar: Candle, pair_id: str, side: str
    ) -> None:
        sl_dist = _as_float(event.detail.get("sl_dist"))
        entry_price = _as_float(event.detail.get("entry"))
        if side not in _DIRECTION or sl_dist is None or sl_dist <= 0:
            log_event(
                "execution_entry_incomplete",
                level=logging.ERROR,
                pair_id=pair_id,
                side=side or None,
            )
            return
        operation_id = operation_id_for(symbol=self._s.symbol, pair_id=pair_id, side=side)
        if not isinstance(self._client, MarketEntryClient):
            await self.halt("execution client does not support market entries")
            return
        observed_at = _now()
        age = (observed_at - bar.ts.astimezone(UTC)).total_seconds()
        bracket = self.local_brackets.get(pair_id) or {}
        target_r = _as_float(event.detail.get("target_r"))
        if target_r is None:
            target_r = _as_float(bracket.get("target_r"))
        if target_r is None:
            target_r = self._s.rr
        payload = self._client.build_market_entry(
            operation_id=operation_id,
            occurred_at=observed_at,
            symbol=self._s.symbol,
            direction=_DIRECTION[side],
            stop_distance=sl_dist,
            target_distance=sl_dist * target_r,
            note=f"{pair_id}|{side}",
        )
        order = TrackedOrder(
            pair_id=pair_id,
            side=side,  # type: ignore[arg-type]
            operation_id=str(operation_id),
            submitted_at=_now().isoformat(),
            entry_price=entry_price,
            shadow=not self.mode.sends_orders,
            payload=payload,
            decision_at=event.ts.isoformat(),
            observed_at=observed_at.isoformat(),
            observation_delay_seconds=(observed_at - event.ts.astimezone(UTC)).total_seconds(),
        )
        self.orders[pair_id] = {side: order}
        self.local_brackets.pop(pair_id, None)
        self._persist()
        if age > self._s.execution_max_observation_age_seconds or age < -5:
            order.state = "skipped"
            order.broker_state = BrokerLifecycle.NOT_SUBMITTED.value
            order.reason = "stale_observation" if age > 0 else "future_observation"
            self._persist()
            return
        if self.mode.sends_orders:
            ready, reason = await self._client.trading_ready()
            if not ready:
                order.state = "skipped"
                order.broker_state = BrokerLifecycle.NOT_SUBMITTED.value
                order.reason = f"gateway_not_ready: {reason}"
                self._persist()
                return
        await self._submit(order)
        log_event(
            "execution_market_signal_submitted",
            session=event.session,
            pair_id=pair_id,
            side=side,
            mode=self.mode.value,
        )

    async def _on_stop_moved(self, event: EngineEvent, _bar: Candle) -> None:
        pair_id = str(event.detail.get("pair_id"))
        side = str(event.detail.get("side") or "")
        new_sl = _as_float(event.detail.get("new_sl"))
        order = (self.orders.get(pair_id) or {}).get(side)
        if order is None or new_sl is None:
            return
        if not self.mode.sends_orders or order.position_id is None:
            return
        if not isinstance(self._client, ProtectionClient):
            return
        result = await self._client.amend_protection(
            operation_id=operation_id_for(
                symbol=self._s.symbol, pair_id=pair_id, side=f"{side}-protect"
            ),
            occurred_at=_now(),
            position_id=order.position_id,
            stop_loss=new_sl,
        )
        self._absorb(order, result)

    async def _on_exit(self, event: EngineEvent, _bar: Candle) -> None:
        pair_id = str(event.detail.get("pair_id"))
        if pair_id in self.groups:
            group = self.groups[pair_id]
            group.engine_prediction["closed"] = True
            await self._cancel_oco_group(group, "structure_closed")
            for order in (self.orders.get(pair_id) or {}).values():
                order.engine_closed = True
            self._persist()
            return
        legs = self.orders.get(pair_id)
        if not legs:
            return
        if self._s.execution_provider is ExecutionProvider.MT5:
            # A paper exit cannot prove broker closure. Retain IDs and replay tombstones.
            for order in legs.values():
                order.engine_closed = True
            self._persist()
            return
        # The broker's own stop or target closes the position; the engine's exit is the
        # record of it. Only drop tracking once nothing is left resting.
        for order in legs.values():
            if order.resting:
                await self._cancel(order, reason="structure_closed")
        if all(not order.resting for order in legs.values()):
            self.orders.pop(pair_id, None)

    async def _on_breach(self, event: EngineEvent, _bar: Candle) -> None:
        """A prop-guard breach only blocks new structures in the engine. Orders resting at
        the broker are unaffected by that, so cancel them here."""
        await self.halt(f"prop_guard: {event.detail.get('reason')}")

    # ---- control ---------------------------------------------------------------

    async def halt(self, reason: str) -> None:
        """Cancel every resting order and refuse further work until restarted."""
        for group in self.groups.values():
            await self._cancel_oco_group(group, "halted")
        for order in self.resting_orders():
            if order.pair_id in self.groups:
                continue
            await self._cancel(order, reason="halted")
        self.halted_reason = reason
        log_event("execution_halted", level=logging.ERROR, reason=reason)

    async def reconcile(self) -> None:
        """After a restart, ask the gateway what it already knows before submitting anything.

        Deterministic operation ids mean a resubmit would be recognised as a duplicate, but
        resolving state first keeps the local view honest rather than relying on that.
        """
        if not self.mode.sends_orders:
            return
        for group in self.groups.values():
            await self._refresh_oco_group(group)
            if group.cancel_reason is not None:
                await self._cancel_oco_group(group, group.cancel_reason)
        if (
            self.halted_reason is not None
            and self.halted_reason.startswith("MT5 OCO ")
            and not any(group.state in {"unknown", "halted"} for group in self.groups.values())
        ):
            self.halted_reason = None
        for order in self.tracked():
            if order.pair_id in self.groups:
                continue
            if order.state not in {ExecutionState.PENDING.value, ExecutionState.UNKNOWN.value}:
                continue
            result = await self._client.get_operation(UUID(order.operation_id))
            if result.state is ExecutionState.NOT_FOUND:
                order.state = "not_submitted"
                continue
            self._absorb(order, result)
        self._persist()

    async def _stage_oco_group(self, event: EngineEvent, bar: Candle) -> None:
        pair_id = str(event.detail.get("pair_id"))
        if pair_id in self.groups:
            return
        if not isinstance(self._client, OcoGroupClient):
            await self.halt("MT5 gateway client does not support OCO groups")
            return
        if any(
            existing.engine_prediction.get("closed")
            and any(
                order.broker_state
                not in {"closed", "cancelled", "expired", "rejected", "not_submitted"}
                for order in (self.orders.get(existing.pair_id) or {}).values()
            )
            for existing in self.groups.values()
        ):
            await self.halt("broker exposure remains after a strategy exit")
            return
        sl = _as_float(event.detail.get("sl_dist"))
        target_r = _as_float(event.detail.get("target_r"))
        upper = _as_float(event.detail.get("upper_trigger"))
        lower = _as_float(event.detail.get("lower_trigger"))
        expiry = self._expiry(bar, event.detail.get("expiry_bars"))
        if (
            sl is None
            or target_r is None
            or upper is None
            or lower is None
            or expiry is None
            or sl <= 0
            or target_r <= 0
            or lower >= upper
        ):
            await self.halt(
                "MT5 OCO staging requires ordered triggers, protection, and bounded expiry"
            )
            return
        now = _now()
        payload = self._client.build_oco_group(
            group_id=operation_id_for(symbol=self._s.symbol, pair_id=pair_id, side="oco-group"),
            occurred_at=now,
            decision_at=event.ts,
            symbol=self._s.symbol,
            upper_trigger=upper,
            lower_trigger=lower,
            stop_distance=sl,
            target_distance=sl * target_r,
            expires_at=expiry,
        )
        group = TrackedOcoGroup(
            pair_id=pair_id,
            group_id=str(payload["group_id"]),
            payload=payload,
            shadow=not self.mode.sends_orders,
        )
        self.groups[pair_id] = group
        self._persist()
        age = (now - bar.ts.astimezone(UTC)).total_seconds()
        if expiry <= now or age > self._s.execution_max_observation_age_seconds or age < -5:
            group.state = "skipped"
            group.reason = "stale_or_future_bracket"
            self._persist()
            return
        if not self.mode.sends_orders:
            group.state = "shadow"
            self._persist()
            return
        ready, reason = await self._client.trading_ready()
        if not ready:
            group.state = "skipped"
            group.reason = f"gateway_not_ready: {reason}"
            self._persist()
            return
        self._absorb_oco_group(group, await self._client.submit_group(payload))

    async def _refresh_oco_group(self, group: TrackedOcoGroup) -> None:
        if not self.mode.sends_orders or group.state in {"skipped", "shadow"}:
            return
        if isinstance(self._client, OcoGroupClient):
            self._absorb_oco_group(group, await self._client.get_group(UUID(group.group_id)))

    async def _cancel_oco_group(self, group: TrackedOcoGroup, reason: str) -> None:
        if group.state in {"skipped", "shadow"} or not self.mode.sends_orders:
            return
        group.cancel_reason = reason
        self._persist()
        if isinstance(self._client, OcoGroupClient):
            self._absorb_oco_group(
                group, await self._client.cancel_group(UUID(group.group_id), reason)
            )

    def _absorb_oco_group(self, group: TrackedOcoGroup, result: ExecutionResult) -> None:
        group.reason = result.reason
        if result.response is not None and isinstance(result.response.get("legs"), dict):
            group.response = result.response
            group.state = str(result.response.get("state", "unknown"))
            legs: dict[str, TrackedOrder] = {}
            for side in ("long", "short"):
                raw = result.response["legs"].get(side) or {}
                leg_state = str(raw.get("state", "unknown"))
                position_ids = raw.get("position_ids") or []
                legs[side] = TrackedOrder(
                    pair_id=group.pair_id,
                    side=side,
                    operation_id=str(raw.get("signal_id", group.group_id)),
                    submitted_at=str(group.payload["occurred_at"]),
                    state=result.state.value,
                    order_id=raw.get("order_id"),
                    position_id=position_ids[0] if position_ids else None,
                    broker_state=leg_state,
                    fill_price=float(raw["fill_price"]) if raw.get("fill_price") else None,
                    broker_resting=raw.get("resting"),
                    entry_price=float(
                        group.payload["upper_trigger" if side == "long" else "lower_trigger"]
                    ),
                    executed_volume=str(raw.get("executed_volume", "0")),
                    applied_protection=raw.get("applied_protection") or {},
                    engine_closed=bool(group.engine_prediction.get("closed")),
                    payload={"execution_type": "stop", "group_id": group.group_id},
                )
            self.orders[group.pair_id] = legs
            if not any(order.broker_resting is not False for order in legs.values()) and not any(
                order.broker_state in {"unknown", "dispatching"} for order in legs.values()
            ):
                group.cancel_reason = None
        elif result.state is ExecutionState.NOT_FOUND:
            group.state = "unknown"
            group.reason = "gateway group missing; dispatch will not be retried"
        elif result.state is ExecutionState.REJECTED:
            group.state = "rejected"
        else:
            group.state = "unknown"
        if group.state in {"unknown", "halted"}:
            self.halted_reason = f"MT5 OCO {group.group_id}: {group.reason or group.state}"
        self._persist()

    def _absorb(self, order: TrackedOrder, result: ExecutionResult) -> None:
        """Fold a gateway response into tracked state and the failure counter."""
        alias = self._client.account
        if (order_id := result.order_ids.get(alias)) is not None:
            order.order_id = order_id
        for target in (result.response or {}).get("targets", []):
            if isinstance(target, dict) and target.get("account") == alias:
                if isinstance(position_id := target.get("position_id"), int):
                    order.position_id = position_id
                if target.get("broker_state") is not None:
                    order.broker_state = str(target["broker_state"])
                if target.get("executed_volume") is not None:
                    order.executed_volume = str(target["executed_volume"])
                if isinstance(protection := target.get("applied_protection"), dict):
                    order.applied_protection = protection
        if (price := result.fill_price(alias)) is not None:
            order.fill_price = price
        order.state = result.state.value
        order.reason = result.reason
        if result.state is ExecutionState.UNKNOWN:
            order.broker_state = BrokerLifecycle.UNKNOWN.value
        elif result.state is ExecutionState.REJECTED:
            order.broker_state = BrokerLifecycle.REJECTED.value

        if result.state is ExecutionState.UNKNOWN:
            self.consecutive_failures += 1
            log_event(
                "execution_call_failed",
                level=logging.ERROR,
                pair_id=order.pair_id,
                side=order.side,
                reason=result.reason,
                consecutive=self.consecutive_failures,
            )
            if self.consecutive_failures >= self._s.execution_max_consecutive_failures:
                self.halted_reason = f"{self.consecutive_failures} consecutive execution failures"
        else:
            self.consecutive_failures = 0
        if result.state is ExecutionState.REJECTED:
            log_event(
                "execution_rejected",
                level=logging.ERROR,
                pair_id=order.pair_id,
                side=order.side,
                reason=result.reason,
            )

    def _expiry(self, bar: Candle, expiry_bars: object) -> datetime | None:
        """Wall-clock watchdog deadline with one parent-bar grace interval.

        Engine expiry counts eligible parent bars; this backstop counts elapsed
        time. Market closures can make the broker expire first. Both deadlines
        are visible in the payload; neither promises to avoid fill/cancel races.
        """
        if not isinstance(expiry_bars, int) or expiry_bars <= 0:
            return None
        minutes = (expiry_bars + 1) * self._s.engine_params().timeframe_minutes
        return bar.ts.astimezone(UTC) + timedelta(minutes=minutes)


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _now() -> datetime:
    return datetime.now(tz=UTC)
