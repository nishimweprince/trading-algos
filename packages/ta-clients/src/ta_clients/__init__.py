"""Typed clients for our own services. See README.md."""

from .candles import DEFAULT_PAGE_SIZE, CandleStore, SupportsCandleStore
from .execution import (
    OPERATION_NAMESPACE,
    ExecutionClient,
    ExecutionResult,
    ExecutionState,
    SupportsExecution,
    client_order_id_for,
    decimal_text,
    operation_id_for,
    safe_reason,
    timestamp_text,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "CandleStore",
    "SupportsCandleStore",
    "OPERATION_NAMESPACE",
    "ExecutionClient",
    "ExecutionResult",
    "ExecutionState",
    "SupportsExecution",
    "client_order_id_for",
    "decimal_text",
    "operation_id_for",
    "safe_reason",
    "timestamp_text",
]
