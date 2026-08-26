"""The seam between this service and a broker.

One port, two very different brokers behind it. The asymmetry is the thing to
understand before changing anything here:

- **cTrader is event-driven.** ``place`` sends a ProtoOANewOrderReq and returns;
  the outcome arrives later as a ProtoOAExecutionEvent, which settles the target
  through ``ExecutionRepository.update_target``.
- **MetaTrader 5 is synchronous and blocking.** ``order_send`` returns the
  outcome inline, wrapped in ``asyncio.to_thread`` so it does not stall the loop.

No extra abstraction is needed to reconcile those, because the ledger already
models it: ``TargetState`` runs RESERVED → DISPATCHED → terminal, and
``OperationState`` distinguishes PENDING from UNKNOWN from a terminal state. The
MT5 adapter simply reaches a terminal state inside ``place``; the cTrader adapter
returns DISPATCHED and settles later. The ledger is the join point, so callers
never branch on which broker is behind the port.

An adapter that cannot do something raises ``ServiceError(501, ...)`` rather than
silently succeeding — a market-data-only deployment must fail loudly on an order,
not pretend.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from ta_contracts import (
    Candle,
    Direction,
    ExecutionType,
    SymbolInfo,
    TargetResult,
    Tick,
    Timeframe,
)


class ConnectionSnapshot(Protocol):
    """What ``readiness`` reports. Both brokers already produce this shape."""

    connected: bool
    trade_allowed: bool
    reason: str | None


@runtime_checkable
class BrokerAdapter(Protocol):
    """One broker connection, as the rest of the service sees it."""

    name: str

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        """(ready, details). Details are surfaced verbatim on /health/*."""
        ...

    def accounts(self) -> tuple[str, ...]:
        """Account aliases this adapter serves."""
        ...

    async def reconcile(self) -> None:
        """Re-attach broker state to unresolved ledger targets after a restart.

        Both services already do this: cTrader replays orders and positions on
        reconnect, MT5 scans order history on startup. Without it, a crash
        between dispatch and settlement strands a target in DISPATCHED forever.
        """
        ...

    # --- market data --------------------------------------------------------

    async def symbols(self, account: str) -> list[SymbolInfo]: ...

    async def tick(self, account: str, symbol: str) -> Tick: ...

    async def candles(
        self, account: str, symbol: str, timeframe: Timeframe, count: int
    ) -> list[Candle]: ...

    # --- execution ----------------------------------------------------------

    async def place(
        self,
        *,
        account: str,
        client_order_id: str,
        instrument: str,
        execution_type: ExecutionType,
        direction: Direction,
        volume_lots: Decimal,
        entry_price: Decimal | None = None,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        stop_loss_distance: Decimal | None = None,
        take_profit_distance: Decimal | None = None,
        expires_at: Any | None = None,
        note: str | None = None,
    ) -> TargetResult: ...

    async def amend_order(
        self,
        *,
        account: str,
        order_id: int,
        volume_lots: Decimal | None = None,
        entry_price: Decimal | None = None,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        expires_at: Any | None = None,
    ) -> TargetResult: ...

    async def cancel_order(self, *, account: str, order_id: int) -> TargetResult: ...

    async def amend_position(
        self,
        *,
        account: str,
        position_id: int,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        trailing_stop_loss: bool = False,
    ) -> TargetResult: ...

    async def close_position(
        self, *, account: str, position_id: int, volume_lots: Decimal
    ) -> TargetResult: ...
