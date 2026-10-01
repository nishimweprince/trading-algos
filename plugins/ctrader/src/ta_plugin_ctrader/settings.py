"""The cTrader configuration, as a structural type and as a settings mixin.

``CTraderSettings`` is what the plugin reads; any object with these attributes
will do. ``CTraderSettingsMixin`` is the usual way to provide it: mix it into a
service's ``BaseServiceSettings`` subclass and the CTRADER_* and transport
environment names bind identically in every service, so execution-service and
market-data-service cannot drift on what a variable means.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Self, TypeVar

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from ta_core import PLACEHOLDER_PREFIX

from .accounts import CTRADER_HOSTS, AccountDefinition, load_account_registry

__all__ = ["CTraderSettings", "CTraderSettingsMixin", "apply_account_registry"]


class CTraderSettings(Protocol):
    profile: str | None

    # Application credentials and OAuth token pair.
    client_id: SecretStr | None  # CTRADER_CLIENT_ID
    client_secret: SecretStr | None  # CTRADER_CLIENT_SECRET
    access_token: SecretStr | None  # CTRADER_ACCESS_TOKEN
    refresh_token: SecretStr | None  # CTRADER_REFRESH_TOKEN
    token_cache_path: Path  # must be unique per process: refresh rotates the pair

    # One-shot discovery against a single account (--discover-symbols).
    account_id: int | None  # CTRADER_ACCOUNT_ID
    ctrader_port: int  # CTRADER_PORT

    # Account registry (ACCOUNTS_CONFIG_PATH).
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

    # Execution safety switches, read when reporting which accounts may trade.
    trading_enabled: bool
    live_trading_enabled: bool

    @property
    def resolved_host(self) -> str: ...

    @property
    def gateway_enabled(self) -> bool: ...

    @property
    def gateway_accounts(self) -> tuple[AccountDefinition, ...]: ...


class CTraderSettingsMixin(BaseSettings):
    # Mirrors BaseServiceSettings, which this is normally mixed into. Repeated
    # rather than inherited so the mixin is also usable on its own; without
    # populate_by_name the aliased fields cannot be set by field name.
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # Set by ta_core.load_settings; declared here so the mixin stands alone.
    profile: str | None = None

    # Optional at the type level: a host that does not run cTrader has none of
    # these. ProviderFactory.missing_settings reports them when it does.
    client_id: SecretStr | None = Field(default=None, validation_alias="CTRADER_CLIENT_ID")
    client_secret: SecretStr | None = Field(default=None, validation_alias="CTRADER_CLIENT_SECRET")
    access_token: SecretStr | None = Field(default=None, validation_alias="CTRADER_ACCESS_TOKEN")
    refresh_token: SecretStr | None = Field(default=None, validation_alias="CTRADER_REFRESH_TOKEN")
    account_id: int | None = Field(default=None, gt=0, validation_alias="CTRADER_ACCOUNT_ID")
    environment: str = Field(default="demo", validation_alias="CTRADER_ENVIRONMENT")
    ctrader_host: str | None = Field(default=None, validation_alias="CTRADER_HOST")
    ctrader_port: int = Field(default=5035, gt=0, le=65535, validation_alias="CTRADER_PORT")

    accounts_config_path: Path | None = Field(default=None, validation_alias="ACCOUNTS_CONFIG_PATH")
    default_market_data_account: str | None = Field(
        default=None, validation_alias="DEFAULT_MARKET_DATA_ACCOUNT"
    )
    accounts: tuple[AccountDefinition, ...] = ()

    trading_enabled: bool = Field(default=False, validation_alias="TRADING_ENABLED")
    live_trading_enabled: bool = Field(default=False, validation_alias="LIVE_TRADING_ENABLED")

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
    # cTrader documents 5 req/s on the historical endpoints, per connection.
    historical_requests_per_second: float = Field(
        default=4.0, gt=0, le=5.0, validation_alias="HISTORICAL_REQUESTS_PER_SECOND"
    )
    non_historical_requests_per_second: float = Field(
        default=45.0,
        gt=0,
        le=50.0,
        validation_alias="NON_HISTORICAL_REQUESTS_PER_SECOND",
    )
    token_cache_path: Path = Field(
        default=Path("data/token-cache.json"), validation_alias="TOKEN_CACHE_PATH"
    )

    @field_validator("environment")
    @classmethod
    def validate_ctrader_environment(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in CTRADER_HOSTS:
            raise ValueError("CTRADER_ENVIRONMENT must be demo or live")
        return normalized

    @field_validator("client_id", "client_secret", "access_token", "refresh_token")
    @classmethod
    def reject_ctrader_placeholder(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value().startswith(PLACEHOLDER_PREFIX):
            raise ValueError(
                "still holds the .env.example placeholder value; replace it with a real secret"
            )
        return value

    @model_validator(mode="after")
    def validate_ctrader_backoff(self) -> Self:
        if self.reconnect_initial_backoff_seconds > self.reconnect_max_backoff_seconds:
            raise ValueError(
                "RECONNECT_INITIAL_BACKOFF_SECONDS cannot exceed RECONNECT_MAX_BACKOFF_SECONDS"
            )
        return self

    @property
    def resolved_host(self) -> str:
        return self.ctrader_host or CTRADER_HOSTS[self.environment]

    @property
    def gateway_enabled(self) -> bool:
        return bool(self.accounts)

    @property
    def enabled_accounts(self) -> tuple[AccountDefinition, ...]:
        return tuple(account for account in self.accounts if account.enabled)

    @property
    def gateway_accounts(self) -> tuple[AccountDefinition, ...]:
        """Accounts the runtime should reconcile with the broker.

        Production discovers the token-authorized set at startup, so every
        registry entry is a candidate even when its old static ``enabled`` flag
        is false. Other profiles retain the explicit enable-list behavior.
        The broker-reported ``isLive`` value remains authoritative at runtime.
        """
        if self.profile == "production":
            return self.accounts
        return self.enabled_accounts

    def account(self, alias: str) -> AccountDefinition:
        numeric_id = int(alias) if alias.isdecimal() else None
        for account in self.gateway_accounts:
            if account.alias == alias or account.ctid_trader_account_id == numeric_id:
                return account
        raise KeyError(alias)


SettingsT = TypeVar("SettingsT", bound="CTraderSettingsMixin")


def apply_account_registry(settings: SettingsT) -> SettingsT:
    """Layer ACCOUNTS_CONFIG_PATH over ``settings``, or return it unchanged."""
    if settings.accounts_config_path is None:
        return settings
    registry = load_account_registry(settings.accounts_config_path)
    return settings.model_copy(
        update={
            "accounts": registry.accounts,
            "default_market_data_account": (
                settings.default_market_data_account or registry.default_market_data_account
            ),
        }
    )
