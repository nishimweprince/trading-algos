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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from .config import Settings
from .execution import ExecutionClient, ExecutionResult, ExecutionState, operation_id_for
from .logging_config import log_event
from .models import Candle, EngineEvent, EntryMode

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

    @property
    def resting(self) -> bool:
        """Still potentially live at the broker: submitted, not filled, not cancelled."""
        return (
            self.order_id is not None
            and self.position_id is None
            and self.state
            not in {
                ExecutionState.REJECTED.value,
                "cancelled",
            }
        )


class ExecutionBridge:
    """Places, cancels and amends broker orders in response to engine events.

    Constructed only when ``MARKET_EXECUTION_MODE`` is not ``off``. In ``shadow`` the exact
    payload is built, recorded and surfaced, but no request is made — so payload shape and
    the live view can be checked against real sessions before anything is sent.
    """

    def __init__(self, settings: Settings, client: ExecutionClient) -> None:
        self._s = settings
        self._client = client
        self.mode = settings.market_execution_mode
        self.orders: dict[str, dict[str, TrackedOrder]] = {}
        self.consecutive_failures = 0
        self.halted_reason: str | None = None

    # ---- state -----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
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

    def tracked(self) -> list[TrackedOrder]:
        return [order for legs in self.orders.values() for order in legs.values()]

    def resting_orders(self) -> list[TrackedOrder]:
        return [order for order in self.tracked() if order.resting]

    # ---- event handling --------------------------------------------------------

    async def handle(self, event: EngineEvent, bar: Candle) -> None:
        if self.halted_reason is not None:
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
            return
        result = await self._client.submit(order.payload)
        self._absorb(order, result)

    async def _on_cancelled(self, event: EngineEvent, _bar: Candle) -> None:
        detail = event.detail
        pair_id = str(detail.get("pair_id"))
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
        order = (self.orders.get(pair_id) or {}).get(side)
        if order is None:
            return
        order.state = ExecutionState.SUCCEEDED.value
        if not self.mode.sends_orders:
            order.fill_price = _as_float(event.detail.get("entry"))
            return
        result = await self._client.get_operation(UUID(order.operation_id))
        self._absorb(order, result)

    async def _on_stop_moved(self, event: EngineEvent, _bar: Candle) -> None:
        pair_id = str(event.detail.get("pair_id"))
        side = str(event.detail.get("side") or "")
        new_sl = _as_float(event.detail.get("new_sl"))
        order = (self.orders.get(pair_id) or {}).get(side)
        if order is None or new_sl is None:
            return
        if not self.mode.sends_orders or order.position_id is None:
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
        legs = self.orders.get(pair_id)
        if not legs:
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
        for order in self.resting_orders():
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
        for order in self.tracked():
            if order.state not in {ExecutionState.PENDING.value, ExecutionState.UNKNOWN.value}:
                continue
            result = await self._client.get_operation(UUID(order.operation_id))
            if result.state is ExecutionState.NOT_FOUND:
                order.state = "not_submitted"
                continue
            self._absorb(order, result)

    def _absorb(self, order: TrackedOrder, result: ExecutionResult) -> None:
        """Fold a gateway response into tracked state and the failure counter."""
        alias = self._client.account
        if (order_id := result.order_ids.get(alias)) is not None:
            order.order_id = order_id
        for target in (result.response or {}).get("targets", []):
            if isinstance(target, dict) and target.get("account") == alias:
                if isinstance(position_id := target.get("position_id"), int):
                    order.position_id = position_id
        if (price := result.fill_price(alias)) is not None:
            order.fill_price = price
        order.state = result.state.value
        order.reason = result.reason

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
        """GTD expiry as a backstop one bar behind the engine's own cancel.

        The engine cancels an untriggered bracket after ``OCO_EXPIRY_BARS`` bars. Giving the
        broker an extra bar means the two never race: normally the engine's cancel lands
        first, and if this process dies the broker still cleans up on its own.
        """
        if not isinstance(expiry_bars, int) or expiry_bars <= 0:
            return None
        minutes = (expiry_bars + 1) * self._s.engine_params().timeframe_minutes
        return bar.ts.astimezone(UTC) + timedelta(minutes=minutes)


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _now() -> datetime:
    return datetime.now(tz=UTC)
