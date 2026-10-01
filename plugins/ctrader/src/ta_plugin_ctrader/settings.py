"""The configuration this plugin reads, as a structural type.

The plugin never imports a service's settings class. Any object with these
attributes will do; execution-service's ``Settings`` is one, and so will be
market-data-service's. The environment names in the comments are the ones the
service is expected to bind them to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from .accounts import AccountDefinition

__all__ = ["CTraderSettings"]


class CTraderSettings(Protocol):
    profile: str | None

    # Application credentials and OAuth token pair.
    client_id: SecretStr | None  # CTRADER_CLIENT_ID
    client_secret: SecretStr | None  # CTRADER_CLIENT_SECRET
    access_token: SecretStr | None  # CTRADER_ACCESS_TOKEN
    refresh_token: SecretStr | None  # CTRADER_REFRESH_TOKEN
    token_cache_path: Path  # must be unique per process: refresh rotates the pair

    # Legacy single-account session.
    account_id: int | None  # CTRADER_ACCOUNT_ID
    ctrader_port: int  # CTRADER_PORT

    # Multi-account gateway (ACCOUNTS_CONFIG_PATH).
    accounts: tuple[AccountDefinition, ...]
    default_market_data_account: str | None

    # Transport.
    request_timeout_seconds: float
    heartbeat_interval_seconds: float
    connect_timeout_seconds: float
    reconnect_initial_backoff_seconds: float
    reconnect_max_backoff_seconds: float
    reconnect_stability_seconds: float
    historical_requests_per_second: float
    non_historical_requests_per_second: float
    subscriber_queue_size: int

    # Execution safety switches, read when deciding which accounts may trade.
    trading_enabled: bool
    live_trading_enabled: bool

    @property
    def resolved_host(self) -> str: ...

    @property
    def symbols(self) -> frozenset[str]: ...

    @property
    def gateway_enabled(self) -> bool: ...

    @property
    def gateway_accounts(self) -> tuple[AccountDefinition, ...]: ...
