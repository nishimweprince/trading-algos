from __future__ import annotations

import re
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from ta_contracts import DEFAULT_SIGNAL_SOURCES
from ta_core import PLACEHOLDER_PREFIX, BaseServiceSettings, resolve_env_file
from ta_core import load_settings as _load_settings
from ta_notify import NotificationSettings

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


class Settings(BaseServiceSettings, NotificationSettings):
    """api_key, host, log_level, events_log_path and profile come from the base.

    The NOTIFICATION_* fields come from ta-notify's mixin, which is the same set
    mt5-trader declared by hand.
    """

    # Optional at the type level, required by validate_adapter_requirements when
    # ctrader is enabled. Symmetric with the MetaTrader 5 block below: an
    # MT5-only Windows host has no cTrader credentials and must still start.
    client_id: SecretStr | None = Field(default=None, validation_alias="CTRADER_CLIENT_ID")
    client_secret: SecretStr | None = Field(default=None, validation_alias="CTRADER_CLIENT_SECRET")
    access_token: SecretStr | None = Field(default=None, validation_alias="CTRADER_ACCESS_TOKEN")
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

    # --- adapter selection ---------------------------------------------------
    #
    # Which brokers this process talks to. The same codebase runs on macOS with
    # ADAPTERS=ctrader and on the Windows host with ADAPTERS=mt5; the adapter
    # module is imported lazily so a macOS install never touches MetaTrader5,
    # which has no wheel outside Windows.
    adapters_csv: str = Field(default="ctrader", validation_alias="ADAPTERS")

    # --- MetaTrader 5 --------------------------------------------------------
    #
    # Optional at the type level, required by validate_adapter_requirements when
    # mt5 is enabled. They cannot simply be required: a cTrader-only deployment
    # has no terminal, no login and no MT5 server, and must still start.
    terminal_path: Path | None = Field(default=None, validation_alias="MT5_TERMINAL_PATH")
    login: int | None = Field(default=None, gt=0, validation_alias="MT5_LOGIN")
    password: SecretStr | None = Field(default=None, validation_alias="MT5_PASSWORD")
    server: str | None = Field(default=None, min_length=1, validation_alias="MT5_SERVER")
    allowed_symbols_csv: str = Field(default="", validation_alias="ALLOWED_SYMBOLS")
    allowed_signal_sources_csv: str = Field(
        default=DEFAULT_SIGNAL_SOURCES,
        min_length=1,
        validation_alias="ALLOWED_SIGNAL_SOURCES",
    )
    maximum_volume: Decimal | None = Field(default=None, gt=0, validation_alias="MAXIMUM_VOLUME")
    magic_number: int = Field(default=0, ge=0, validation_alias="MAGIC_NUMBER")
    database_path: Path = Field(default=Path("data/signals.db"), validation_alias="DATABASE_PATH")
    default_deviation_points: int = Field(
        default=10, ge=0, validation_alias="DEFAULT_DEVIATION_POINTS"
    )
    maximum_deviation_points: int = Field(
        default=20,
        ge=0,
        validation_alias=AliasChoices("MAXIMUM_DEVIATION_POINTS", "MAX_DEVIATION_POINTS"),
    )
    mt5_timeout_ms: int = Field(default=60_000, gt=0, validation_alias="MT5_TIMEOUT_MS")
    max_candles_lookback: int = Field(default=5000, gt=0, validation_alias="MAX_CANDLES_LOOKBACK")
    signals_log_path: Path = Field(
        default=Path("logs/signals.jsonl"), validation_alias="SIGNALS_LOG_PATH"
    )

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

    @property
    def adapters(self) -> tuple[str, ...]:
        return tuple(
            token.strip().lower() for token in self.adapters_csv.split(",") if token.strip()
        )

    @property
    def allowed_symbols(self) -> frozenset[str]:
        """Case is preserved deliberately.

        MetaTrader 5 symbol lookup is case-sensitive and Deriv names them
        "Volatility 75 Index", "Step Index". Upper-casing here silently breaks
        every Deriv symbol.
        """
        return frozenset(
            symbol.strip() for symbol in self.allowed_symbols_csv.split(",") if symbol.strip()
        )

    @property
    def allowed_signal_sources(self) -> frozenset[str]:
        return frozenset(
            token.strip().lower()
            for token in self.allowed_signal_sources_csv.split(",")
            if token.strip()
        )

    @model_validator(mode="after")
    def colocate_mt5_event_log(self) -> Settings:
        """Keep an MT5 host's events.jsonl beside its signals.jsonl.

        mt5-trader derived it that way (`signals_log_path.parent / events.jsonl`)
        rather than from EVENTS_LOG_PATH, and operators' log tooling points at
        that directory. Only applies when EVENTS_LOG_PATH was not set explicitly.
        """
        if "mt5" in self.adapters and "events_log_path" not in self.model_fields_set:
            object.__setattr__(
                self, "events_log_path", self.signals_log_path.parent / "events.jsonl"
            )
        return self

    @model_validator(mode="after")
    def validate_adapter_requirements(self) -> Settings:
        """Require an adapter's configuration only when that adapter is enabled.

        The alternative — making the MetaTrader 5 fields unconditionally
        required — would stop a cTrader-only host from starting for want of a
        terminal path it will never use.
        """
        known = {"ctrader", "mt5"}
        unknown = set(self.adapters) - known
        if unknown:
            raise ValueError(
                f"unknown ADAPTERS: {', '.join(sorted(unknown))}; "
                f"allowed: {', '.join(sorted(known))}"
            )
        if not self.adapters:
            raise ValueError("ADAPTERS must name at least one adapter")
        if "ctrader" in self.adapters:
            missing_ctrader = [
                alias
                for alias, value in (
                    ("CTRADER_CLIENT_ID", self.client_id),
                    ("CTRADER_CLIENT_SECRET", self.client_secret),
                    ("CTRADER_ACCESS_TOKEN", self.access_token),
                )
                if value is None
            ]
            if missing_ctrader:
                raise ValueError(
                    f"ADAPTERS includes ctrader, which requires: {', '.join(missing_ctrader)}"
                )
        if "mt5" in self.adapters:
            # Ported from mt5-trader's validate_defaults. These are not covered
            # by the missing-field check below because they constrain values
            # that have defaults.
            if self.default_deviation_points > self.maximum_deviation_points:
                raise ValueError("DEFAULT_DEVIATION_POINTS cannot exceed MAXIMUM_DEVIATION_POINTS")
            if not self.allowed_signal_sources:
                raise ValueError("ALLOWED_SIGNAL_SOURCES must contain at least one source slug")
            for source in self.allowed_signal_sources:
                if re.fullmatch(r"[a-z][a-z0-9_]*", source) is None:
                    raise ValueError(
                        f"ALLOWED_SIGNAL_SOURCES contains invalid slug {source!r}; "
                        "use lowercase letters, digits, and underscores"
                    )
            missing = [
                alias
                for alias, value in (
                    ("MT5_TERMINAL_PATH", self.terminal_path),
                    ("MT5_LOGIN", self.login),
                    ("MT5_PASSWORD", self.password),
                    ("MT5_SERVER", self.server),
                    ("MAXIMUM_VOLUME", self.maximum_volume),
                )
                if value is None
            ]
            if not self.allowed_symbols:
                missing.append("ALLOWED_SYMBOLS")
            if missing:
                raise ValueError(f"ADAPTERS includes mt5, which requires: {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def validate_derived(self) -> Settings:
        # Scoped to the cTrader adapter: these describe a cTrader connection, and
        # an MT5-only host configures none of them.
        if "ctrader" in self.adapters and not self.accounts_config_path:
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

    @property
    def allowed_order_sources(self) -> frozenset[str]:
        return frozenset(
            value.strip().lower()
            for value in self.allowed_order_sources_csv.split(",")
            if value.strip()
        )

    def account(self, alias: str) -> AccountDefinition:
        numeric_id = int(alias) if alias.isdecimal() else None
        for account in self.gateway_accounts:
            if account.alias == alias or account.ctid_trader_account_id == numeric_id:
                return account
        raise KeyError(alias)
