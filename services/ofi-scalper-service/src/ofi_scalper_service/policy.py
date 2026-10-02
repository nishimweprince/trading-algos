"""Entry, exit and sizing (plan §1.5) as a pure per-symbol state machine.

No I/O and no clock: every input carries its own event time, so the live
bridge, the shadow replay and the research backtest all drive this same code
and get the same decisions from the same inputs.

One cycle at a time per symbol::

    FLAT --signal--> ENTRY_RESTING --done, filled--> OPEN --TP filled--> FLAT
                       | timeout, decay, blocked:      | stop or time stop
                       | cancel; done, unfilled        v
                       +--> FLAT                     CLOSING --closed--> FLAT

- **Entry**: calibrated ``p`` of one side at or above ``threshold x gate
  multiplier``, and expected edge ``(p_side - p_opposite) x barrier`` at least
  the maker round trip plus the buffer. A post-only limit at the touch.
- **Take profit**: a reduce-only post-only limit at the barrier from the fill.
- **Stop**: watched here, on the mid; a reduce-only market close at the
  opposite barrier. **Time stop**: the same at ``horizon x k``.

The policy only proposes. The bridge runs every order past ``RiskState`` first.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any, Literal

__all__ = [
    "Cancel",
    "Close",
    "Context",
    "Cycle",
    "Filters",
    "Halt",
    "Phase",
    "Place",
    "PolicyParams",
    "Scores",
    "Signal",
    "Step",
    "SymbolPolicy",
    "kelly_fraction",
    "kelly_size",
]

Side = Literal["buy", "sell"]
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


class Phase(StrEnum):
    FLAT = "flat"
    ENTRY_RESTING = "entry_resting"
    OPEN = "open"
    CLOSING = "closing"


@dataclass(frozen=True, slots=True)
class PolicyParams:
    """Chosen by research and shipped inside the model's manifest, never in .env."""

    horizon_s: float
    threshold: float
    barrier_bp: float
    buffer_bp: float = 0.0
    entry_timeout_ms: int = 1000
    time_stop_mult: float = 2.0
    max_close_attempts: int = 3

    def __post_init__(self) -> None:
        if not 0 < self.threshold < 1:
            raise ValueError("policy.threshold must be in (0, 1)")
        if self.horizon_s <= 0 or self.barrier_bp <= 0 or self.entry_timeout_ms <= 0:
            raise ValueError("policy horizon_s, barrier_bp and entry_timeout_ms must be > 0")
        if self.buffer_bp < 0 or self.time_stop_mult <= 0 or self.max_close_attempts < 1:
            raise ValueError("policy buffer_bp >= 0, time_stop_mult > 0, max_close_attempts >= 1")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PolicyParams:
        known = {name for name in cls.__dataclass_fields__}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"unknown policy fields: {', '.join(unknown)}")
        return cls(**raw)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Scores:
    """Calibrated class probabilities for the model's horizon."""

    up: float
    down: float
    none: float


@dataclass(frozen=True, slots=True)
class Filters:
    """What an order must satisfy (from the plugin's ``OrderFilters``)."""

    tick: Decimal
    step: Decimal
    min_qty: Decimal
    min_notional: Decimal

    @classmethod
    def from_order_filters(cls, f: Any) -> Filters:
        return cls(
            tick=Decimal(f.tick_size),
            step=Decimal(f.lot_step),
            min_qty=Decimal(f.min_qty),
            min_notional=Decimal(f.min_notional),
        )


@dataclass(frozen=True, slots=True)
class Context:
    """One grid step's inputs for one symbol."""

    t_ns: int
    bid: float | None  # the venue touch orders are priced from
    ask: float | None
    scores: Scores | None
    notional_usd: float
    maker_bp: float
    taker_bp: float
    blocked: str | None = None  # gate off, paused, halted, venue not ready
    threshold_mult: float = 1.0
    size_mult: float = 1.0

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


# --- actions the bridge carries out ---------------------------------------------


@dataclass(frozen=True, slots=True)
class Place:
    symbol: str
    cycle_id: str
    leg: Literal["entry", "tp"]
    side: Side
    qty: Decimal
    price: Decimal
    reduce_only: bool
    post_only: bool = True


@dataclass(frozen=True, slots=True)
class Close:
    """Reduce-only market order."""

    symbol: str
    cycle_id: str
    attempt: int
    side: Side
    qty: Decimal
    reason: str

    @property
    def leg(self) -> str:
        return f"close{self.attempt}"


@dataclass(frozen=True, slots=True)
class Cancel:
    symbol: str
    cycle_id: str
    leg: str
    reason: str


@dataclass(frozen=True, slots=True)
class Halt:
    symbol: str
    reason: str


Action = Place | Close | Cancel | Halt


@dataclass(frozen=True, slots=True)
class Signal:
    """A sample whose probability crossed the threshold, and what was done."""

    symbol: str
    t_ns: int
    side: Side
    p: float
    p_opposite: float
    threshold: float
    edge_bp: float
    cost_bp: float
    action: str  # "enter" or "skip:<reason>"
    cycle_id: str | None = None


@dataclass(frozen=True, slots=True)
class Step:
    actions: list[Action] = field(default_factory=list)
    signal: Signal | None = None


# --- one cycle's state ---------------------------------------------------------------


@dataclass
class Cycle:
    cycle_id: str
    symbol: str
    side: Side  # entry side
    p: float
    threshold: float
    signal_ns: int
    entry_qty: Decimal
    entry_price: Decimal  # the limit
    # leg -> (cumulative executed qty, average price); legs: entry, tp, close<n>
    fills: dict[str, tuple[Decimal, float]] = field(default_factory=dict)
    entry_fill_ns: int | None = None
    entry_done: bool = False
    tp_price: Decimal | None = None
    tp_live: bool = False
    cancels: set[str] = field(default_factory=set)
    close_attempts: int = 0
    exit_reason: str | None = None
    liquidating: bool = False
    exit_ns: int | None = None

    @property
    def exit_side(self) -> Side:
        return "sell" if self.side == "buy" else "buy"

    @property
    def entry_filled(self) -> Decimal:
        return self.fills.get("entry", (Decimal(0), 0.0))[0]

    @property
    def entry_avg(self) -> float | None:
        qty, price = self.fills.get("entry", (Decimal(0), 0.0))
        return price if qty > 0 else None

    @property
    def exit_filled(self) -> Decimal:
        return sum((qty for leg, (qty, _) in self.fills.items() if leg != "entry"), Decimal(0))

    @property
    def exit_avg(self) -> float | None:
        legs = [(qty, price) for leg, (qty, price) in self.fills.items() if leg != "entry"]
        total = sum((qty for qty, _ in legs), Decimal(0))
        if total <= 0:
            return None
        return sum(float(qty) * price for qty, price in legs) / float(total)

    @property
    def remaining(self) -> Decimal:
        return self.entry_filled - self.exit_filled

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["entry_qty"] = str(self.entry_qty)
        out["entry_price"] = str(self.entry_price)
        out["tp_price"] = None if self.tp_price is None else str(self.tp_price)
        out["fills"] = {leg: [str(q), p] for leg, (q, p) in self.fills.items()}
        out["cancels"] = sorted(self.cancels)
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Cycle:
        values = dict(raw)
        values["entry_qty"] = Decimal(values["entry_qty"])
        values["entry_price"] = Decimal(values["entry_price"])
        if values.get("tp_price") is not None:
            values["tp_price"] = Decimal(values["tp_price"])
        values["fills"] = {leg: (Decimal(q), float(p)) for leg, (q, p) in values["fills"].items()}
        values["cancels"] = set(values.get("cancels") or ())
        return cls(**values)


# --- the machine ---------------------------------------------------------------------


def _floor(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(ROUND_FLOOR) * step


def _ceil(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(ROUND_CEILING) * step


class SymbolPolicy:
    def __init__(self, symbol: str, params: PolicyParams, filters: Filters) -> None:
        self.symbol = symbol
        self.params = params
        self.filters = filters
        self.phase = Phase.FLAT
        self.cycle: Cycle | None = None
        self.finished: list[Cycle] = []

    # --- samples -------------------------------------------------------------------

    def on_sample(self, ctx: Context) -> Step:
        if self.phase is Phase.FLAT:
            return self._maybe_enter(ctx)
        if self.phase is Phase.ENTRY_RESTING:
            return Step(self._watch_entry(ctx))
        if self.phase is Phase.OPEN:
            return Step(self._watch_open(ctx))
        return Step()

    def _maybe_enter(self, ctx: Context) -> Step:
        scores = ctx.scores
        if scores is None:
            return Step()
        side: Side = "buy" if scores.up >= scores.down else "sell"
        p, p_opp = (scores.up, scores.down) if side == "buy" else (scores.down, scores.up)
        threshold = self.params.threshold * ctx.threshold_mult
        if p < threshold:
            return Step()
        edge = (p - p_opp) * self.params.barrier_bp
        cost = 2 * ctx.maker_bp + self.params.buffer_bp

        def signal(action: str, cycle_id: str | None = None) -> Signal:
            return Signal(
                self.symbol, ctx.t_ns, side, p, p_opp, threshold, edge, cost, action, cycle_id
            )

        if ctx.blocked:
            return Step(signal=signal(f"skip:{ctx.blocked}"))
        if edge < cost:
            return Step(signal=signal("skip:edge_below_cost"))
        touch = ctx.bid if side == "buy" else ctx.ask
        if touch is None or touch <= 0:
            return Step(signal=signal("skip:no_quote"))
        price = Decimal(str(touch))
        price = (
            _floor(price, self.filters.tick) if side == "buy" else _ceil(price, self.filters.tick)
        )
        qty = _floor(Decimal(str(ctx.notional_usd * ctx.size_mult)) / price, self.filters.step)
        if qty <= 0 or qty < self.filters.min_qty or qty * price < self.filters.min_notional:
            return Step(signal=signal("skip:below_minimum"))
        cycle_id = f"{self.symbol}-{ctx.t_ns}"
        self.cycle = Cycle(
            cycle_id=cycle_id,
            symbol=self.symbol,
            side=side,
            p=p,
            threshold=threshold,
            signal_ns=ctx.t_ns,
            entry_qty=qty,
            entry_price=price,
        )
        self.phase = Phase.ENTRY_RESTING
        place = Place(self.symbol, cycle_id, "entry", side, qty, price, reduce_only=False)
        return Step([place], signal("enter", cycle_id))

    def _watch_entry(self, ctx: Context) -> list[Action]:
        cycle = self.cycle
        assert cycle is not None
        if "entry" in cycle.cancels:
            return []
        reason: str | None = None
        if ctx.blocked:
            reason = "blocked"
        elif ctx.t_ns - cycle.signal_ns >= self.params.entry_timeout_ms * NS_PER_MS:
            reason = "timeout"
        else:
            scores = ctx.scores
            p = None if scores is None else (scores.up if cycle.side == "buy" else scores.down)
            if p is None or p < self.params.threshold * ctx.threshold_mult:
                reason = "decay"
        if reason is None:
            return []
        cycle.cancels.add("entry")
        return [Cancel(self.symbol, cycle.cycle_id, "entry", reason)]

    def _watch_open(self, ctx: Context) -> list[Action]:
        cycle = self.cycle
        assert cycle is not None and cycle.entry_avg is not None
        mid = ctx.mid
        barrier = self.params.barrier_bp / 10_000
        if mid is not None:
            if cycle.side == "buy" and mid <= cycle.entry_avg * (1 - barrier):
                return self._close("stop", ctx.t_ns)
            if cycle.side == "sell" and mid >= cycle.entry_avg * (1 + barrier):
                return self._close("stop", ctx.t_ns)
        assert cycle.entry_fill_ns is not None
        limit_ns = self.params.horizon_s * self.params.time_stop_mult * NS_PER_S
        if ctx.t_ns - cycle.entry_fill_ns >= limit_ns:
            return self._close("time_stop", ctx.t_ns)
        return []

    def _close(self, reason: str, t_ns: int) -> list[Action]:
        cycle = self.cycle
        assert cycle is not None
        cycle.exit_reason = cycle.exit_reason or reason
        self.phase = Phase.CLOSING
        actions: list[Action] = []
        if cycle.tp_live and "tp" not in cycle.cancels:
            cycle.cancels.add("tp")
            actions.append(Cancel(self.symbol, cycle.cycle_id, "tp", reason))
        actions.append(self._close_order(reason))
        return actions

    def _close_order(self, reason: str) -> Close:
        cycle = self.cycle
        assert cycle is not None
        cycle.close_attempts += 1
        return Close(
            self.symbol,
            cycle.cycle_id,
            cycle.close_attempts,
            cycle.exit_side,
            cycle.remaining,
            reason,
        )

    # --- order updates ----------------------------------------------------------------

    def on_update(
        self,
        cycle_id: str,
        leg: str,
        executed: Decimal,
        avg_price: float | None,
        terminal: bool,
        t_ns: int,
    ) -> list[Action]:
        """Cumulative fill state of one order. ``terminal``: it will never change again."""
        cycle = self.cycle
        if cycle is None or cycle.cycle_id != cycle_id:
            return []
        if executed > 0 and avg_price is not None:
            cycle.fills[leg] = (executed, avg_price)
        if leg == "entry":
            if executed > 0 and cycle.entry_fill_ns is None:
                cycle.entry_fill_ns = t_ns
            if not terminal or cycle.entry_done:
                return []
            cycle.entry_done = True
            if cycle.entry_filled <= 0:
                cycle.exit_reason = cycle.exit_reason or "entry_unfilled"
                return self._finish(t_ns)
            if cycle.liquidating:
                self.phase = Phase.CLOSING
                return [self._close_order(cycle.exit_reason or "liquidate")]
            self.phase = Phase.OPEN
            return [self._take_profit()]
        if cycle.entry_done and cycle.remaining <= 0:
            if leg == "tp":
                cycle.exit_reason = cycle.exit_reason or "take_profit"
            if cycle.tp_live and leg != "tp" and "tp" not in cycle.cancels:
                # Closed by market while the TP still rests (it would fail as
                # reduce-only, but do not leave it behind).
                cycle.cancels.add("tp")
                return [
                    Cancel(self.symbol, cycle.cycle_id, "tp", "position_closed"),
                    *self._finish(t_ns),
                ]
            if leg == "tp" or not cycle.tp_live:
                return self._finish(t_ns)
            return []
        if not terminal:
            return []
        if leg == "tp":
            cycle.tp_live = False
            if cycle.remaining <= 0:
                return self._finish(t_ns)
            if self.phase is Phase.OPEN:
                # Refused or cancelled outside our control (e.g. post-only would take).
                return self._close("tp_lost", t_ns)
            return []
        # A close leg ended with a position left.
        if self.phase is Phase.CLOSING and cycle.remaining > 0:
            if cycle.close_attempts >= self.params.max_close_attempts:
                return [
                    Halt(self.symbol, f"{cycle.cycle_id}: {cycle.close_attempts} closes failed")
                ]
            return [self._close_order(cycle.exit_reason or "close_retry")]
        return []

    def _take_profit(self) -> Place:
        cycle = self.cycle
        assert cycle is not None and cycle.entry_avg is not None
        barrier = Decimal(str(self.params.barrier_bp)) / Decimal(10_000)
        avg = Decimal(str(cycle.entry_avg))
        if cycle.side == "buy":
            price = _ceil(avg * (1 + barrier), self.filters.tick)
        else:
            price = _floor(avg * (1 - barrier), self.filters.tick)
        cycle.tp_price = price
        cycle.tp_live = True
        return Place(
            self.symbol,
            cycle.cycle_id,
            "tp",
            cycle.exit_side,
            cycle.remaining,
            price,
            reduce_only=True,
        )

    def _finish(self, t_ns: int) -> list[Action]:
        cycle = self.cycle
        assert cycle is not None
        cycle.exit_ns = t_ns
        self.finished.append(cycle)
        self.cycle = None
        self.phase = Phase.FLAT
        return []

    # --- operator / risk --------------------------------------------------------------

    def liquidate(self, reason: str, t_ns: int) -> list[Action]:
        """Halt or kill: cancel what rests, close what is open."""
        cycle = self.cycle
        if cycle is None:
            return []
        cycle.liquidating = True
        cycle.exit_reason = cycle.exit_reason or reason
        if self.phase is Phase.ENTRY_RESTING:
            if "entry" in cycle.cancels:
                return []
            cycle.cancels.add("entry")
            return [Cancel(self.symbol, cycle.cycle_id, "entry", reason)]
        if self.phase is Phase.OPEN:
            return self._close(reason, t_ns)
        return []

    def take_finished(self) -> list[Cycle]:
        done, self.finished = self.finished, []
        return done

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "cycle": None if self.cycle is None else self.cycle.to_dict(),
        }

    def restore(self, raw: dict[str, Any]) -> None:
        self.phase = Phase(raw.get("phase", "flat"))
        cycle = raw.get("cycle")
        self.cycle = None if cycle is None else Cycle.from_dict(cycle)
        if self.cycle is None:
            self.phase = Phase.FLAT


# --- sizing (disabled until calibration on our own fills passes, plan §1.5) -----------


def kelly_fraction(p: float, b: float) -> float:
    """``f* = p - (1 - p) / b`` with ``b`` = net average win / net average loss."""
    if b <= 0:
        return 0.0
    return p - (1 - p) / b


def kelly_size(p: float, b: float, cap: float, cutoff: float = 0.0) -> float:
    """Fraction of the risk budget: ``min(0.25 f*, cap)``, zero at or below the cutoff."""
    f = kelly_fraction(p, b)
    if f <= cutoff:
        return 0.0
    return min(0.25 * f, cap)
