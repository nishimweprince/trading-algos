"""The execution bridge: model -> policy -> risk -> orders.

Modelled on ``backtesting_service/execution_bridge.py``: deterministic
operation ids, reconcile before trusting local state, halt after repeated
UNKNOWN outcomes, and shadow builds exactly what testnet would send.

Per grid sample and symbol: score the sample, ask ``SymbolPolicy`` what to do,
run every proposed order past ``RiskState.check_order``, and hand what passes
to a venue:

- **shadow** (``ShadowVenue``): nothing is sent. A pessimistic simulator fills
  a resting maker order only when a trade prints *through* its price, and a
  market order at the opposite touch.
- **testnet** (``GatewayVenue``): orders go to execution-service over HTTP and
  fills are read back by polling the operation. An UNKNOWN outcome is
  reconciled through ``get_operation``, never resubmitted. Orders are priced
  from demo trading's own touch; the signal is still mainnet's.

Halts (kill switch, loss limits, execution faults): cancel everything, close
every cycle reduce-only, flatten as a backstop. Only an operator ack resumes.

The core is synchronous so a replay can drive shadow mode deterministically
from recorded event times; only the gateway's I/O is asynchronous.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from ta_clients.execution import ExecutionResult, ExecutionState, operation_id_for
from ta_contracts.execution import OrderRequest
from ta_core.logging_config import log_event

from .alerts import Alerts
from .config import ExecutionMode
from .model import LoadedModel
from .policy import (
    Action,
    Cancel,
    Close,
    Context,
    Cycle,
    Filters,
    Halt,
    Phase,
    Place,
    SymbolPolicy,
)
from .risk import OrderIntent, RiskState
from .trades import ADVERSE_HORIZONS_S, TradeBook, trade_record

__all__ = [
    "DEFAULT_FILTERS",
    "ExecutionBridge",
    "GatewayVenue",
    "ShadowVenue",
    "TrackedOrder",
]

SOURCE = "ofi_scalper"
NS = 1_000_000_000
TERMINAL = {"filled", "closed", "partially_filled_final", "cancelled", "rejected"}
QUOTE_MAX_AGE_MS = 2_000
CONTROL_SECONDS = 5.0

# Mainnet USDⓈ-M filters (2026-10), for replays that have no exchangeInfo.
DEFAULT_FILTERS = {
    "BTCUSDT": Filters(Decimal("0.1"), Decimal("0.001"), Decimal("0.001"), Decimal("100")),
    "ETHUSDT": Filters(Decimal("0.01"), Decimal("0.001"), Decimal("0.001"), Decimal("20")),
}


@dataclass
class TrackedOrder:
    cycle_id: str
    symbol: str
    leg: str  # entry | tp | close<n>
    side: str
    qty: str
    price: str | None  # None: market
    post_only: bool
    reduce_only: bool
    operation_id: str
    created_ns: int
    state: str = "new"
    executed: str = "0"
    avg_price: float | None = None
    order_id: int | None = None
    terminal: bool = False
    reason: str | None = None
    sent_ns: int | None = None
    acked_ns: int | None = None
    cancel_requested: bool = False
    cancel_sent: bool = False

    @property
    def key(self) -> str:
        return f"{self.cycle_id}|{self.leg}"

    @property
    def market(self) -> bool:
        return self.price is None

    @property
    def rtt_ms(self) -> float | None:
        if self.sent_ns is None or self.acked_ns is None:
            return None
        return round((self.acked_ns - self.sent_ns) / 1e6, 3)


class Venue(Protocol):
    def submit(self, order: TrackedOrder, t_ns: int) -> None: ...

    def cancel(self, order: TrackedOrder, t_ns: int) -> None: ...


# --- shadow ----------------------------------------------------------------------------


class ShadowVenue:
    """Would-have fills, pessimistic: a maker order needs a print through its price."""

    def __init__(self, bridge: ExecutionBridge) -> None:
        self.bridge = bridge
        self.touch: dict[str, tuple[float, float]] = {}
        self.resting: dict[str, TrackedOrder] = {}

    def submit(self, order: TrackedOrder, t_ns: int) -> None:
        order.state, order.sent_ns, order.acked_ns = "shadow", t_ns, t_ns
        touch = self.touch.get(order.symbol)
        if order.market:
            if touch is None:
                self.bridge.update(order, Decimal(0), None, True, t_ns, "no_touch")
                return
            price = touch[1] if order.side == "buy" else touch[0]
            self.bridge.update(order, Decimal(order.qty), price, True, t_ns)
            return
        assert order.price is not None
        price = float(order.price)
        if touch is not None and order.post_only:
            crosses = price >= touch[1] if order.side == "buy" else price <= touch[0]
            if crosses:
                self.bridge.update(order, Decimal(0), None, True, t_ns, "post_only_would_take")
                return
        self.resting[order.key] = order

    def cancel(self, order: TrackedOrder, t_ns: int) -> None:
        if self.resting.pop(order.key, None) is not None:
            self.bridge.update(
                order, Decimal(order.executed), order.avg_price, True, t_ns, "cancelled"
            )

    def on_trade(self, symbol: str, price: float, t_ns: int) -> None:
        for key, order in list(self.resting.items()):
            if order.symbol != symbol:
                continue
            assert order.price is not None
            limit = float(order.price)
            through = price < limit if order.side == "buy" else price > limit
            if through:
                del self.resting[key]
                self.bridge.update(order, Decimal(order.qty), limit, True, t_ns)


# --- testnet: execution-service over HTTP ---------------------------------------------


class GatewayVenue:
    """Sends through execution-service; fills come back by polling the operation."""

    def __init__(self, bridge: ExecutionBridge, client: Any, *, poll_ms: int) -> None:
        self.bridge = bridge
        self.client = client
        self.poll_seconds = poll_ms / 1000
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.controls: deque[dict[str, Any]] = deque(maxlen=50)

    # sync entry points (the bridge's core calls these)

    def submit(self, order: TrackedOrder, t_ns: int) -> None:
        order.state = "queued"
        self.queue.put_nowait(("submit", order))

    def cancel(self, order: TrackedOrder, t_ns: int) -> None:
        order.cancel_requested = True
        if order.order_id is not None and not order.cancel_sent:
            self.queue.put_nowait(("cancel", order))
        # else: sent once the gateway reports an order id

    def control(self, name: str, *args: Any, delay: float = 0.0) -> None:
        self.queue.put_nowait(("control", (name, args, delay)))

    # async side

    async def worker(self) -> None:
        while True:
            kind, item = await self.queue.get()
            try:
                if kind == "submit":
                    await self._submit(item)
                elif kind == "cancel":
                    await self._cancel(item)
                else:
                    await self._control(*item)
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the queue
                log_event(
                    "ofi_bridge_job_failed", level=logging.ERROR, kind=kind, error=repr(exc)[:200]
                )

    async def _submit(self, order: TrackedOrder) -> None:
        payload = self.bridge.payload(order)
        order.state = "sent"
        order.sent_ns = self.bridge.clock_ns()
        self.bridge.persist()  # sent: from here on, reconcile, never resubmit
        result = await self.client.submit(payload)
        order.acked_ns = self.bridge.clock_ns()
        self.absorb(order, result)

    async def _cancel(self, order: TrackedOrder) -> None:
        if order.terminal or order.cancel_sent or order.order_id is None:
            return
        order.cancel_sent = True
        result = await self.client.cancel_order(
            operation_id=operation_id_for(
                symbol=order.symbol, pair_id=order.cycle_id, side=f"{order.leg}-cancel"
            ),
            occurred_at=datetime.now(UTC),
            order_id=order.order_id,
        )
        if result.state is ExecutionState.UNKNOWN:
            self.bridge.note_unknown(f"cancel {order.key}: {result.reason}")
        # The original order's operation says what really happened; the poller reads it.

    async def _control(self, name: str, args: tuple[Any, ...], delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        result: ExecutionResult = await getattr(self.client, name)(*args)
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "control": name,
            "args": list(args),
            "state": result.state.value,
            "reason": result.reason,
            "response": result.response,
        }
        self.controls.append(entry)
        level = logging.INFO if result.state is ExecutionState.SUCCEEDED else logging.ERROR
        log_event("ofi_bridge_control", level=level, **entry)
        if result.state is ExecutionState.UNKNOWN:
            self.bridge.note_unknown(f"{name}: {result.reason}")

    async def poller(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            await self.poll_once()

    async def poll_once(self) -> None:
        for order in list(self.bridge.orders.values()):
            if order.terminal or order.state in {"new", "queued", "sent", "refused"}:
                continue
            result = await self.client.get_operation(UUID(order.operation_id))
            if result.state is ExecutionState.NOT_FOUND:
                # The gateway never recorded it, so it was never sent to Binance.
                self.bridge.update(
                    order, Decimal(0), None, True, self.bridge.clock_ns(), "not_submitted"
                )
                continue
            self.absorb(order, result, polled=True)

    def absorb(self, order: TrackedOrder, result: ExecutionResult, *, polled: bool = False) -> None:
        now = self.bridge.clock_ns()
        if result.state is ExecutionState.UNKNOWN:
            order.state = "unknown"
            order.reason = result.reason
            if not polled:
                self.bridge.note_unknown(f"{order.key}: {result.reason}")
            self.bridge.persist()
            return
        if not polled:
            self.bridge.note_ok()
        target = next(
            (
                t
                for t in (result.response or {}).get("targets", [])
                if isinstance(t, dict) and t.get("account") == self.client.account
            ),
            None,
        )
        if target is None:
            if result.state is ExecutionState.REJECTED:
                self.bridge.update(order, Decimal(0), None, True, now, result.reason)
            return
        state = str(target.get("state"))
        if isinstance(target.get("order_id"), int):
            order.order_id = target["order_id"]
        order.state = state
        executed = Decimal(str(target.get("executed_volume_lots") or "0"))
        price = target.get("execution_price")
        reason = target.get("error_code") or (result.reason if state == "rejected" else None)
        self.bridge.update(
            order,
            executed,
            float(price) if price is not None else None,
            state in TERMINAL,
            now,
            reason,
        )
        if order.cancel_requested and not order.terminal and not order.cancel_sent:
            self.queue.put_nowait(("cancel", order))


# --- the bridge -------------------------------------------------------------------------


class ExecutionBridge:
    def __init__(
        self,
        settings: Any,
        *,
        risk: RiskState,
        alerts: Alerts,
        model: LoadedModel | None,
        filters: dict[str, Filters] | None = None,
        client: Any | None = None,
        quotes: Any | None = None,
        state_dir: Path | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        fees: Callable[[str], tuple[float, float] | None] = lambda _symbol: None,
        book: TradeBook | None = None,
    ) -> None:
        self.settings = settings
        self.mode: ExecutionMode = settings.execution_mode
        self.risk = risk
        self.alerts = alerts
        self.model = model
        self.client = client
        self.quotes = quotes
        self.clock_ns = clock_ns
        self.fees = fees
        self.state_path = None if state_dir is None else state_dir / "bridge.json"
        self.book = book if book is not None else TradeBook(state_dir)
        self.policies: dict[str, SymbolPolicy] = {}
        if filters is not None:
            self.configure(filters)
        self.orders: dict[str, TrackedOrder] = {}
        self.last_sample: dict[str, dict[str, Any]] = {}
        self.consecutive_unknown = 0
        self.position_mismatch: str | None = None
        self._halt_handled = False
        self._marks: dict[str, dict[str, float]] = {}
        self._funding: dict[str, dict[str, Any]] = {}
        self._pending: list[Cycle] = []
        self.realized_usd = 0.0
        self.counts: dict[str, int] = {}
        self.venue_ready = self.mode is ExecutionMode.SHADOW
        self.gateway_reason: str | None = None
        self.shadow: ShadowVenue | None = None
        self.gateway: GatewayVenue | None = None
        self.venue: Venue
        if self.mode is ExecutionMode.TESTNET:
            if client is None:
                raise ValueError("testnet mode needs an execution client")
            self.gateway = GatewayVenue(self, client, poll_ms=settings.fill_poll_ms)
            self.venue = self.gateway
        else:
            self.shadow = ShadowVenue(self)
            self.venue = self.shadow
        self._tasks: list[asyncio.Task[None]] = []
        self.dead_man_armed = False

    def configure(self, filters: dict[str, Filters]) -> None:
        """One policy per symbol, once the order filters are known. No model: none."""
        if self.model is None:
            return
        for symbol, symbol_filters in filters.items():
            self.policies[symbol] = SymbolPolicy(symbol, self.model.policy, symbol_filters)

    # --- lifecycle (testnet I/O) -------------------------------------------------------

    async def start(self) -> None:
        self.restore()
        if self.gateway is None:
            return
        if self.quotes is not None:
            await self.quotes.start()
        self._tasks = [
            asyncio.create_task(self.gateway.worker(), name="ofi-bridge-worker"),
            asyncio.create_task(self._boot(), name="ofi-bridge-boot"),
        ]

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self.gateway is not None and self.dead_man_armed and not self._busy():
            # Clean stop with nothing working: disarm. Otherwise leave it armed so
            # Binance cancels what rests if this process does not come back.
            with contextlib.suppress(Exception):
                await self.client.dead_man(list(self.settings.binance_futures_symbols), 0)
        if self.quotes is not None:
            await self.quotes.close()
        self.flush_pending()
        self.persist()

    async def _boot(self) -> None:
        """Reconcile what the last process left before trusting anything local."""
        assert self.gateway is not None
        while True:
            ready, reason = await self.client.trading_ready()
            if ready:
                break
            self.gateway_reason = reason
            await asyncio.sleep(CONTROL_SECONDS)
        for order in list(self.orders.values()):
            if order.terminal:
                continue
            if order.state in {"new", "queued"}:
                # Never left this process: the gateway has never seen it.
                self.update(order, Decimal(0), None, True, self.clock_ns(), "not_submitted")
                continue
            order.state = "unknown"  # "sent" before the restart: ask, do not resend
        await self.gateway.poll_once()
        await self._check_positions()
        self.venue_ready = True
        self.gateway_reason = None
        self._tasks.append(asyncio.create_task(self.gateway.poller(), name="ofi-bridge-poller"))
        self._tasks.append(asyncio.create_task(self._control_loop(), name="ofi-bridge-control"))
        log_event("ofi_bridge_ready", mode=self.mode.value, restored_orders=len(self.orders))

    async def _check_positions(self) -> None:
        positions = await self.client.list_positions()
        actual: dict[str, Decimal] = {}
        for row in positions:
            qty = Decimal(str(row.get("volume_lots") or "0"))
            signed = qty if row.get("direction") == "buy" else -qty
            actual[str(row.get("instrument"))] = (
                actual.get(str(row.get("instrument")), Decimal(0)) + signed
            )
        problems = []
        for symbol in self.settings.binance_futures_symbols:
            expected = self._expected_position(symbol)
            if actual.get(symbol, Decimal(0)) != expected:
                problems.append(f"{symbol}: venue {actual.get(symbol, 0)} vs bridge {expected}")
        if problems:
            self.position_mismatch = "; ".join(problems)
            self.risk.execution_fault(f"position mismatch: {self.position_mismatch}")
            self.alerts.send(
                "position-mismatch",
                "OFI: a position the bridge did not open",
                [
                    self.position_mismatch,
                    "Halted; nothing was flattened. Resolve it on the gateway "
                    "(POST /v1/accounts/<alias>/flatten), then restart the scalper.",
                ],
                always=True,
            )

    def _expected_position(self, symbol: str) -> Decimal:
        policy = self.policies.get(symbol)
        cycle = policy.cycle if policy is not None else None
        if cycle is None:
            return Decimal(0)
        return cycle.remaining if cycle.side == "buy" else -cycle.remaining

    async def _control_loop(self) -> None:
        """Every few seconds: gateway readiness, the dead-man, equity."""
        symbols = list(self.settings.binance_futures_symbols)
        while True:
            ready, reason = await self.client.trading_ready()
            self.gateway_reason = None if ready else reason
            # Armed for as long as this process runs: if it dies, Binance cancels.
            assert self.gateway is not None
            self.gateway.control("dead_man", symbols, self.settings.dead_man_ms)
            self.dead_man_armed = True
            await self._update_equity_from_gateway()
            await asyncio.sleep(CONTROL_SECONDS)

    async def _update_equity_from_gateway(self) -> None:
        accounts = await self.client.list_accounts()
        wallet = next(
            (
                a.get("wallet_balance_usdt")
                for a in accounts
                if a.get("alias") == self.settings.execution_account
            ),
            None,
        )
        if wallet is None:
            return
        self.risk.update_equity(float(wallet) + self._unrealized())
        self._check_halt(self.clock_ns())

    # --- the hot path ------------------------------------------------------------------

    def on_sample(self, sample: dict[str, Any], gate: dict[str, Any] | None) -> None:
        symbol = sample["symbol"]
        t_ns = sample["t_ns"]
        self.last_sample[symbol] = sample
        bid, ask = self._quote(symbol, sample)
        if self.shadow is not None and bid is not None and ask is not None:
            self.shadow.touch[symbol] = (bid, ask)
        self._check_halt(t_ns)
        self._mark(symbol, t_ns, sample)
        policy = self.policies.get(symbol)
        if policy is None or self.model is None:
            return
        scores = self.model.scorer.score(sample)
        fees = self.fees(symbol)
        blocked = self._blocked(symbol, gate, bid, ask, fees)
        maker, taker = fees if fees is not None else (0.0, 0.0)
        step = policy.on_sample(
            Context(
                t_ns=t_ns,
                bid=bid,
                ask=ask,
                scores=scores,
                notional_usd=self.settings.order_notional_usd,
                maker_bp=maker,
                taker_bp=taker,
                blocked=blocked,
                threshold_mult=float((gate or {}).get("threshold_mult", 1.0)),
                size_mult=float((gate or {}).get("max_inventory_mult", 1.0)),
            )
        )
        refused = self.execute(step.actions, t_ns)
        if step.signal is not None:
            record = {
                **asdict(step.signal),
                "at": datetime.fromtimestamp(t_ns / 1e9, UTC).isoformat(),
                "model_version": self.model.version,
                "mode": self.mode.value,
                "gate": gate,
            }
            if step.signal.cycle_id is not None and step.signal.cycle_id in refused:
                record["action"] = f"refused:{refused[step.signal.cycle_id]}"
            self.count(f"signal_{record['action'].split(':')[0]}")
            self.book.signal(t_ns, record)
        self._collect(symbol, t_ns)
        if self.mode is ExecutionMode.SHADOW:
            self.risk.update_equity(
                self.settings.shadow_equity_usd + self.realized_usd + self._unrealized()
            )
            self._check_halt(t_ns)
        if step.actions:
            self.persist()

    def on_trade(self, symbol: str, t_ns: int, price: float) -> None:
        if self.shadow is not None:
            self.shadow.on_trade(symbol, price, t_ns)

    def _quote(self, symbol: str, sample: dict[str, Any]) -> tuple[float | None, float | None]:
        if self.mode is ExecutionMode.TESTNET:
            quote = self.quotes.get(symbol, QUOTE_MAX_AGE_MS) if self.quotes is not None else None
            return (quote.bid, quote.ask) if quote is not None else (None, None)
        return sample.get("best_bid"), sample.get("best_ask")

    def _blocked(
        self,
        symbol: str,
        gate: dict[str, Any] | None,
        bid: float | None,
        ask: float | None,
        fees: tuple[float, float] | None,
    ) -> str | None:
        if self.risk.halted:
            return "halted"
        if self.position_mismatch is not None:
            return "position_mismatch"
        pauses = self.risk.paused(symbol)
        if pauses:
            return "paused:" + ",".join(sorted(pauses))
        if gate is None:
            return "gate_pending"
        if not gate.get("on", False):
            return "gate:" + ",".join(gate.get("reasons") or ["off"])
        if not self.venue_ready:
            return "venue_not_ready"
        if self.gateway is not None and self.gateway_reason is not None:
            return "gateway_not_ready"
        if fees is None:
            return "fees_unknown"
        if bid is None or ask is None:
            return "no_venue_quote"
        return None

    # --- actions -----------------------------------------------------------------------

    def execute(self, actions: list[Action], t_ns: int) -> dict[str, str]:
        """Carry out policy actions. Returns cycle_id -> reason for refused entries."""
        refused: dict[str, str] = {}
        for action in actions:
            if isinstance(action, Halt):
                if self.risk.execution_fault(action.reason):
                    self.alerts.send(
                        "bridge-halt", "OFI halted: execution fault", [action.reason], always=True
                    )
                self._check_halt(t_ns)
            elif isinstance(action, Cancel):
                order = self.orders.get(f"{action.cycle_id}|{action.leg}")
                if order is not None and not order.terminal:
                    order.cancel_requested = True
                    self.venue.cancel(order, t_ns)
            elif isinstance(action, Place | Close):
                reason = self._place(action, t_ns)
                if reason is not None and isinstance(action, Place) and action.leg == "entry":
                    refused[action.cycle_id] = reason
        return refused

    def _place(self, action: Place | Close, t_ns: int) -> str | None:
        market = isinstance(action, Close)
        reduce_only = True if market else action.reduce_only  # type: ignore[union-attr]
        leg = action.leg
        order = TrackedOrder(
            cycle_id=action.cycle_id,
            symbol=action.symbol,
            leg=leg,
            side=action.side,
            qty=str(action.qty),
            price=None if market else str(action.price),  # type: ignore[union-attr]
            post_only=False if market else action.post_only,  # type: ignore[union-attr]
            reduce_only=reduce_only,
            operation_id=str(
                operation_id_for(symbol=action.symbol, pair_id=action.cycle_id, side=leg)
            ),
            created_ns=t_ns,
        )
        self.orders[order.key] = order
        reference = float(order.price) if order.price else self._mid(action.symbol)
        notional = float(action.qty) * (reference or 0.0)
        if market and notional <= 0:
            notional = float(action.qty)  # unknown mid: still a positive reduce-only intent
        decision = self.risk.check_order(
            OrderIntent(action.symbol, action.side, notional, reduce_only=reduce_only)
        )
        if not decision.allowed:
            self.count(f"refused_{decision.reason.split(':')[0]}")
            log_event(
                "ofi_order_refused",
                level=logging.WARNING,
                symbol=action.symbol,
                leg=leg,
                reason=decision.reason,
            )
            if decision.needs_approval:
                self.alerts.send(
                    "approval",
                    "OFI order needs approval: refused",
                    [
                        f"{action.symbol} {action.side} {action.qty} (~${notional:,.0f})",
                        "Above OFI_MANUAL_APPROVAL_NOTIONAL_USD; a 1-30 s signal cannot wait.",
                    ],
                )
            order.state = "refused"
            self.update(order, Decimal(0), None, True, t_ns, decision.reason)
            return decision.reason
        self.count(f"orders_{leg.rstrip('0123456789')}")
        self.venue.submit(order, t_ns)
        return None

    def payload(self, order: TrackedOrder) -> dict[str, Any]:
        request = OrderRequest(
            operation_id=UUID(order.operation_id),
            occurred_at=datetime.now(UTC),
            source=SOURCE,
            instrument=order.symbol,
            execution_type="market" if order.market else "limit",
            direction=order.side,
            targets=[{"account": self.settings.execution_account, "volume_lots": order.qty}],
            entry_price=None if order.market else Decimal(order.price or "0"),
            post_only=True if order.post_only else None,
            reduce_only=True if order.reduce_only else None,
            note=order.key,
        )
        return request.model_dump(mode="json", exclude_none=True)

    def update(
        self,
        order: TrackedOrder,
        executed: Decimal,
        avg_price: float | None,
        terminal: bool,
        t_ns: int,
        reason: str | None = None,
    ) -> None:
        """Fold one order's cumulative state into the policy."""
        if order.terminal:
            return
        if executed > Decimal(order.executed) or avg_price is not None:
            order.executed = str(max(executed, Decimal(order.executed)))
            if avg_price is not None:
                order.avg_price = avg_price
        if reason:
            order.reason = reason
        if terminal:
            order.terminal = True
            if order.state not in TERMINAL and order.state != "refused":
                order.state = "rejected" if Decimal(order.executed) == 0 and reason else "done"
        policy = self.policies.get(order.symbol)
        if policy is None:
            return
        actions = policy.on_update(
            order.cycle_id, order.leg, Decimal(order.executed), order.avg_price, terminal, t_ns
        )
        self._sync_risk_position(order.symbol)
        self.execute(actions, t_ns)
        self._collect(order.symbol, t_ns)
        self.persist()

    # --- unknowns and halts --------------------------------------------------------------

    def note_unknown(self, detail: str) -> None:
        self.consecutive_unknown += 1
        self.count("unknown")
        log_event(
            "ofi_bridge_unknown",
            level=logging.ERROR,
            detail=detail,
            consecutive=self.consecutive_unknown,
        )
        if self.consecutive_unknown >= self.settings.max_consecutive_unknown:
            if self.risk.execution_fault(
                f"{self.consecutive_unknown} consecutive UNKNOWN outcomes"
            ):
                self.alerts.send(
                    "bridge-unknown",
                    "OFI halted: execution-service outcomes unknown",
                    [detail, "Reconciling, never resubmitting. Ack once the gateway is healthy."],
                    always=True,
                )
            self._check_halt(self.clock_ns())

    def note_ok(self) -> None:
        self.consecutive_unknown = 0

    def _check_halt(self, t_ns: int) -> None:
        if self.risk.halted and not self._halt_handled:
            self.on_halt(self.risk.halt.reason.value if self.risk.halt else "halt", t_ns)
        elif not self.risk.halted:
            self._halt_handled = False

    def on_halt(self, reason: str, t_ns: int | None = None) -> None:
        """Cancel everything, close every cycle reduce-only, flatten as a backstop."""
        if self._halt_handled:
            return
        self._halt_handled = True
        t_ns = t_ns or self.clock_ns()
        log_event("ofi_bridge_halt", level=logging.CRITICAL, reason=reason, mode=self.mode.value)
        if self.gateway is not None:
            self.gateway.control("cancel_all")
        if self.position_mismatch is not None:
            return  # the bridge's own view is wrong: cancel only, a human flattens
        for policy in self.policies.values():
            self.execute(policy.liquidate(reason, t_ns), t_ns)
        if self.gateway is not None:
            # After our own reduce-only closes had their chance; a no-op when flat.
            self.gateway.control("flatten", delay=1.0)
        self.persist()

    # --- records -------------------------------------------------------------------------

    def _mark(self, symbol: str, t_ns: int, sample: dict[str, Any]) -> None:
        """Mid move after each entry fill (adverse selection), and the funding in force."""
        mid = sample.get("mid")
        policy = self.policies.get(symbol)
        cycles = [c for c in self._pending if c.symbol == symbol]
        if policy is not None and policy.cycle is not None:
            cycles.append(policy.cycle)
        for cycle in cycles:
            if cycle.entry_fill_ns is None or cycle.entry_avg is None:
                continue
            if cycle.cycle_id not in self._funding and sample.get("secs_to_funding") is not None:
                self._funding[cycle.cycle_id] = {
                    "at_ns": t_ns + int(sample["secs_to_funding"] * NS),
                    "rate": sample.get("funding_rate"),
                }
            marks = self._marks.setdefault(cycle.cycle_id, {})
            if mid is None:
                continue
            sign = 1 if cycle.side == "buy" else -1
            for horizon in ADVERSE_HORIZONS_S:
                key = f"{horizon}s"
                if key not in marks and t_ns >= cycle.entry_fill_ns + horizon * NS:
                    marks[key] = round(sign * (mid - cycle.entry_avg) / cycle.entry_avg * 10_000, 3)
        self._write_due()

    def _collect(self, symbol: str, t_ns: int) -> None:
        policy = self.policies.get(symbol)
        if policy is None:
            return
        for cycle in policy.take_finished():
            self._pending.append(cycle)
        self._write_due()

    def _write_due(self, force: bool = False) -> None:
        keep: list[Cycle] = []
        for cycle in self._pending:
            marks = self._marks.get(cycle.cycle_id, {})
            complete = cycle.entry_fill_ns is None or len(marks) == len(ADVERSE_HORIZONS_S)
            if complete or force:
                self._write_trade(cycle, marks)
            else:
                keep.append(cycle)
        self._pending = keep

    def _write_trade(self, cycle: Cycle, marks: dict[str, float]) -> None:
        fees = self.fees(cycle.symbol) or (0.0, 0.0)
        legs = {o.leg: o for o in self.orders.values() if o.cycle_id == cycle.cycle_id}
        record = trade_record(
            cycle,
            mode=self.mode.value,
            model_version=self.model.version if self.model else None,
            maker_bp=fees[0],
            taker_bp=fees[1],
            liquidity={leg: ("taker" if o.market else "maker") for leg, o in legs.items()},
            funding=self._funding.pop(cycle.cycle_id, None),
            adverse_bp={f"{h}s": marks.get(f"{h}s") for h in ADVERSE_HORIZONS_S},
            rtt_ms={leg: o.rtt_ms for leg, o in legs.items() if o.rtt_ms is not None},
        )
        self._marks.pop(cycle.cycle_id, None)
        if record.get("net_usd") is not None:
            self.realized_usd += record["net_usd"]
        self.count("cycles_filled" if record["filled"] else "cycles_unfilled")
        self.book.trade(cycle.exit_ns or self.clock_ns(), record)
        log_event("ofi_trade", **{k: v for k, v in record.items() if k != "legs"})
        for key in [
            k for k, o in self.orders.items() if o.cycle_id == cycle.cycle_id and o.terminal
        ]:
            del self.orders[key]

    def flush_pending(self) -> None:
        self._write_due(force=True)

    # --- helpers -------------------------------------------------------------------------

    def _busy(self) -> bool:
        return any(p.phase is not Phase.FLAT for p in self.policies.values()) or any(
            not o.terminal for o in self.orders.values()
        )

    def _mid(self, symbol: str) -> float | None:
        if self.mode is ExecutionMode.TESTNET and self.quotes is not None:
            quote = self.quotes.get(symbol, QUOTE_MAX_AGE_MS)
            if quote is not None:
                return quote.mid
        sample = self.last_sample.get(symbol)
        return None if sample is None else sample.get("mid")

    def _unrealized(self) -> float:
        total = 0.0
        for symbol, policy in self.policies.items():
            cycle = policy.cycle
            mid = self._mid(symbol)
            if cycle is None or cycle.entry_avg is None or mid is None or cycle.remaining <= 0:
                continue
            sign = 1 if cycle.side == "buy" else -1
            total += sign * (mid - cycle.entry_avg) * float(cycle.remaining)
        return total

    def _sync_risk_position(self, symbol: str) -> None:
        policy = self.policies.get(symbol)
        cycle = policy.cycle if policy is not None else None
        if cycle is None or cycle.entry_avg is None:
            self.risk.set_position(symbol, 0.0)
            return
        sign = 1 if cycle.side == "buy" else -1
        self.risk.set_position(symbol, sign * float(cycle.remaining) * cycle.entry_avg)

    def count(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    # --- persistence (testnet) --------------------------------------------------------------

    def persist(self) -> None:
        if self.state_path is None or self.mode is not ExecutionMode.TESTNET:
            return
        state = {
            "schema": 1,
            "model_version": self.model.version if self.model else None,
            "policies": {s: p.snapshot() for s, p in self.policies.items()},
            "orders": {k: asdict(o) for k, o in self.orders.items()},
            "marks": self._marks,
            "funding": self._funding,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, default=str))
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log_event("ofi_bridge_persist_failed", level=logging.ERROR, error=str(exc)[:200])

    def restore(self) -> None:
        if self.state_path is None or self.mode is not ExecutionMode.TESTNET:
            return
        if not self.state_path.is_file():
            return
        state = json.loads(self.state_path.read_text())
        for symbol, raw in (state.get("policies") or {}).items():
            if symbol in self.policies:
                self.policies[symbol].restore(raw)
            elif raw.get("cycle"):
                self.position_mismatch = f"{symbol}: a cycle from a model that is no longer loaded"
        self.orders = {k: TrackedOrder(**raw) for k, raw in (state.get("orders") or {}).items()}
        self._marks = state.get("marks") or {}
        self._funding = state.get("funding") or {}
        if self.position_mismatch is not None:
            self.risk.execution_fault(self.position_mismatch)

    # --- reads -----------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": self.mode.value,
            "venue_ready": self.venue_ready,
            "gateway_not_ready": self.gateway_reason,
            "position_mismatch": self.position_mismatch,
            "consecutive_unknown": self.consecutive_unknown,
            "dead_man_armed": self.dead_man_armed,
            "policy": {s: p.snapshot() for s, p in self.policies.items()},
            "open_orders": [asdict(o) for o in self.orders.values() if not o.terminal],
            "realized_usd": round(self.realized_usd, 4),
            "counts": dict(self.counts),
        }
        if self.gateway is not None:
            out["controls"] = list(self.gateway.controls)[-10:]
        if self.quotes is not None:
            out["venue_quotes"] = self.quotes.status()
        return out
