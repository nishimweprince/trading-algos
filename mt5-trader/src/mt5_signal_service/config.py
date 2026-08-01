from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    max_candles_lookback: int = Field(
        default=5000, gt=0, validation_alias="MAX_CANDLES_LOOKBACK"
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    profile: str | None = None

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @model_validator(mode="after")
    def validate_defaults(self) -> Settings:
        if self.default_deviation_points > self.maximum_deviation_points:
            raise ValueError("DEFAULT_DEVIATION_POINTS cannot exceed MAXIMUM_DEVIATION_POINTS")
        if not self.allowed_symbols:
            raise ValueError("ALLOWED_SYMBOLS must contain at least one exact broker symbol")
        return self

    @property
    def allowed_symbols(self) -> frozenset[str]:
        return frozenset(
            symbol.strip() for symbol in self.allowed_symbols_csv.split(",") if symbol.strip()
        )
