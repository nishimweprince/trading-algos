from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import DEFAULT_SIGNAL_SOURCES, SOURCE_SLUG_PATTERN

_SOURCE_SLUG = re.compile(SOURCE_SLUG_PATTERN)


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
    return settings.model_copy(update={"profile": profile})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    terminal_path: Path = Field(validation_alias="MT5_TERMINAL_PATH")
    login: int = Field(gt=0, validation_alias="MT5_LOGIN")
    password: SecretStr = Field(validation_alias="MT5_PASSWORD")
    server: str = Field(min_length=1, validation_alias="MT5_SERVER")
    api_key: SecretStr = Field(min_length=16, validation_alias="API_KEY")
    allowed_symbols_csv: str = Field(min_length=1, validation_alias="ALLOWED_SYMBOLS")
    allowed_signal_sources_csv: str = Field(
        default=DEFAULT_SIGNAL_SOURCES,
        min_length=1,
        validation_alias="ALLOWED_SIGNAL_SOURCES",
    )
    maximum_volume: Decimal = Field(gt=0, validation_alias="MAXIMUM_VOLUME")
    magic_number: int = Field(ge=0, validation_alias="MAGIC_NUMBER")
    database_path: Path = Field(validation_alias="DATABASE_PATH")

    host: str = Field(default="127.0.0.1", min_length=1, validation_alias="HOST")
    port: int = Field(default=8000, gt=0, le=65535, validation_alias="PORT")

    trading_enabled: bool = Field(default=False, validation_alias="TRADING_ENABLED")
    signal_max_age_seconds: int = Field(default=60, gt=0, validation_alias="SIGNAL_MAX_AGE_SECONDS")
    future_tolerance_seconds: int = Field(
        default=5, ge=0, validation_alias="FUTURE_TOLERANCE_SECONDS"
    )
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
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    profile: str | None = None

    notifications_enabled: bool = Field(default=False, validation_alias="NOTIFICATIONS_ENABLED")
    notification_service_url: str = Field(
        default="http://127.0.0.1:3010",
        min_length=1,
        validation_alias="NOTIFICATION_SERVICE_URL",
    )
    notification_api_key: SecretStr | None = Field(
        default=None, validation_alias="NOTIFICATION_API_KEY"
    )
    notification_channels_csv: str = Field(default="", validation_alias="NOTIFICATION_CHANNELS")
    signals_log_path: Path = Field(
        default=Path("logs/signals.jsonl"), validation_alias="SIGNALS_LOG_PATH"
    )

    _VALID_NOTIFICATION_CHANNELS = frozenset({"TELEGRAM", "EMAIL", "SMS", "WHATSAPP"})

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @field_validator("notification_channels_csv", mode="before")
    @classmethod
    def normalize_notification_channels_csv(cls, value: object) -> object:
        if value is None:
            return ""
        return value

    @model_validator(mode="after")
    def validate_defaults(self) -> Settings:
        if self.default_deviation_points > self.maximum_deviation_points:
            raise ValueError("DEFAULT_DEVIATION_POINTS cannot exceed MAXIMUM_DEVIATION_POINTS")
        if not self.allowed_symbols:
            raise ValueError("ALLOWED_SYMBOLS must contain at least one exact broker symbol")
        if not self.allowed_signal_sources:
            raise ValueError("ALLOWED_SIGNAL_SOURCES must contain at least one source slug")
        for source in self.allowed_signal_sources:
            if _SOURCE_SLUG.fullmatch(source) is None:
                raise ValueError(
                    f"ALLOWED_SIGNAL_SOURCES contains invalid slug {source!r}; "
                    "use lowercase letters, digits, and underscores"
                )
        for channel in self.notification_channels:
            if channel not in self._VALID_NOTIFICATION_CHANNELS:
                raise ValueError(
                    f"NOTIFICATION_CHANNELS contains unknown channel {channel!r}; "
                    f"expected one of {sorted(self._VALID_NOTIFICATION_CHANNELS)}"
                )
        return self

    @property
    def notification_channels(self) -> frozenset[str]:
        return frozenset(
            token.strip().upper()
            for token in self.notification_channels_csv.split(",")
            if token.strip()
        )

    @property
    def allowed_symbols(self) -> frozenset[str]:
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
