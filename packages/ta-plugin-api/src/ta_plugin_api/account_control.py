"""Account-wide safety controls a venue may offer: cancel all, flatten, dead-man.

Optional, like ``OcoVenue``: execution-service exposes the routes for accounts
whose provider satisfies ``AccountControlVenue`` and answers 501 for the rest.
These are the actions a kill switch needs, so they are deliberately blunt:

- ``cancel_all``: every open order on the account (or one instrument).
- ``flatten``: close every open position (or one instrument) at market,
  reduce-only, so it can shrink a position but never open or flip one.
- ``dead_man``: arm the venue's own countdown that cancels all open orders if
  it is not refreshed within ``countdown_ms``; each call re-arms it, ``0``
  disarms it. The caller refreshes it every few seconds while alive, so if the
  caller (or the gateway, or the network) dies, the venue cancels by itself.

None of them raise for a venue outcome: failures come back as
``ControlResult(ok=False, ...)`` with the venue's error in ``detail``, because a
caller in the middle of a kill must keep going with the next step. An unknown
account raises ``KeyError`` (execution-service maps it to 404).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["AccountControlVenue", "ControlResult"]


@dataclass(frozen=True)
class ControlResult:
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AccountControlVenue(Protocol):
    async def cancel_all(self, account: str, instrument: str | None = None) -> ControlResult: ...

    async def flatten(self, account: str, instrument: str | None = None) -> ControlResult: ...

    async def dead_man(
        self, account: str, instruments: Sequence[str], countdown_ms: int
    ) -> ControlResult: ...
