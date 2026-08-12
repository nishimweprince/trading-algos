from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .instruments import (
    InstrumentConfig,
    instrument_from_legacy,
    load_instruments_from_file,
)
from .sessions import DEFAULT_SESSION_SPECS, SessionWindow, build_windows


def resolve_env_file(profile: str | None) -> Path:
    if profile is None:
        return Path(".env")
    return Path(f".env.{profile}")


def resolve_symbols_file(env_file: Path, symbols_file: Path | None) -> Path | None:
    if symbols_file is None:
        return None
    if symbols_file.is_absolute():
        return symbols_file
    return env_file.parent / symbols_file


def load_settings(profile: str | None = None) -> Settings:
    env_file = resolve_env_file(profile)
    if not env_file.is_file():
        hint = f".env.example.{profile}" if profile else ".env.example.forex"
        raise FileNotFoundError(f"Missing {env_file}. Copy {hint} and configure it.")
    settings = Settings(_env_file=env_file, _env_file_encoding="utf-8")
    symbols_path = resolve_symbols_file(env_file, settings.symbols_file)
    if symbols_path is not None:
        instruments = load_instruments_from_file(symbols_path)
    else:
        instruments = [instrument_from_legacy(settings.quote, settings.mt5_symbol)]
    return settings.model_copy(update={"profile": profile, "instruments": instruments})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    data_api_url: str = Field(min_length=1, validation_alias="DATA_API_URL")
    data_api_key: SecretStr | None = Field(default=None, validation_alias="DATA_API_KEY")
    symbols_file: Path | None = Field(default=None, validation_alias="SYMBOLS_FILE")
    quote: str = Field(default="", validation_alias="QUOTE")
    data_lookback: int = Field(default=600, gt=0, validation_alias="DATA_LOOKBACK")
    data_quote_param: str = Field(default="quote", validation_alias="DATA_QUOTE_PARAM")
    data_count_param: str = Field(default="count", validation_alias="DATA_COUNT_PARAM")
    data_timeout_seconds: float = Field(default=10.0, gt=0, validation_alias="DATA_TIMEOUT_SECONDS")

    poll_interval_seconds: float = Field(
        default=15.0, gt=0, validation_alias="POLL_INTERVAL_SECONDS"
    )

    target_tf_minutes: int = Field(default=5, gt=0, validation_alias="TARGET_TF_MINUTES")
    bucket_offset_minutes: int = Field(default=0, ge=0, validation_alias="BUCKET_OFFSET_MINUTES")

    # Reversal trigger (file.txt Section 11) — the live entry.
    reversal_rsi_len: int = Field(default=14, gt=0, validation_alias="REVERSAL_SENSITIVITY")
    reversal_oversold: float = Field(
        default=25.0, ge=0, le=100, validation_alias="REVERSAL_OVERSOLD"
    )
    reversal_overbought: float = Field(
        default=75.0, ge=0, le=100, validation_alias="REVERSAL_OVERBOUGHT"
    )

    # Supertrend trigger (file.txt Section 5) — the alternative entry; Pine defaults.
    supertrend_sensitivity: float = Field(
        default=5.5, gt=0, validation_alias="SUPERTREND_SENSITIVITY"
    )
    supertrend_atr_len: int = Field(default=11, gt=0, validation_alias="SUPERTREND_ATR_LEN")
    sma_len: int = Field(default=13, gt=0, validation_alias="SMA_LEN")
    risk_reward: float = Field(default=2.0, gt=0, validation_alias="RISK_REWARD")
    send_stop_loss: bool = Field(default=True, validation_alias="SEND_STOP_LOSS")
    send_take_profit: bool = Field(default=True, validation_alias="SEND_TAKE_PROFIT")
    use_hard_targets: bool = Field(default=True, validation_alias="USE_HARD_TARGETS")
    stop_loss_pips: float = Field(default=40.0, gt=0, validation_alias="STOP_LOSS_PIPS")
    take_profit_pips: float = Field(default=50.0, gt=0, validation_alias="TAKE_PROFIT_PIPS")
    price_digits: int = Field(default=5, ge=0, le=10, validation_alias="PRICE_DIGITS")
    pip_size_override: float | None = Field(default=None, gt=0, validation_alias="PIP_SIZE")

    trading_sessions_csv: str = Field(
        default="tokyo,new_york", validation_alias="TRADING_SESSIONS"
    )
    session_tokyo: str = Field(
        default=DEFAULT_SESSION_SPECS["tokyo"], validation_alias="SESSION_TOKYO"
    )
    session_new_york: str = Field(
        default=DEFAULT_SESSION_SPECS["new_york"], validation_alias="SESSION_NEW_YORK"
    )

    notifications_enabled: bool = Field(default=False, validation_alias="NOTIFICATIONS_ENABLED")
    notification_service_url: str = Field(
        default="http://127.0.0.1:3010", min_length=1, validation_alias="NOTIFICATION_SERVICE_URL"
    )
    notification_api_key: SecretStr | None = Field(
        default=None, validation_alias="NOTIFICATION_API_KEY"
    )
    notification_channels_csv: str = Field(
        default="TELEGRAM", validation_alias="NOTIFICATION_CHANNELS"
    )
    notification_timeout_seconds: float = Field(
        default=30.0, gt=0, validation_alias="NOTIFICATION_TIMEOUT_SECONDS"
    )

    mfe_break_even_pips: float = Field(default=30.0, gt=0, validation_alias="MFE_BREAK_EVEN_PIPS")
    track_open_trades: bool = Field(default=True, validation_alias="TRACK_OPEN_TRADES")
    tracked_trade_ttl_hours: float = Field(
        default=24.0, gt=0, validation_alias="TRACKED_TRADE_TTL_HOURS"
    )

    mt5_symbol: str = Field(default="", validation_alias="MT5_SYMBOL")
    volume: Decimal = Field(gt=0, validation_alias="VOLUME")
    deviation_points: int | None = Field(default=None, ge=0, validation_alias="DEVIATION_POINTS")
    ignore_signal_age: bool = Field(default=True, validation_alias="IGNORE_SIGNAL_AGE")

    mt5_signal_api_url: str = Field(
        default="http://127.0.0.1:8000", validation_alias="MT5_SIGNAL_API_URL"
    )
    mt5_signal_api_key: SecretStr = Field(min_length=1, validation_alias="MT5_SIGNAL_API_KEY")
    mt5_timeout_seconds: float = Field(default=10.0, gt=0, validation_alias="MT5_TIMEOUT_SECONDS")
    require_ready: bool = Field(default=True, validation_alias="REQUIRE_READY")

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    logs_dir: Path = Field(default=Path("logs"), validation_alias="LOGS_DIR")
    max_retries: int = Field(default=3, ge=0, validation_alias="MAX_RETRIES")
    retry_base_delay_ms: int = Field(default=500, gt=0, validation_alias="RETRY_BASE_DELAY_MS")
    profile: str | None = None
    instruments: list[InstrumentConfig] = Field(default_factory=list)

    @field_validator("symbols_file", mode="before")
    @classmethod
    def empty_symbols_file_as_none(cls, value: object) -> object:
        if value == "" or value is None:
            return None
        return value

    @field_validator("deviation_points", mode="before")
    @classmethod
    def empty_deviation_points_as_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @model_validator(mode="after")
    def validate_bucket_offset(self) -> Settings:
        if self.bucket_offset_minutes >= self.target_tf_minutes:
            raise ValueError("BUCKET_OFFSET_MINUTES must be less than TARGET_TF_MINUTES")
        return self

    @model_validator(mode="after")
    def validate_quote_or_symbols_file(self) -> Settings:
        if self.symbols_file is None:
            if not self.quote:
                raise ValueError("QUOTE is required when SYMBOLS_FILE is not set")
            if not self.mt5_symbol:
                raise ValueError("MT5_SYMBOL is required when SYMBOLS_FILE is not set")
        return self

    @field_validator("pip_size_override", mode="before")
    @classmethod
    def empty_pip_size_as_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @property
    def pip_size(self) -> float:
        if self.pip_size_override is not None:
            return self.pip_size_override
        return 10.0 ** -(self.price_digits - 1)

    @property
    def trading_sessions(self) -> list[str]:
        """Session names to trade in. Empty means no session restriction."""
        return [
            token.strip().lower()
            for token in self.trading_sessions_csv.split(",")
            if token.strip()
        ]

    @property
    def session_specs(self) -> dict[str, str]:
        return {"tokyo": self.session_tokyo, "new_york": self.session_new_york}

    def session_windows(self) -> list[SessionWindow]:
        """Resolved windows. Raises ValueError on an unknown name or bad spec."""
        return build_windows(self.trading_sessions, self.session_specs)

    @property
    def notification_channels(self) -> frozenset[str]:
        return frozenset(
            token.strip().upper()
            for token in self.notification_channels_csv.split(",")
            if token.strip()
        )

    @model_validator(mode="after")
    def validate_reversal_levels(self) -> Settings:
        if self.reversal_oversold >= self.reversal_overbought:
            raise ValueError("REVERSAL_OVERSOLD must be below REVERSAL_OVERBOUGHT")
        return self

    @model_validator(mode="after")
    def validate_hard_targets(self) -> Settings:
        # RSI produces no price level, so the reversal trigger has no indicator-derived
        # stop to fall back on. Without hard targets it would send orders with no risk.
        if not self.use_hard_targets:
            raise ValueError(
                "USE_HARD_TARGETS must be true: the reversal trigger has no "
                "indicator-derived stop level, so SL/TP must be fixed pip distances"
            )
        return self

    @model_validator(mode="after")
    def validate_sessions(self) -> Settings:
        # Fail at startup rather than at the first out-of-hours signal.
        self.session_windows()
        return self

    @model_validator(mode="after")
    def validate_notification_channels(self) -> Settings:
        allowed = {"TELEGRAM", "EMAIL", "SMS", "WHATSAPP"}
        unknown = self.notification_channels - allowed
        if unknown:
            raise ValueError(
                f"unknown NOTIFICATION_CHANNELS: {', '.join(sorted(unknown))}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )
        if self.notifications_enabled and not self.notification_channels:
            raise ValueError("NOTIFICATION_CHANNELS is required when NOTIFICATIONS_ENABLED=true")
        return self
