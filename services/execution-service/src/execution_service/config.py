from __future__ import annotations

import re
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from ta_core import PLACEHOLDER_PREFIX, BaseServiceSettings, resolve_env_file
from ta_core import load_settings as _load_settings

# Re-exported: main.py and the tests import it from here, and it is part of this
# module's surface even though the implementation moved to ta-core.
__all__ = [
    "CTRADER_HOSTS",
    "AccountDefinition",
    "AccountRegistry",
    "Settings",
    "load_account_registry",
    "load_settings",
    "resolve_env_file",
]

CTRADER_HOSTS = {
    "demo": "demo.ctraderapi.com",
    "live": "live.ctraderapi.com",
}


def load_settings(profile: str | None = None) -> Settings:
    """Load .env[.profile], then layer the account registry over it.

    The profile-scoped path defaults are a correctness requirement, not a
    convenience: two profiles sharing one token cache mutually invalidate each
    other's rotated refresh tokens, recoverable only by redoing the browser
    OAuth flow.
    """
    settings = _load_settings(
        Settings,
        profile,
        default_example=".env.example.forex",
        profile_scoped_paths={
            "token_cache_path": "data/token-cache.{profile}.json",
            "events_log_path": "logs/events.{profile}.jsonl",
        },
    )
    if settings.accounts_config_path is not None:
        registry = load_account_registry(settings.accounts_config_path)
        settings = settings.model_copy(
            update={
                "accounts": registry.accounts,
                "default_market_data_account": (
                    settings.default_market_data_account or registry.default_market_data_account
                ),
            }
        )
        settings.validate_gateway_configuration()
    return settings


class AccountDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_-]*$")
    ctid_trader_account_id: int = Field(gt=0)
    environment: str
    enabled: bool = True
    instruments: dict[str, str] = Field(min_length=1)

    @field_validator("environment")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in CTRADER_HOSTS:
            raise ValueError("account environment must be demo or live")
        return normalized

    @field_validator("instruments")
    @classmethod
    def normalize_instruments(cls, value: dict[str, str]) -> dict[str, str]:
        normalized = {
            canonical.strip().upper(): broker_symbol.strip()
            for canonical, broker_symbol in value.items()
            if canonical.strip() and broker_symbol.strip()
        }
        if not normalized:
            raise ValueError("account instruments must not be empty")
        return normalized


class AccountRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_market_data_account: str
    accounts: tuple[AccountDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_registry(self) -> AccountRegistry:
        aliases = [account.alias for account in self.accounts]
        ids = [account.ctid_trader_account_id for account in self.accounts]
        if len(aliases) != len(set(aliases)):
            raise ValueError("account aliases must be unique")
        if len(ids) != len(set(ids)):
            raise ValueError("ctidTraderAccountIds must be unique")
        enabled = {account.alias for account in self.accounts if account.enabled}
        if self.default_market_data_account not in enabled:
            raise ValueError("default_market_data_account must name an enabled account")
        return self


def load_account_registry(path: Path) -> AccountRegistry:
    if not path.is_file():
        raise FileNotFoundError(f"Missing account registry {path}")
    with path.open("rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)
    return AccountRegistry.model_validate(raw)


class Settings(BaseServiceSettings):
    """api_key, host, log_level, events_log_path and profile come from the base."""

    client_id: SecretStr = Field(validation_alias="CTRADER_CLIENT_ID")
    client_secret: SecretStr = Field(validation_alias="CTRADER_CLIENT_SECRET")
    access_token: SecretStr = Field(validation_alias="CTRADER_ACCESS_TOKEN")
    refresh_token: SecretStr | None = Field(default=None, validation_alias="CTRADER_REFRESH_TOKEN")
    account_id: int | None = Field(default=None, gt=0, validation_alias="CTRADER_ACCOUNT_ID")
    environment: str = Field(default="demo", validation_alias="CTRADER_ENVIRONMENT")
    ctrader_host: str | None = Field(default=None, validation_alias="CTRADER_HOST")
    ctrader_port: int = Field(default=5035, gt=0, le=65535, validation_alias="CTRADER_PORT")

    symbols_csv: str = Field(default="", validation_alias="SYMBOLS")
    accounts_config_path: Path | None = Field(default=None, validation_alias="ACCOUNTS_CONFIG_PATH")
    default_market_data_account: str | None = Field(
        default=None, validation_alias="DEFAULT_MARKET_DATA_ACCOUNT"
    )
    accounts: tuple[AccountDefinition, ...] = ()

    trading_enabled: bool = Field(default=False, validation_alias="TRADING_ENABLED")
    live_trading_enabled: bool = Field(default=False, validation_alias="LIVE_TRADING_ENABLED")
    max_volume_lots: Decimal | None = Field(default=None, gt=0, validation_alias="MAX_VOLUME_LOTS")
    allowed_order_sources_csv: str = Field(default="", validation_alias="ALLOWED_ORDER_SOURCES")
    signal_max_age_seconds: int = Field(default=60, gt=0, validation_alias="SIGNAL_MAX_AGE_SECONDS")
    future_tolerance_seconds: int = Field(
        default=5, ge=0, validation_alias="FUTURE_TOLERANCE_SECONDS"
    )
    execution_response_timeout_seconds: float = Field(
        default=10.0, gt=0, validation_alias="EXECUTION_RESPONSE_TIMEOUT_SECONDS"
    )
    execution_database_path: Path = Field(
        default=Path("data/executions.sqlite3"), validation_alias="EXECUTION_DATABASE_PATH"
    )
    non_historical_requests_per_second: float = Field(
        default=45.0,
        gt=0,
        le=50.0,
        validation_alias="NON_HISTORICAL_REQUESTS_PER_SECOND",
    )

    # Overrides the base default of 8000; this service has always bound 8010.
    port: int = Field(default=8010, gt=0, le=65535, validation_alias="PORT")

    # The broker drops a connection that has not sent a heartbeat for 10s. The
    # ceiling is a schema constraint, not a comment, because it is a protocol
    # invariant rather than a tuning preference.
    heartbeat_interval_seconds: float = Field(
        default=5.0, gt=0, le=9.0, validation_alias="HEARTBEAT_INTERVAL_SECONDS"
    )
    request_timeout_seconds: float = Field(
        default=10.0, gt=0, validation_alias="REQUEST_TIMEOUT_SECONDS"
    )
    # Covers DNS, TCP and the TLS handshake. Without a bound, a peer that accepts
    # the connection but never finishes TLS hangs the supervisor indefinitely —
    # no error, no backoff, and /health/ready stuck reporting "starting".
    connect_timeout_seconds: float = Field(
        default=15.0, gt=0, validation_alias="CONNECT_TIMEOUT_SECONDS"
    )
    reconnect_initial_backoff_seconds: float = Field(
        default=1.0, gt=0, validation_alias="RECONNECT_INITIAL_BACKOFF_SECONDS"
    )
    reconnect_max_backoff_seconds: float = Field(
        default=60.0, gt=0, validation_alias="RECONNECT_MAX_BACKOFF_SECONDS"
    )
    # How long a connection must survive before its backoff counts as recovered.
    # A broker that accepts the handshake and drops immediately would otherwise
    # reset the backoff on every attempt and never stop hammering.
    reconnect_stability_seconds: float = Field(
        default=30.0, gt=0, validation_alias="RECONNECT_STABILITY_SECONDS"
    )
    # How long startup waits for the first handshake before serving anyway.
    # Blocking forever on a broker outage would make the process undiagnosable;
    # /health/ready reports the real state and the supervisor keeps retrying.
    startup_ready_timeout_seconds: float = Field(
        default=20.0, gt=0, validation_alias="STARTUP_READY_TIMEOUT_SECONDS"
    )

    subscriber_queue_size: int = Field(default=256, gt=0, validation_alias="SUBSCRIBER_QUEUE_SIZE")
    sse_keepalive_seconds: float = Field(
        default=15.0, gt=0, validation_alias="SSE_KEEPALIVE_SECONDS"
    )
    max_candles_lookback: int = Field(default=5000, gt=0, validation_alias="MAX_CANDLES_LOOKBACK")
    tick_staleness_seconds: float = Field(
        default=60.0, gt=0, validation_alias="TICK_STALENESS_SECONDS"
    )
    # cTrader documents 5 req/s on the historical endpoints, per connection.
    historical_requests_per_second: float = Field(
        default=4.0, gt=0, le=5.0, validation_alias="HISTORICAL_REQUESTS_PER_SECOND"
    )

    token_cache_path: Path = Field(
        default=Path("data/token-cache.json"), validation_alias="TOKEN_CACHE_PATH"
    )

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in CTRADER_HOSTS:
            raise ValueError("CTRADER_ENVIRONMENT must be demo or live")
        return normalized

    @field_validator("client_id", "client_secret", "access_token", "refresh_token", "api_key")
    @classmethod
    def reject_placeholder(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value().startswith(PLACEHOLDER_PREFIX):
            raise ValueError(
                "still holds the .env.example placeholder value; replace it with a real secret"
            )
        return value

    @model_validator(mode="after")
    def validate_derived(self) -> Settings:
        if not self.accounts_config_path:
            if self.account_id is None:
                raise ValueError("CTRADER_ACCOUNT_ID is required without ACCOUNTS_CONFIG_PATH")
            if not self.symbols:
                raise ValueError("SYMBOLS must contain at least one exact cTrader symbol name")
        if self.reconnect_initial_backoff_seconds > self.reconnect_max_backoff_seconds:
            raise ValueError(
                "RECONNECT_INITIAL_BACKOFF_SECONDS cannot exceed RECONNECT_MAX_BACKOFF_SECONDS"
            )
        return self

    def validate_gateway_configuration(self) -> None:
        if not self.accounts:
            raise ValueError("ACCOUNTS_CONFIG_PATH contains no accounts")
        if self.max_volume_lots is None:
            raise ValueError("MAX_VOLUME_LOTS is required with ACCOUNTS_CONFIG_PATH")
        if not self.allowed_order_sources:
            raise ValueError("ALLOWED_ORDER_SOURCES is required with ACCOUNTS_CONFIG_PATH")
        enabled_aliases = {account.alias for account in self.enabled_accounts}
        if self.default_market_data_account not in enabled_aliases:
            raise ValueError("DEFAULT_MARKET_DATA_ACCOUNT must name an enabled account")
        invalid_sources = sorted(
            source
            for source in self.allowed_order_sources
            if re.fullmatch(r"[a-z][a-z0-9_]*", source) is None
        )
        if invalid_sources:
            raise ValueError(
                f"ALLOWED_ORDER_SOURCES contains invalid source slugs: {invalid_sources}"
            )

    @property
    def resolved_host(self) -> str:
        return self.ctrader_host or CTRADER_HOSTS[self.environment]

    @property
    def symbols(self) -> frozenset[str]:
        """Exact, case-sensitive cTrader symbol names. Startup fails on any that
        cannot be resolved against the broker's catalog."""
        return frozenset(symbol.strip() for symbol in self.symbols_csv.split(",") if symbol.strip())

    @property
    def source(self) -> str:
        if self.profile:
            return f"ctrader-markets.{self.profile}"
        return "ctrader-markets"

    @property
    def gateway_enabled(self) -> bool:
        return bool(self.accounts)

    @property
    def enabled_accounts(self) -> tuple[AccountDefinition, ...]:
        return tuple(account for account in self.accounts if account.enabled)

    @property
    def allowed_order_sources(self) -> frozenset[str]:
        return frozenset(
            value.strip().lower()
            for value in self.allowed_order_sources_csv.split(",")
            if value.strip()
        )

    def account(self, alias: str) -> AccountDefinition:
        for account in self.enabled_accounts:
            if account.alias == alias:
                return account
        raise KeyError(alias)
