"""A local USDⓈ-M order book, kept in step with ``<symbol>@depth@0ms``.

``DepthSync`` is the procedure from Binance's "How to manage a local order
book correctly", as a synchronous state machine with no I/O so it can be
driven identically by the live stream and by a replay of recorded files:

1. Buffer diff events while no book exists (``SYNCING``).
2. Apply a REST snapshot (``/fapi/v1/depth``) carrying ``lastUpdateId``.
3. Drop buffered events with ``u < lastUpdateId``.
4. The first applied event must have ``U <= lastUpdateId <= u``.
5. Every later event must have ``pu`` equal to the previous event's ``u``.

Any break in 4 or 5, or a book left crossed after an event, discards the book
and returns to ``SYNCING`` with ``needs_snapshot`` set.

``SnapshotSync`` (end of module) is the stateless alternative for
``BINANCE_FUTURES_BOOK_MODE=partial``: complete top-N books, nothing to chain. Quantities are
absolute; zero removes the level, and removing a level that is not present is
normal.

The state machine never fetches anything. Its owner reads ``needs_snapshot``,
fetches, and calls ``apply_snapshot``; events that arrive meanwhile are
buffered.
"""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "BUFFER_LIMIT",
    "DepthDiff",
    "DepthSync",
    "LocalOrderBook",
    "SnapshotSync",
    "SyncOutcome",
    "SyncState",
]

# A resync that cannot complete before this many diffs arrive starts over: a
# snapshot that slow is already stale.
BUFFER_LIMIT = 20_000

Level = tuple[float, float]


@dataclass(frozen=True, slots=True)
class DepthDiff:
    """The fields of a ``depthUpdate`` event the book needs."""

    first_id: int  # U
    final_id: int  # u
    prev_final_id: int  # pu
    bids: Sequence[Level]
    asks: Sequence[Level]


class SyncState(StrEnum):
    SYNCING = "syncing"  # no book; buffering, waiting for a snapshot
    AWAITING_FIRST = "awaiting_first"  # snapshot applied, no event bridged it yet
    LIVE = "live"


class SyncOutcome(StrEnum):
    BUFFERED = "buffered"
    DROPPED = "dropped"  # older than the snapshot
    APPLIED = "applied"
    GAP = "gap"  # continuity broke; book discarded, snapshot needed
    CROSSED = "crossed"  # book crossed after applying; discarded, snapshot needed


class LocalOrderBook:
    """Price -> quantity per side, with the touch cached.

    Prices are the floats Binance's decimal strings parse to; the same string
    always parses to the same float, so they are safe as keys.
    """

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self._best_bid: float | None = None
        self._best_ask: float | None = None

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self._best_bid = self._best_ask = None

    def load(self, bids: Iterable[Level], asks: Iterable[Level]) -> None:
        self.clear()
        self.bids.update((price, qty) for price, qty in bids if qty > 0)
        self.asks.update((price, qty) for price, qty in asks if qty > 0)
        self._best_bid = max(self.bids) if self.bids else None
        self._best_ask = min(self.asks) if self.asks else None

    def apply(self, bids: Iterable[Level], asks: Iterable[Level]) -> None:
        recompute_bid = recompute_ask = False
        for price, qty in bids:
            if qty > 0:
                self.bids[price] = qty
                if self._best_bid is None or price > self._best_bid:
                    self._best_bid = price
            elif self.bids.pop(price, None) is not None and price == self._best_bid:
                recompute_bid = True
        for price, qty in asks:
            if qty > 0:
                self.asks[price] = qty
                if self._best_ask is None or price < self._best_ask:
                    self._best_ask = price
            elif self.asks.pop(price, None) is not None and price == self._best_ask:
                recompute_ask = True
        if recompute_bid:
            self._best_bid = max(self.bids) if self.bids else None
        if recompute_ask:
            self._best_ask = min(self.asks) if self.asks else None

    @property
    def best_bid(self) -> Level | None:
        price = self._best_bid
        return None if price is None else (price, self.bids[price])

    @property
    def best_ask(self) -> Level | None:
        price = self._best_ask
        return None if price is None else (price, self.asks[price])

    @property
    def crossed(self) -> bool:
        return (
            self._best_bid is not None
            and self._best_ask is not None
            and self._best_bid >= self._best_ask
        )

    def mid(self) -> float | None:
        if self._best_bid is None or self._best_ask is None:
            return None
        return (self._best_bid + self._best_ask) / 2

    def top(self, levels: int) -> tuple[list[Level], list[Level]]:
        """The best ``levels`` bids (descending) and asks (ascending)."""
        bid_prices = heapq.nlargest(levels, self.bids)
        ask_prices = heapq.nsmallest(levels, self.asks)
        return (
            [(price, self.bids[price]) for price in bid_prices],
            [(price, self.asks[price]) for price in ask_prices],
        )

    def notional_within(self, bp: float) -> tuple[float, float]:
        """Quote notional resting within ``bp`` basis points of mid, per side."""
        mid = self.mid()
        if mid is None:
            return 0.0, 0.0
        floor = mid * (1 - bp / 10_000)
        ceiling = mid * (1 + bp / 10_000)
        bid = sum(price * qty for price, qty in self.bids.items() if price >= floor)
        ask = sum(price * qty for price, qty in self.asks.items() if price <= ceiling)
        return bid, ask


class DepthSync:
    def __init__(self, symbol: str, *, buffer_limit: int = BUFFER_LIMIT) -> None:
        self.symbol = symbol
        self.book = LocalOrderBook()
        self.state = SyncState.SYNCING
        self.needs_snapshot = True
        self.last_final_id: int | None = None
        self.snapshot_id: int | None = None
        self.gaps = 0
        self.resyncs = 0
        self._buffer: deque[DepthDiff] = deque()
        self._buffer_limit = buffer_limit

    @property
    def verified(self) -> bool:
        return self.state is SyncState.LIVE

    def on_diff(self, diff: DepthDiff) -> SyncOutcome:
        if self.state is SyncState.SYNCING:
            self._buffer.append(diff)
            if len(self._buffer) > self._buffer_limit:
                # The snapshot never came (or came too late); start over.
                self._buffer.clear()
                self._buffer.append(diff)
                self.needs_snapshot = True
            return SyncOutcome.BUFFERED
        if self.state is SyncState.AWAITING_FIRST:
            assert self.snapshot_id is not None
            if diff.final_id < self.snapshot_id:
                return SyncOutcome.DROPPED
            if diff.first_id <= self.snapshot_id <= diff.final_id:
                return self._apply(diff)
            return self._break(diff, SyncOutcome.GAP)
        if diff.prev_final_id != self.last_final_id:
            return self._break(diff, SyncOutcome.GAP)
        return self._apply(diff)

    def apply_snapshot(
        self, last_update_id: int, bids: Iterable[Level], asks: Iterable[Level]
    ) -> SyncOutcome | None:
        """Load a snapshot, then replay whatever the buffer holds after it.

        Returns the outcome of the last replayed diff, or ``None`` when the
        buffer had nothing newer than the snapshot (the book then waits for
        the first live diff to bridge it).
        """
        self.book.load(bids, asks)
        self.snapshot_id = last_update_id
        self.last_final_id = None
        self.needs_snapshot = False
        self.state = SyncState.AWAITING_FIRST
        pending = list(self._buffer)
        self._buffer.clear()
        outcome: SyncOutcome | None = None
        for index, diff in enumerate(pending):
            result = self.on_diff(diff)
            if result is SyncOutcome.DROPPED:
                continue
            outcome = result
            if result in {SyncOutcome.GAP, SyncOutcome.CROSSED}:
                # _break re-buffered the offending diff; keep everything after it.
                self._buffer.extend(pending[index + 1 :])
                break
        return outcome

    def _apply(self, diff: DepthDiff) -> SyncOutcome:
        self.book.apply(diff.bids, diff.asks)
        self.last_final_id = diff.final_id
        if self.book.crossed:
            return self._break(diff, SyncOutcome.CROSSED, rebuffer=False)
        self.state = SyncState.LIVE
        return SyncOutcome.APPLIED

    def reset(self) -> None:
        """Discard the book and everything buffered; the next diff starts a resync."""
        self.resyncs += 1
        self.book.clear()
        self.state = SyncState.SYNCING
        self.needs_snapshot = True
        self.last_final_id = None
        self.snapshot_id = None
        self._buffer.clear()

    def _break(
        self, diff: DepthDiff, outcome: SyncOutcome, *, rebuffer: bool = True
    ) -> SyncOutcome:
        self.gaps += 1
        self.reset()
        if rebuffer:
            self._buffer.append(diff)
        return outcome


class SnapshotSync:
    """A book fed by partial-depth snapshots (``<s>@depth<N>@<speed>``).

    Each frame is a complete top-N book, so there is no chain to break and no
    REST snapshot to fetch: the book is ``LIVE`` from the first valid frame.
    Frames still carry ``U``/``u``/``pu``; a ``pu`` that does not match the
    previous ``u`` means snapshots were skipped (counted in ``skipped``), which
    costs freshness, never correctness. A crossed snapshot is refused and the
    book drops to ``SYNCING`` until the next valid one.

    Same surface as :class:`DepthSync` where consumers touch it: ``book``,
    ``state``, ``verified``, ``gaps``, ``resyncs``, ``last_final_id``,
    ``reset``. Depth beyond the top N levels is unknown, so
    ``book.notional_within`` sums only what the snapshot carries.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.book = LocalOrderBook()
        self.state = SyncState.SYNCING
        self.needs_snapshot = False
        self.last_final_id: int | None = None
        self.gaps = 0  # crossed snapshots refused
        self.resyncs = 0
        self.skipped = 0  # pu breaks: snapshots Binance sent that we never saw

    @property
    def verified(self) -> bool:
        return self.state is SyncState.LIVE

    def on_snapshot(
        self,
        final_id: int,
        prev_final_id: int,
        bids: Sequence[Level],
        asks: Sequence[Level],
    ) -> SyncOutcome:
        if self.last_final_id is not None:
            if final_id <= self.last_final_id:
                return SyncOutcome.DROPPED  # older than what we hold
            if prev_final_id != self.last_final_id:
                self.skipped += 1
        self.book.load(bids, asks)
        if self.book.crossed or self.book.best_bid is None or self.book.best_ask is None:
            self.gaps += 1
            self.book.clear()
            self.state = SyncState.SYNCING
            self.last_final_id = None
            return SyncOutcome.CROSSED
        self.last_final_id = final_id
        self.state = SyncState.LIVE
        return SyncOutcome.APPLIED

    def reset(self) -> None:
        self.resyncs += 1
        self.book.clear()
        self.state = SyncState.SYNCING
        self.last_final_id = None
