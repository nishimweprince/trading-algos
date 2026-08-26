"""Errors specific to this service.

`ServiceError` — the HTTP-shaped failure every service raises — now lives in
ta-core and is re-exported here so call sites are unchanged. What stays is
broker-specific and has no business in a shared package.
"""

from __future__ import annotations

from ta_core import ServiceError

__all__ = [
    "CTraderError",
    "CTraderTimeout",
    "FrameError",
    "ServiceError",
    "SymbolResolutionError",
]


class CTraderError(RuntimeError):
    """A ProtoOAErrorRes returned by the broker, carrying its error code."""

    def __init__(self, error_code: str, description: str | None = None) -> None:
        super().__init__(f"{error_code}: {description}" if description else error_code)
        self.error_code = error_code
        self.description = description


class CTraderTimeout(TimeoutError):
    """A correlated request was not answered inside the request timeout."""


class FrameError(ValueError):
    """A length-prefixed frame was malformed or implausibly large."""


class SymbolResolutionError(ValueError):
    """A configured or requested symbol has no unambiguous broker mapping."""
