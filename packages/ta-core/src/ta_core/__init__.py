"""Shared service scaffolding. See README.md.

Errors, structured logging and the settings base import eagerly; they cost
nothing but pydantic-settings. The FastAPI app factory and the uvicorn CLI
bootstrap are resolved lazily through the module ``__getattr__`` below, so
``from ta_core import log_event`` does not drag in FastAPI and uvicorn.

That matters for the adopters outside ``services/``: ipda and lux-algo want the
JSON log formatter and nothing else, and lux-algo has no HTTP server at all.
The names stay importable from the package root either way, so every existing
``from ta_core import create_base_app`` keeps working -- it just pays for
FastAPI at the moment it asks for it. Install ``ta-core[web]`` to have it there.
"""

from typing import TYPE_CHECKING

from .errors import ServiceError
from .logging_config import (
    JsonlLogger,
    configure_file_logs,
    configure_logging,
    log_event,
    reset_file_logs,
)
from .settings import (
    PLACEHOLDER_PREFIX,
    BaseServiceSettings,
    load_settings,
    resolve_env_file,
)

if TYPE_CHECKING:  # so the lazy names still resolve for editors and type checkers
    from .app import (
        COMMON_ERRORS,
        ErrorBody,
        ErrorResponse,
        HealthResponse,
        authenticated,
        create_base_app,
    )
    from .cli import base_parser, load_or_exit, serve

# name -> submodule holding it. Both submodules need the [web] extra.
_LAZY = {
    "COMMON_ERRORS": "app",
    "ErrorBody": "app",
    "ErrorResponse": "app",
    "HealthResponse": "app",
    "authenticated": "app",
    "create_base_app": "app",
    "base_parser": "cli",
    "load_or_exit": "cli",
    "serve": "cli",
}


def __getattr__(name: str) -> object:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        import importlib

        return getattr(importlib.import_module(f".{module}", __name__), name)
    except ImportError as exc:  # pragma: no cover - depends on how ta-core was installed
        raise ImportError(f"ta_core.{name} needs the web extra: install 'ta-core[web]'") from exc


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "COMMON_ERRORS",
    "PLACEHOLDER_PREFIX",
    "BaseServiceSettings",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "JsonlLogger",
    "ServiceError",
    "authenticated",
    "base_parser",
    "configure_file_logs",
    "configure_logging",
    "create_base_app",
    "load_or_exit",
    "load_settings",
    "log_event",
    "reset_file_logs",
    "resolve_env_file",
    "serve",
]
