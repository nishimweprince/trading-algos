"""The execution bridge: model -> policy -> risk -> simulated orders.

Nothing is ever sent to an exchange: this service trades in shadow only.

Per grid sample and symbol: score the sample, ask ``SymbolPolicy`` what to do,
run every proposed order past ``RiskState.check_order``, and hand what passes
to ``ShadowVenue``, which fills it against the live mainnet book the way the
exchange would have (queue position, latency; see its docstring).

The kill switch halts: every cycle is closed and nothing new is entered until
an operator ack. Loss limits are recorded as breaches but do not stop trading
(``RiskState.enforce_loss_limits``), so data collection never stops.

The core is synchronous so a replay drives it deterministically from recorded
event times; the live service drives it from the feed.
"""

from __future__ import annotations

import heapq
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ta_core.logging_config import log_event

from .alerts import Alerts
from .config import ExecutionMode
from .flow_guard import FlowGuard
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
    "ShadowVenue",
    "TrackedOrder",
]

NS = 1_000_000_000
TERMINAL = {"filled", "closed", "partially_filled_final", "cancelled", "rejected"}
# Unfilled endings that are not a refusal by the venue: no entry back-off after these.
# A post-only order that would take expires on Binance too; it is not rejected.
NOT_REFUSED = {"cancelled", "post_only_would_take", "not_submitted"}

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
    created_ns: int
    state: str = "new"
    executed: str = "0"
    avg_price: float | None = None
    terminal: bool = False
    reason: str | None = None
    sent_ns: int | None = None
    acked_ns: int | None = None
    cancel_requested: bool = False

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


# --- shadow ----------------------------------------------------------------------------


class ShadowVenue:
    """Would-have fills: the shadow mode's venue, and the backtest's.

    Two fill models for a resting maker order (``queue_model``):

    - ``pessimistic`` (default): only a trade printed *through* its price fills it.
    - ``queue``: risk-averse queue position. On arrival the order joins behind the
      quantity displayed at its level; trades at its price (by the other side)
      eat that queue first, and if the level shrinks below it the queue ahead
      shrinks with it (cancels are assumed to be ahead of us only as far as they
      must be). Trade volume beyond the queue fills us, partly if need be. A
      print through the price fills whatever is left.

    ``delay_ns`` is the time from decision to the order (or cancel) reaching the
    venue. Meanwhile the market moves on: the post-only check and a market
    order's price use the touch at arrival, and a cancel can lose to a fill.
    """

    def __init__(
        self, bridge: ExecutionBridge, *, queue_model: str = "pessimistic", delay_ns: int = 0
    ) -> None:
        if queue_model not in {"pessimistic", "queue"}:
            raise ValueError(f"unknown queue model {queue_model!r}")
        self.bridge = bridge
        self.queue_model = queue_model
        self.delay_ns = delay_ns
        self.touch: dict[str, tuple[float, float]] = {}
        self.books: dict[str, Any] = {}  # symbol -> live local book (bids/asks dicts)
        self.resting: dict[str, TrackedOrder] = {}
        self.queue_ahead: dict[str, Decimal] = {}  # exact: quantities come as decimal text
        self._pending: list[tuple[int, int, str, TrackedOrder]] = []
        self._seq = 0

    # --- time --------------------------------------------------------------------

    def advance(self, t_ns: int) -> None:
        """Deliver every order and cancel that has reached the venue by ``t_ns``."""
        while self._pending and self._pending[0][0] <= t_ns:
            at, _, kind, order = heapq.heappop(self._pending)
            if kind == "submit":
                self._arrive(order, at)
            else:
                self._cancel_now(order, at)

    def _later(self, kind: str, order: TrackedOrder, t_ns: int) -> None:
        self._seq += 1
        heapq.heappush(self._pending, (t_ns + self.delay_ns, self._seq, kind, order))

    # --- market state ---------------------------------------------------------------

    def _touch(self, symbol: str) -> tuple[float, float] | None:
        book = self.books.get(symbol)
        if book is not None:
            bids, asks = book.top(1)
            if bids and asks:
                return bids[0][0], asks[0][0]
        return self.touch.get(symbol)

    def _level(self, order: TrackedOrder) -> Decimal:
        book = self.books.get(order.symbol)
        if book is None or order.price is None:
            return Decimal(0)
        side = book.bids if order.side == "buy" else book.asks
        return Decimal(str(side.get(float(order.price), 0.0)))

    def on_book(self, symbol: str, book: Any) -> None:
        self.books[symbol] = book
        if self.queue_model != "queue":
            return
        for key, order in self.resting.items():
            if order.symbol == symbol and key in self.queue_ahead:
                self.queue_ahead[key] = min(self.queue_ahead[key], self._level(order))

    # --- orders -----------------------------------------------------------------------

    def submit(self, order: TrackedOrder, t_ns: int) -> None:
        order.state, order.sent_ns = "shadow", t_ns
        if self.delay_ns:
            self._later("submit", order, t_ns)
        else:
            self._arrive(order, t_ns)

    def cancel(self, order: TrackedOrder, t_ns: int) -> None:
        if self.delay_ns:
            self._later("cancel", order, t_ns)
        else:
            self._cancel_now(order, t_ns)

    def _arrive(self, order: TrackedOrder, t_ns: int) -> None:
        order.acked_ns = t_ns
        touch = self._touch(order.symbol)
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
        if self.queue_model == "queue":
            self.queue_ahead[order.key] = self._level(order)

    def _cancel_now(self, order: TrackedOrder, t_ns: int) -> None:
        if self.resting.pop(order.key, None) is not None:
            self.queue_ahead.pop(order.key, None)
            self.bridge.update(
                order, Decimal(order.executed), order.avg_price, True, t_ns, "cancelled"
            )

    def on_trade(
        self,
        symbol: str,
        price: float,
        t_ns: int,
        qty: float = 0.0,
        buyer_is_maker: bool | None = None,
    ) -> None:
        for key, order in list(self.resting.items()):
            if order.symbol != symbol or key not in self.resting:
                continue
            assert order.price is not None
            limit = float(order.price)
            through = price < limit if order.side == "buy" else price > limit
            if through:
                self._fill(order, Decimal(order.qty), limit, t_ns)
                continue
            if self.queue_model != "queue" or price != limit or qty <= 0:
                continue
            # At our price, only the other side's aggression reaches a resting order:
            # a resting buy is hit by sellers (the buyer was the maker).
            if buyer_is_maker is not None and buyer_is_maker != (order.side == "buy"):
                continue
            ahead = self.queue_ahead.get(key, Decimal(0)) - Decimal(str(qty))
            self.queue_ahead[key] = max(ahead, Decimal(0))
            if ahead < 0:
                remaining = Decimal(order.qty) - Decimal(order.executed)
                take = min(remaining, -ahead)
                self._fill(order, Decimal(order.executed) + take, limit, t_ns)

    def _fill(self, order: TrackedOrder, executed: Decimal, price: float, t_ns: int) -> None:
        done = executed >= Decimal(order.qty)
        if done:
            self.resting.pop(order.key, None)
            self.queue_ahead.pop(order.key, None)
        self.bridge.update(order, min(executed, Decimal(order.qty)), price, done, t_ns)


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
        state_dir: Path | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        fees: Callable[[str], tuple[float, float] | None] = lambda _symbol: None,
        book: TradeBook | None = None,
        queue_model: str = "pessimistic",
        delay_ns: int = 0,
    ) -> None:
        self.settings = settings
        self.mode: ExecutionMode = settings.execution_mode
        if self.mode is not ExecutionMode.SHADOW:
            raise ValueError(f"the bridge only trades in shadow, not {self.mode.value}")
        self.risk = risk
        self.alerts = alerts
        self.model = model
        self.flow_guard = FlowGuard(model.policy) if model is not None else None
        self.clock_ns = clock_ns
        self.fees = fees
        self.book = book if book is not None else TradeBook(state_dir)
        self.policies: dict[str, SymbolPolicy] = {}
        if filters is not None:
            self.configure(filters)
        self.orders: dict[str, TrackedOrder] = {}
        self.last_sample: dict[str, dict[str, Any]] = {}
        # symbol -> (rejected entries in a row, no new entry before this t_ns)
        self.reject_backoff: dict[str, tuple[int, int]] = {}
        self._halt_handled = False
        self._marks: dict[str, dict[str, float]] = {}
        self._funding: dict[str, dict[str, Any]] = {}
        self._pending: list[Cycle] = []
        self.realized_usd = 0.0
        self.counts: dict[str, int] = {}
        self.shadow = ShadowVenue(self, queue_model=queue_model, delay_ns=delay_ns)
        self.venue = self.shadow

    def configure(self, filters: dict[str, Filters]) -> None:
        """One policy per symbol, once the order filters are known. No model: none."""
        if self.model is None:
            return
        for symbol, symbol_filters in filters.items():
            self.policies[symbol] = SymbolPolicy(symbol, self.model.policy, symbol_filters)

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        """Nothing to connect: the shadow venue lives in this process."""

    async def close(self) -> None:
        self.flush_pending()
        self.book.close()

    # --- the hot path ------------------------------------------------------------------

    def on_sample(self, sample: dict[str, Any], gate: dict[str, Any] | None) -> None:
        symbol = sample["symbol"]
        t_ns = sample["t_ns"]
        self.last_sample[symbol] = sample
        self.advance(t_ns)
        bid, ask = self._quote(symbol, sample)
        if bid is not None and ask is not None:
            self.shadow.touch[symbol] = (bid, ask)
        self._check_halt(t_ns)
        self._mark(symbol, t_ns, sample)
        self.book.marks.on_sample(symbol, t_ns, sample.get("mid"))
        policy = self.policies.get(symbol)
        if policy is None or self.model is None:
            return
        scores = self.model.scorer.score(sample)
        against = self.flow_guard.against(sample) if self.flow_guard is not None else frozenset()
        fees = self.fees(symbol)
        blocked = self._blocked(symbol, gate, bid, ask, fees, entering=policy.phase is Phase.FLAT)
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
                flow_against=against,
            )
        )
        refused = self.execute(step.actions, t_ns)
        if step.signal is not None:
            record = {
                **asdict(step.signal),
                "at": datetime.fromtimestamp(t_ns / 1e9, UTC).isoformat(),
                "model_version": self.model.version,
                "ungated": self._ungated(),
                "mode": self.mode.value,
                "gate": gate,
            }
            if step.signal.cycle_id is not None and step.signal.cycle_id in refused:
                record["action"] = f"refused:{refused[step.signal.cycle_id]}"
            self.count(f"signal_{record['action'].split(':')[0]}")
            self.book.signal(t_ns, record)
            self.book.marks.on_signal(t_ns, record, sample.get("mid"))
        self._collect(symbol, t_ns)
        self.risk.update_equity(
            self.settings.shadow_equity_usd + self.realized_usd + self._unrealized()
        )
        self._check_halt(t_ns)

    def on_trade(
        self,
        symbol: str,
        t_ns: int,
        price: float,
        qty: float = 0.0,
        buyer_is_maker: bool | None = None,
    ) -> None:
        self.shadow.advance(t_ns)
        self.shadow.on_trade(symbol, price, t_ns, qty, buyer_is_maker)

    def on_book(self, symbol: str, book: Any) -> None:
        """Every applied depth update: the shadow venue's touch and queue."""
        self.shadow.on_book(symbol, book)

    def advance(self, t_ns: int) -> None:
        """Event time moved on: deliver delayed orders and cancels."""
        self.shadow.advance(t_ns)

    def _estimate_fill_price(self, order: TrackedOrder) -> float | None:
        """The order's limit, else the side of the current venue touch it would take."""
        if order.price is not None:
            return float(order.price)
        sample = self.last_sample.get(order.symbol)
        if sample is None:
            return None
        bid, ask = self._quote(order.symbol, sample)
        return ask if order.side == "buy" else bid

    def _quote(self, symbol: str, sample: dict[str, Any]) -> tuple[float | None, float | None]:
        return sample.get("best_bid"), sample.get("best_ask")

    def _blocked(
        self,
        symbol: str,
        gate: dict[str, Any] | None,
        bid: float | None,
        ask: float | None,
        fees: tuple[float, float] | None,
        *,
        entering: bool = True,
    ) -> str | None:
        """Why the symbol may not trade now. The policy cancels a resting entry when
        this is set, so checks that only concern a *new* entry (``entering``: the
        symbol is flat) must not fire for one already resting."""
        if self.risk.halted:
            return "halted"
        pauses = self.risk.paused(symbol)
        if pauses:
            return "paused:" + ",".join(sorted(pauses))
        if gate is None:
            return "gate_pending"
        if not gate.get("on", False):
            return "gate:" + ",".join(gate.get("reasons") or ["off"])
        if fees is None:
            return "fees_unknown"
        if bid is None or ask is None:
            return "no_venue_quote"
        if not entering:
            return None
        backoff = self.reject_backoff.get(symbol)
        if backoff is not None and self.clock_ns() < backoff[1]:
            return "reject_cooldown"
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
        if executed > 0 and avg_price is None and order.avg_price is None:
            # A fill reported without a price (Binance demo answers some market
            # orders with avgPrice 0): the quantity must still count, or the
            # position looks open and another close goes out.
            avg_price = self._estimate_fill_price(order)
            log_event(
                "ofi_fill_price_estimated",
                level=logging.WARNING,
                order=order.key,
                executed=str(executed),
                price=avg_price,
            )
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
            if order.leg == "entry":
                self._note_entry_outcome(order, t_ns)
        policy = self.policies.get(order.symbol)
        if policy is None:
            return
        actions = policy.on_update(
            order.cycle_id, order.leg, Decimal(order.executed), order.avg_price, terminal, t_ns
        )
        self._sync_risk_position(order.symbol)
        self.execute(actions, t_ns)
        self._collect(order.symbol, t_ns)

    def _note_entry_outcome(self, order: TrackedOrder, t_ns: int) -> None:
        """Back off after a rejected entry, so a persistent refusal (a filter, the
        risk limits) is not retried on every 100 ms grid step."""
        if order.state != "rejected" or order.reason in NOT_REFUSED:
            self.reject_backoff.pop(order.symbol, None)
            return
        streak = self.reject_backoff.get(order.symbol, (0, 0))[0] + 1
        base = getattr(self.settings, "reject_cooldown_ms", 1_000)
        ceiling = getattr(self.settings, "reject_cooldown_max_ms", 60_000)
        wait_ms = min(base * 2 ** min(streak - 1, 30), ceiling)
        self.reject_backoff[order.symbol] = (streak, t_ns + wait_ms * 1_000_000)
        self.count("entry_rejected")
        log_event(
            "ofi_entry_rejected",
            level=logging.WARNING if streak < 5 else logging.ERROR,
            symbol=order.symbol,
            cycle_id=order.cycle_id,
            reason=order.reason,
            in_a_row=streak,
            cooldown_ms=wait_ms,
            mode=self.mode.value,
        )
        if streak == 5:
            self.alerts.send(
                f"entry-rejected-{order.symbol}",
                f"OFI: {order.symbol} entries keep being rejected",
                [f"{streak} in a row, last: {order.reason}", f"next try in {wait_ms / 1000:.0f} s"],
            )

    # --- unknowns and halts --------------------------------------------------------------

    def _check_halt(self, t_ns: int) -> None:
        if self.risk.halted and not self._halt_handled:
            self.on_halt(self.risk.halt.reason.value if self.risk.halt else "halt", t_ns)
        elif not self.risk.halted:
            self._halt_handled = False

    def on_halt(self, reason: str, t_ns: int | None = None) -> None:
        """Close every cycle and enter nothing until the halt is acked."""
        if self._halt_handled:
            return
        self._halt_handled = True
        t_ns = t_ns or self.clock_ns()
        log_event("ofi_bridge_halt", level=logging.CRITICAL, reason=reason, mode=self.mode.value)
        self.count(f"halt_{reason}")
        detail = self.risk.halt.detail if self.risk.halt is not None else None
        self.book.halt(
            t_ns,
            {
                "at": datetime.fromtimestamp(t_ns / 1e9, UTC).isoformat(),
                "reason": reason,
                "detail": detail,
                "mode": self.mode.value,
            },
        )
        for policy in self.policies.values():
            self.execute(policy.liquidate(reason, t_ns), t_ns)

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
        record["ungated"] = self._ungated()
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

    def _mid(self, symbol: str) -> float | None:
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

    def _ungated(self) -> bool | None:
        """The loaded model has no research gates pass (research's stand-ins say nothing)."""
        return getattr(self.model, "ungated", None)

    def count(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    # --- reads -----------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": self.mode.value,
            "reject_backoff": {
                symbol: {"in_a_row": n, "until_ns": until}
                for symbol, (n, until) in self.reject_backoff.items()
            },
            "ungated": self._ungated(),
            "policy": {s: p.snapshot() for s, p in self.policies.items()},
            "open_orders": [asdict(o) for o in self.orders.values() if not o.terminal],
            "realized_usd": round(self.realized_usd, 4),
            "counts": dict(self.counts),
        }
        if self.flow_guard is not None and self.flow_guard.enabled:
            out["flow_guard"] = dict(self.flow_guard.flagged)
        out["shadow_fills"] = {
            "queue_model": self.shadow.queue_model,
            "delay_ms": round(self.shadow.delay_ns / 1e6, 3),
        }
        return out
