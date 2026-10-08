"""Where an entry goes. Paper only: the live port refuses.

``PaperVenue`` models a marketable FAK limit at the decision's ``entry_px``:

- It meets the book ``latency_ms`` **after** the decision (the engine resolves
  the order on the first event at or past that time, against the book as it
  stood then). If the ask moved above the limit, nothing fills.
- It walks the ask levels up to the limit and takes what is there: partial
  fills are normal, the remainder is cancelled.
- The fee is computed per level from each level's price.

It also reports the naive kpc assumption (100% at the recorded touch, zero
latency) alongside, so the gap between the two is measured, not assumed
(kpc trap 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .book import Book
from .fees import taker_fee

__all__ = ["FillResult", "LiveVenue", "PaperVenue", "Venue"]


@dataclass(frozen=True, slots=True)
class FillResult:
    shares: float
    cost: float
    fee: float
    levels: int
    best_ask_at_fill: float | None

    @property
    def vwap(self) -> float | None:
        return self.cost / self.shares if self.shares else None


class Venue(Protocol):
    latency_ns: int

    def fill(
        self, book: Book, limit_px: float, shares: int, fee_rate: float, fee_exponent: float
    ) -> FillResult: ...


class PaperVenue:
    def __init__(self, latency_ms: int) -> None:
        self.latency_ns = latency_ms * 1_000_000

    def fill(
        self, book: Book, limit_px: float, shares: int, fee_rate: float, fee_exponent: float
    ) -> FillResult:
        remaining = float(shares)
        cost = fee = 0.0
        levels = 0
        best = book.best_ask() if book.snapshot_ns is not None else None
        if book.snapshot_ns is not None:
            for price, size in book.asks_upto(limit_px):
                if remaining <= 0:
                    break
                take = min(size, remaining)
                cost += take * price
                fee += taker_fee(price, take, fee_rate, fee_exponent)
                remaining -= take
                levels += 1
        filled = shares - remaining
        return FillResult(filled, cost, fee, levels, best)


class LiveVenue:
    """Phase 3. Polymarket CLOB V2 (py-clob-client-v2) from a permitted jurisdiction."""

    latency_ns = 0

    def fill(self, *args: object, **kwargs: object) -> FillResult:
        raise NotImplementedError("live orders are out of scope until the paper gates pass")
