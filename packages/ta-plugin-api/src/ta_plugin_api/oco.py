"""The broker half of one-cancels-other entry groups.

execution-service's ``OcoCoordinator`` owns everything generic about a group:
the durable document, idempotency, admission gates, the leg and group state
machines, winner selection, expiry, faults and incident acknowledgement. A
broker that can host OCO groups publishes an ``OcoVenue``, which owns what
only the broker knows: whether the account can run them, the leg order it
sends, and how to read its own inventory back into the facts the coordinator
decides on.

Venues mutate the group document only where they record a broker intent or
result (``cancel_intent``, ``protection_intent``, ``compensations``…), and they
persist every intent through ``save`` before the call it describes, so a crash
always leaves evidence for the next scan.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from ta_contracts import OcoGroupRequest

__all__ = [
    "FillFacts",
    "LegSend",
    "OcoObservation",
    "OcoVenue",
    "SaveFn",
]

SaveFn = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class LegSend:
    """What sending one leg's entry order did.

    ``result`` is None when the outcome is unknown (no reply, or an error after
    the request left); ``order_id`` is set only when the broker accepted it.
    """

    result: dict[str, Any] | None
    order_id: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class FillFacts:
    """Entry fills of one leg order, with their exits, in UTC."""

    executed_volume: Decimal
    fill_price: Decimal
    position_ids: list[int]
    filled_at_msc: int
    accounting: dict[str, str]
    exit_volume: Decimal
    open_positions: bool
    extra: dict[str, Any] = field(default_factory=dict)


class OcoObservation(Protocol):
    """One consistent snapshot of the broker, queried by the coordinator."""

    def tagged_orders(self, tag: str) -> set[int]:
        """Owned order IDs (live or historical) carrying this leg's tag."""
        ...

    def is_live(self, order_id: int) -> bool: ...

    def terminal_state(self, order_id: int) -> str | None:
        """``cancelled``, ``rejected`` or ``expired`` from history, else None."""
        ...

    def fills(self, order_id: int) -> FillFacts | None: ...


@runtime_checkable
class OcoVenue(Protocol):
    account: str

    def exclusive(self) -> AbstractAsyncContextManager[None]:
        """Serialize with every other call into the same broker session."""
        ...

    # --- admission ------------------------------------------------------------

    def guard(self, symbol: str | None) -> None:
        """Raise ServiceError unless the account (and symbol) can host a group."""
        ...

    def account_identity(self) -> dict[str, Any]:
        """Identity stamped on a new group and checked by ``verify_account``."""
        ...

    def verify_account(self, document: dict[str, Any]) -> None: ...

    def monitor_preflight(self, has_groups: bool, owned_tags: set[str]) -> None:
        """Raise unless the session is usable and holds no untracked owned orders."""
        ...

    # --- placement ------------------------------------------------------------

    def leg_tag(self, group_id: UUID, side: str) -> str: ...

    def prepare_leg(
        self, request: OcoGroupRequest, side: str, tag: str, server_offset: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """(broker order, applied protection) for one pending leg, preflighted."""
        ...

    def send_leg(self, order: dict[str, Any]) -> LegSend: ...

    # --- observation and control ----------------------------------------------

    def observe(self, document: dict[str, Any]) -> OcoObservation: ...

    def cancel_leg(
        self, document: dict[str, Any], leg: dict[str, Any], order_id: int, save: SaveFn
    ) -> None: ...

    def protect_fill(
        self,
        document: dict[str, Any],
        side: str,
        leg: dict[str, Any],
        request: OcoGroupRequest,
        observation: OcoObservation,
        save: SaveFn,
    ) -> None: ...

    def close_positions(
        self,
        document: dict[str, Any],
        leg: dict[str, Any],
        observation: OcoObservation,
        save: SaveFn,
    ) -> None: ...

    def inventory(self) -> dict[str, Any]: ...
