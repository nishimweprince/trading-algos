"""Shared service scaffolding. See README.md."""

from .app import (
    COMMON_ERRORS,
    ErrorBody,
    ErrorResponse,
    HealthResponse,
    authenticated,
    create_base_app,
)
from .cli import base_parser, load_or_exit, serve
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
