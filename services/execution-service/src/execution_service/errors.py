from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """An HTTP-shaped failure rendered as {"error": {...}} by the API handlers."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body


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
