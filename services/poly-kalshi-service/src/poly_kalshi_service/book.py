"""One L2 book per instrument, and the two-sided quote the decision reads.

There is deliberately no last-good cache: a side that goes empty makes the
quote invalid, rather than serving a stale price (kpc trap 6).

Kalshi publishes bids only, on YES and on NO. Its book is stored as YES bids
plus YES asks, where a NO bid at ``p`` is a YES ask at ``1 - p``, so the
Kalshi and Polymarket books read the same way.

**Sizes are rounded** (``SIZE_DP``). Kalshi sends signed fractional deltas
(``"-54.37"``); summing them in floats left about a third of fully cancelled
levels at ~1e-14 contracts instead of 0 (measured 2026-10-06). Those ghost
levels never left the book, and once one sat at the top it crossed the book:
Kalshi BTC showed 0.999 bid / 0.983 ask in production. A crossed book is now
also invalid, never quoted.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Book", "Quote", "px"]

SIZE_DP = 6  # finer than either venue's size precision


def px(value: str | float) -> float:
    """A price as a dict key: 4 dp covers Kalshi's deci-cent and Polymarket's cent ticks."""
    return round(float(value), 4)


@dataclass(frozen=True, slots=True)
class Quote:
    valid: bool
    bid: float | None = None
    ask: float | None = None

    @property
    def mid(self) -> float | None:
        if not self.valid or self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    def as_dict(self) -> dict[str, float | bool | None]:
        return {"valid": self.valid, "bid": self.bid, "ask": self.ask}


INVALID = Quote(False)


class Book:
    __slots__ = ("asks", "bids", "last_update_ns", "snapshot_ns")

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        # Any update at all: the ghost-gap guard needs "printed since the switch".
        self.last_update_ns: int | None = None
        # None until a full snapshot arrived: deltas onto nothing are not a book.
        self.snapshot_ns: int | None = None

    def snapshot(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        recv_ns: int,
    ) -> None:
        self.bids = {p: s for p, s in bids if s > 0}
        self.asks = {p: s for p, s in asks if s > 0}
        self.snapshot_ns = recv_ns
        self.last_update_ns = recv_ns

    def set_level(self, side: str, price: float, size: float, recv_ns: int) -> None:
        levels = self.bids if side == "bid" else self.asks
        size = round(size, SIZE_DP)
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self.last_update_ns = recv_ns

    def add_level(self, side: str, price: float, delta: float, recv_ns: int) -> None:
        levels = self.bids if side == "bid" else self.asks
        self.set_level(side, price, levels.get(price, 0.0) + delta, recv_ns)

    def invalidate(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.snapshot_ns = None

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def state(self, since_ns: int | None = None) -> str:
        """``ok``, or why the quote is invalid: ``no_snapshot``, ``one_sided``,
        ``crossed``, ``not_updated`` (no change since ``since_ns``)."""
        if self.snapshot_ns is None:
            return "no_snapshot"
        if not self.bids or not self.asks:
            return "one_sided"
        if max(self.bids) >= min(self.asks):
            return "crossed"
        if since_ns is not None and (self.last_update_ns is None or self.last_update_ns < since_ns):
            return "not_updated"
        return "ok"

    def quote(self, since_ns: int | None = None) -> Quote:
        """Both sides present and not crossed, a snapshot seen, and (with ``since_ns``)
        updated since then. Anything else is invalid, never a stale price."""
        if self.state(since_ns) != "ok":
            return INVALID
        return Quote(True, self.best_bid(), self.best_ask())

    def asks_upto(self, limit: float) -> list[tuple[float, float]]:
        """Ask levels at or below ``limit``, cheapest first: what a buy at ``limit`` can take."""
        return sorted((p, s) for p, s in self.asks.items() if p <= limit + 1e-9)
