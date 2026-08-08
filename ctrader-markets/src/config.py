from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CTRADER_HOSTS = {
    "demo": "demo.ctraderapi.com",
    "live": "live.ctraderapi.com",
}

# Every secret in .env.example.* starts with this. A value that still carries it
# means the template was copied but never filled in — which for API_KEY would
# otherwise start the service with a key published in this repository.
PLACEHOLDER_PREFIX = "replace-with-"


def resolve_env_file(profile: str | None) -> Path:
    if profile is None:
        return Path(".env")
    return Path(f".env.{profile}")


def load_settings(profile: str | None = None) -> Settings:
    env_file = resolve_env_file(profile)
    if not env_file.is_file():
        hint = f".env.example.{profile}" if profile else ".env.example.forex"
        raise FileNotFoundError(f"Missing {env_file}. Copy {hint} and configure it.")
    settings = Settings(_env_file=env_file, _env_file_encoding="utf-8")

    # Profile-scope the two writable paths unless the env file named them. Two
    # profiles sharing one token cache mutually invalidate each other's rotated
    # refresh tokens, which is only recoverable by redoing the browser OAuth
    # flow — so the default must not be the same path for every profile.
    update: dict[str, object] = {"profile": profile}
    if profile is not None:
        if "token_cache_path" not in settings.model_fields_set:
            update["token_cache_path"] = Path(f"data/token-cache.{profile}.json")
        if "events_log_path" not in settings.model_fields_set:
            update["events_log_path"] = Path(f"logs/events.{profile}.jsonl")
    return settings.model_copy(update=update)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    client_id: SecretStr = Field(validation_alias="CTRADER_CLIENT_ID")
    client_secret: SecretStr = Field(validation_alias="CTRADER_CLIENT_SECRET")
    access_token: SecretStr = Field(validation_alias="CTRADER_ACCESS_TOKEN")
    refresh_token: SecretStr | None = Field(default=None, validation_alias="CTRADER_REFRESH_TOKEN")
    account_id: int = Field(gt=0, validation_alias="CTRADER_ACCOUNT_ID")
    environment: str = Field(default="demo", validation_alias="CTRADER_ENVIRONMENT")
    ctrader_host: str | None = Field(default=None, validation_alias="CTRADER_HOST")
    ctrader_port: int = Field(default=5035, gt=0, le=65535, validation_alias="CTRADER_PORT")

    api_key: SecretStr = Field(min_length=16, validation_alias="API_KEY")
    symbols_csv: str = Field(min_length=1, validation_alias="SYMBOLS")

    host: str = Field(default="127.0.0.1", min_length=1, validation_alias="HOST")
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
    events_log_path: Path = Field(
        default=Path("logs/events.jsonl"), validation_alias="EVENTS_LOG_PATH"
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    profile: str | None = None

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in CTRADER_HOSTS:
            raise ValueError("CTRADER_ENVIRONMENT must be demo or live")
        return normalized

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
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
        if not self.symbols:
            raise ValueError("SYMBOLS must contain at least one exact cTrader symbol name")
        if self.reconnect_initial_backoff_seconds > self.reconnect_max_backoff_seconds:
            raise ValueError(
                "RECONNECT_INITIAL_BACKOFF_SECONDS cannot exceed RECONNECT_MAX_BACKOFF_SECONDS"
            )
        return self

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
