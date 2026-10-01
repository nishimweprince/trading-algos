"""cTrader wire-level failures.

`SymbolResolutionError` is not here: it is the one services catch without
knowing which broker raised it, so it lives in ta-plugin-api.
"""

from __future__ import annotations

__all__ = ["CTraderError", "CTraderTimeout", "FrameError"]


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
