"""The execution-service client, re-exported from ta-clients.

This module was 325 lines that ta-clients now owns; what remains is the import
surface the engine and the execution bridge already use.

The contract that must not drift, restated because it is easy to "fix" by
mistake: a transport failure yields UNKNOWN, never REJECTED. The order may have
reached the broker, so the caller reconciles rather than resubmits.
"""

from __future__ import annotations

from ta_clients import (
    OPERATION_NAMESPACE,
    ExecutionClient,
    ExecutionResult,
    ExecutionState,
    client_order_id_for,
    decimal_text,
    operation_id_for,
    safe_reason,
    timestamp_text,
)

__all__ = [
    "OPERATION_NAMESPACE",
    "ExecutionClient",
    "ExecutionResult",
    "ExecutionState",
    "client_order_id_for",
    "decimal_text",
    "operation_id_for",
    "safe_reason",
    "timestamp_text",
]
