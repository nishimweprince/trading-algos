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
    CloseFailed,
    Context,
    Cycle,
    Filters,
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


class QueuePos:
    """Where a resting order stands in its level's queue (``queue`` model).

    ``ahead`` is the displayed quantity in front of us, ``level`` the whole
    displayed level at our price when last seen, ``book_txn`` the engine time
    (ms) of that depth state. Trades the depth has not shown yet are kept in
    ``unmatched``; cancels inferred from a shrinking level in ``cancels`` as
    ``[txn_ms, quantity not yet matched to a late trade, front share]``.
    """

    __slots__ = ("ahead", "book_txn", "cancels", "level", "unmatched")

    def __init__(self, level: Decimal, book_txn: int | None) -> None:
        self.ahead = level  # joins at the back
        self.level = level
        self.book_txn = book_txn
        self.unmatched: list[tuple[int | None, Decimal]] = []
        self.cancels: list[list[Any]] = []


CANCEL_MEMORY_MS = 1_000


class ShadowVenue:
    """Would-have fills: the shadow mode's venue, and the backtest's.

    Two fill models for a resting maker order (``queue_model``):

    - ``queue`` (default): proportional queue position, close to hftbacktest's
      ``ProbQueueModel``. On arrival the order joins behind the quantity displayed
      at its level. Trades at its price (by the other side) eat the queue in
      front first; volume beyond it fills us, partly if need be. When the level
      shrinks by more than the trades explain, the rest are cancels, split
      between the front and the back of the queue in proportion to their sizes
      (the back absorbs at most what it holds). Size that joins goes behind us.
      Exchange times (depth ``txn_ms``, trade ``trade_ms``) keep a trade from
      counting twice whether its depth update comes before or after it.
    - ``pessimistic``: only a trade printed *through* its price fills it (a
      lower bound; research's selection uses it).

    In both, a print through the price fills whatever is left.

    ``delay_ns`` is the time from decision to the order (or cancel) reaching the
    venue, in local time (``latency.ShadowLatency``). Meanwhile the market moves
    on: the post-only check and a market order's fill use the book at arrival,
    and a cancel can lose to a fill. A market order walks the book's levels
    (VWAP); beyond the displayed depth the rest fills at the last level
    (``walk_exhausted``). What is decided at arrival (a post-only rejection, a
    market fill) reaches the policy at ``ack_delay_ns`` after sending, as the
    exchange's answer would.
    """

    WALK_LEVELS = 20

    def __init__(
        self,
        bridge: ExecutionBridge,
        *,
        queue_model: str = "queue",
        delay_ns: int = 0,
        ack_delay_ns: int | None = None,
    ) -> None:
        if queue_model not in {"pessimistic", "queue"}:
            raise ValueError(f"unknown queue model {queue_model!r}")
        self.bridge = bridge
        self.queue_model = queue_model
        self.delay_ns = delay_ns
        self.ack_delay_ns = delay_ns if ack_delay_ns is None else max(ack_delay_ns, delay_ns)
        self.walk_exhausted = 0
        self.touch: dict[str, tuple[float, float]] = {}
        self.books: dict[str, Any] = {}  # symbol -> live local book (bids/asks dicts)
        self.book_txn: dict[str, int] = {}  # symbol -> engine time of the last depth (ms)
        self.resting: dict[str, TrackedOrder] = {}
        # Market orders that arrived with no touch: they fill at the next one.
        self.waiting: dict[str, TrackedOrder] = {}
        self.queue: dict[str, QueuePos] = {}  # exact: quantities come as decimal text
        self._pending: list[tuple[int, int, str, TrackedOrder, Any]] = []
        self._seq = 0

    # --- time --------------------------------------------------------------------

    def advance(self, t_ns: int) -> None:
        """Deliver every order and cancel that has reached the venue by ``t_ns``."""
        while self._pending and self._pending[0][0] <= t_ns:
            at, _, kind, order, payload = heapq.heappop(self._pending)
            if kind == "submit":
                self._arrive(order, at)
            elif kind == "cancel":
                self._cancel_now(order, at)
            else:  # notify: the answer to an order decided at arrival
                executed, price, reason = payload
                self.bridge.update(order, executed, price, True, at, reason)

    def _later(self, kind: str, order: TrackedOrder, at_ns: int, payload: Any = None) -> None:
        self._seq += 1
        heapq.heappush(self._pending, (at_ns, self._seq, kind, order, payload))

    def _answer(
        self,
        order: TrackedOrder,
        executed: Decimal,
        price: float | None,
        t_ns: int,
        reason: str | None = None,
    ) -> None:
        """A final answer decided at arrival, delivered at the order's ack time."""
        assert order.acked_ns is not None
        if order.acked_ns <= t_ns:
            self.bridge.update(order, executed, price, True, t_ns, reason)
        else:
            self._later("notify", order, order.acked_ns, (executed, price, reason))

    # --- market state ---------------------------------------------------------------

    def set_touch(self, symbol: str, bid: float, ask: float, t_ns: int) -> None:
        """The grid sample's touch; a market order waiting for one fills now."""
        self.touch[symbol] = (bid, ask)
        for key, order in list(self.waiting.items()):
            if order.symbol == symbol:
                del self.waiting[key]
                self._arrive(order, t_ns)

    def _touch(self, symbol: str) -> tuple[float, float] | None:
        book = self.books.get(symbol)
        if book is not None:
            bids, asks = book.top(1)
            if bids and asks:
                return bids[0][0], asks[0][0]
        return self.touch.get(symbol)

    def _level(self, order: TrackedOrder) -> Decimal | None:
        """Displayed quantity at the order's price; None if the book does not reach it
        (a partial top-N snapshot says nothing about deeper levels)."""
        book = self.books.get(order.symbol)
        if book is None or order.price is None:
            return Decimal(0)
        side = book.bids if order.side == "buy" else book.asks
        price = float(order.price)
        qty = side.get(price)
        if qty is not None:
            return Decimal(str(qty))
        if side and (price < min(side) if order.side == "buy" else price > max(side)):
            return None
        return Decimal(0)

    def on_book(self, symbol: str, book: Any, txn_ms: int | None = None) -> None:
        self.books[symbol] = book
        if txn_ms is not None:
            self.book_txn[symbol] = txn_ms
        if self.queue_model != "queue":
            return
        for key, order in self.resting.items():
            pos = self.queue.get(key)
            if order.symbol == symbol and pos is not None:
                self._on_level(pos, self._level(order), txn_ms)

    @staticmethod
    def _on_level(pos: QueuePos, level: Decimal | None, txn_ms: int | None) -> None:
        if level is None:
            return  # beyond a partial snapshot: nothing known
        if txn_ms is None:  # no exchange time: the book is taken as current
            explained = sum((q for _, q in pos.unmatched), Decimal(0))
            pos.unmatched = []
        else:
            explained = sum((q for t, q in pos.unmatched if t is None or t <= txn_ms), Decimal(0))
            pos.unmatched = [(t, q) for t, q in pos.unmatched if t is not None and t > txn_ms]
        change = (pos.level - level) - explained
        if change > 0:  # cancels: split between the front and the back
            total = pos.level - explained
            back = max(total - pos.ahead, Decimal(0))
            share = pos.ahead / total if total > 0 else Decimal(0)
            front = share * change
            front += max(change - front - back, Decimal(0))  # the back cannot absorb it
            pos.ahead -= front
            if txn_ms is not None:
                pos.cancels.append([txn_ms, change, share])
                pos.cancels = [c for c in pos.cancels if c[0] >= txn_ms - CANCEL_MEMORY_MS]
        pos.ahead = min(max(pos.ahead, Decimal(0)), level)  # size that joined is behind us
        pos.level = level
        if txn_ms is not None:
            pos.book_txn = txn_ms

    # --- orders -----------------------------------------------------------------------

    def submit(self, order: TrackedOrder, t_ns: int) -> None:
        order.state, order.sent_ns = "shadow", t_ns
        if self.delay_ns:
            self._later("submit", order, t_ns + self.delay_ns)
        else:
            self._arrive(order, t_ns)

    def cancel(self, order: TrackedOrder, t_ns: int) -> None:
        if self.delay_ns:
            self._later("cancel", order, t_ns + self.delay_ns)
        else:
            self._cancel_now(order, t_ns)

    def _arrive(self, order: TrackedOrder, t_ns: int) -> None:
        sent = order.sent_ns if order.sent_ns is not None else t_ns
        order.acked_ns = max(sent + self.ack_delay_ns, t_ns)
        touch = self._touch(order.symbol)
        if order.market:
            if touch is None:
                self.waiting[order.key] = order
                return
            self._answer(order, Decimal(order.qty), self._walk(order, touch), t_ns)
            return
        assert order.price is not None
        price = float(order.price)
        if touch is not None and order.post_only:
            crosses = price >= touch[1] if order.side == "buy" else price <= touch[0]
            if crosses:
                self._answer(order, Decimal(0), None, t_ns, "post_only_would_take")
                return
        self.resting[order.key] = order
        if self.queue_model == "queue":
            self.queue[order.key] = QueuePos(
                self._level(order) or Decimal(0), self.book_txn.get(order.symbol)
            )

    def _walk(self, order: TrackedOrder, touch: tuple[float, float]) -> float:
        """A market order's average price through the book's displayed levels."""
        book = self.books.get(order.symbol)
        best = touch[1] if order.side == "buy" else touch[0]
        if book is None:
            return best
        bids, asks = book.top(self.WALK_LEVELS)
        levels = asks if order.side == "buy" else bids
        if not levels:
            return best
        left = Decimal(order.qty)
        cost = Decimal(0)
        for price, qty in levels:
            take = min(left, Decimal(str(qty)))
            cost += take * Decimal(str(price))
            left -= take
            if left <= 0:
                break
        if left > 0:
            self.walk_exhausted += 1
            cost += left * Decimal(str(levels[-1][0]))
        return float(cost / Decimal(order.qty))

    def _cancel_now(self, order: TrackedOrder, t_ns: int) -> None:
        if self.resting.pop(order.key, None) is not None:
            self.queue.pop(order.key, None)
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
        trade_ms: int | None = None,
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
            pos = self.queue.get(key)
            if pos is None:
                continue
            ahead = self._on_trade(pos, Decimal(str(qty)), trade_ms)
            if ahead < 0:
                remaining = Decimal(order.qty) - Decimal(order.executed)
                take = min(remaining, -ahead)
                self._fill(order, Decimal(order.executed) + take, limit, t_ns)

    @staticmethod
    def _on_trade(pos: QueuePos, qty: Decimal, trade_ms: int | None) -> Decimal:
        """Take a trade off the front; returns what is left ahead (negative: it reached us)."""
        cut = qty
        if trade_ms is not None and pos.book_txn is not None and trade_ms <= pos.book_txn:
            # The depth already showed this trade, as cancels split front/back.
            # It came off the front: move the back's part of those cancels forward.
            cut = Decimal(0)
            rest = qty
            for cancel in pos.cancels:
                if rest <= 0:
                    break
                if cancel[0] < trade_ms or cancel[1] <= 0:
                    continue
                matched = min(rest, cancel[1])
                cancel[1] -= matched
                rest -= matched
                cut += matched * (1 - cancel[2])
            cut += rest  # not seen as a cancel: take it now
        else:
            pos.unmatched.append((trade_ms, qty))
        ahead = pos.ahead - cut
        pos.ahead = max(ahead, Decimal(0))
        return ahead

    def _fill(self, order: TrackedOrder, executed: Decimal, price: float, t_ns: int) -> None:
        done = executed >= Decimal(order.qty)
        if done:
            self.resting.pop(order.key, None)
            self.queue.pop(order.key, None)
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
        queue_model: str = "queue",
        delay_ns: int = 0,
        ack_delay_ns: int | None = None,
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
        # A halt restored from disk was handled (recorded, closed) before the restart.
        self._halt_handled = risk.restored
        self._marks: dict[str, dict[str, float]] = {}
        self._funding: dict[str, dict[str, Any]] = {}
        self._pending: list[Cycle] = []
        self.realized_usd = 0.0
        self.counts: dict[str, int] = {}
        self.shadow = ShadowVenue(
            self, queue_model=queue_model, delay_ns=delay_ns, ack_delay_ns=ack_delay_ns
        )
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
            self.shadow.set_touch(symbol, bid, ask, t_ns)
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
        for breach in self.risk.take_breaches():
            self._record_breach(breach, t_ns)
        self._check_halt(t_ns)

    def on_trade(
        self,
        symbol: str,
        t_ns: int,
        price: float,
        qty: float = 0.0,
        buyer_is_maker: bool | None = None,
        trade_ms: int | None = None,
    ) -> None:
        self.shadow.advance(t_ns)
        self.shadow.on_trade(symbol, price, t_ns, qty, buyer_is_maker, trade_ms)

    def on_book(self, symbol: str, book: Any, txn_ms: int | None = None) -> None:
        """Every applied depth update: the shadow venue's touch and queue."""
        self.shadow.on_book(symbol, book, txn_ms)

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
            if isinstance(action, CloseFailed):
                self._force_close(action, t_ns)
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

    # --- halts, breaches and faults ------------------------------------------------------

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
                "kind": "halt",
                "reason": reason,
                "detail": detail,
                "mode": self.mode.value,
            },
        )
        for policy in self.policies.values():
            self.execute(policy.liquidate(reason, t_ns), t_ns)

    def _record_breach(self, breach: dict[str, Any], t_ns: int) -> None:
        """A loss limit was crossed in record-only mode: write it down, keep trading."""
        reason = breach["reason"]
        self.count(f"breach_{reason}")
        log_event(
            "ofi_loss_limit_breach",
            level=logging.ERROR,
            reason=reason,
            detail=breach.get("detail"),
            mode=self.mode.value,
        )
        self.book.halt(
            t_ns,
            {
                "at": datetime.fromtimestamp(t_ns / 1e9, UTC).isoformat(),
                "kind": "breach",
                "reason": reason,
                "detail": breach.get("detail"),
                "enforced": False,
                "mode": self.mode.value,
            },
        )
        self.alerts.send(
            f"breach-{reason}",
            f"OFI loss limit crossed: {reason}",
            [str(breach.get("detail") or ""), "recorded, still trading (shadow)"],
            always=True,
        )

    def _force_close(self, action: CloseFailed, t_ns: int) -> None:
        """Every close failed: record the fault, then close the rest at the last
        sample's touch (else the entry price) so the cycle ends and trading goes on."""
        policy = self.policies.get(action.symbol)
        cycle = policy.cycle if policy is not None else None
        if cycle is None or cycle.cycle_id != action.cycle_id or cycle.remaining <= 0:
            return
        fault = self.risk.record_fault(action.reason)
        self.count("close_failed")
        bid, ask = self._quote(action.symbol, self.last_sample.get(action.symbol) or {})
        price = ask if cycle.exit_side == "buy" else bid
        if price is None:
            price = cycle.entry_avg
        log_event(
            "ofi_close_failed",
            level=logging.ERROR,
            symbol=action.symbol,
            cycle_id=cycle.cycle_id,
            detail=action.reason,
            forced_price=price,
            mode=self.mode.value,
        )
        self.book.halt(
            t_ns,
            {
                "at": datetime.fromtimestamp(t_ns / 1e9, UTC).isoformat(),
                "kind": "close_failed",
                "reason": fault["reason"],
                "detail": action.reason,
                "enforced": False,
                "forced_price": price,
                "mode": self.mode.value,
            },
        )
        self.alerts.send(
            f"close-failed-{action.symbol}",
            f"OFI: {action.symbol} closes failed, closed at the touch",
            [action.reason, f"forced at {price}", "recorded, still trading (shadow)"],
            always=True,
        )
        cycle.exit_reason = "forced_close"
        order = TrackedOrder(
            cycle_id=cycle.cycle_id,
            symbol=cycle.symbol,
            leg="forced",
            side=cycle.exit_side,
            qty=str(cycle.remaining),
            price=None,
            post_only=False,
            reduce_only=True,
            created_ns=t_ns,
            state="filled",
        )
        self.orders[order.key] = order
        self.update(order, cycle.remaining, price, True, t_ns, "forced_close")

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
            "arrive_ms": round(self.shadow.delay_ns / 1e6, 3),
            "ack_ms": round(self.shadow.ack_delay_ns / 1e6, 3),
            "walk_exhausted": self.shadow.walk_exhausted,
        }
        return out
