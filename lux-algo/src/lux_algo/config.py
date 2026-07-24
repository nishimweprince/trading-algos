from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Market-data endpoint (polled for 1-minute candles) ---
    data_api_url: str = Field(min_length=1, validation_alias="DATA_API_URL")
    data_api_key: SecretStr | None = Field(default=None, validation_alias="DATA_API_KEY")
    quote: str = Field(min_length=1, validation_alias="QUOTE")
    data_lookback: int = Field(default=600, gt=0, validation_alias="DATA_LOOKBACK")
    data_quote_param: str = Field(default="quote", validation_alias="DATA_QUOTE_PARAM")
    data_count_param: str = Field(default="count", validation_alias="DATA_COUNT_PARAM")
    data_timeout_seconds: float = Field(default=10.0, gt=0, validation_alias="DATA_TIMEOUT_SECONDS")

    poll_interval_seconds: float = Field(
        default=15.0, gt=0, validation_alias="POLL_INTERVAL_SECONDS"
    )

    # --- Timeframe / aggregation ---
    target_tf_minutes: int = Field(default=3, gt=0, validation_alias="TARGET_TF_MINUTES")
    bucket_offset_minutes: int = Field(default=0, ge=0, validation_alias="BUCKET_OFFSET_MINUTES")

    # --- Strategy (LuxAlgo Supertrend entry) ---
    supertrend_sensitivity: float = Field(
        default=5.5, gt=0, validation_alias="SUPERTREND_SENSITIVITY"
    )
    supertrend_atr_len: int = Field(default=11, gt=0, validation_alias="SUPERTREND_ATR_LEN")
    sma_len: int = Field(default=13, gt=0, validation_alias="SMA_LEN")
    risk_reward: float = Field(default=2.0, gt=0, validation_alias="RISK_REWARD")
    send_stop_loss: bool = Field(default=True, validation_alias="SEND_STOP_LOSS")
    send_take_profit: bool = Field(default=True, validation_alias="SEND_TAKE_PROFIT")
    price_digits: int = Field(default=5, ge=0, le=10, validation_alias="PRICE_DIGITS")

    # --- Confluence (overlay agreement gate on the Supertrend trigger) ---
    confluence_mode: str = Field(default="unanimous", validation_alias="CONFLUENCE_MODE")
    confluence_threshold: int = Field(default=0, ge=0, validation_alias="CONFLUENCE_THRESHOLD")
    use_range_filter: bool = Field(default=True, validation_alias="USE_RANGE_FILTER")
    use_superichi: bool = Field(default=True, validation_alias="USE_SUPERICHI")
    use_tbo: bool = Field(default=True, validation_alias="USE_TBO")
    use_smart_trail: bool = Field(default=True, validation_alias="USE_SMART_TRAIL")
    use_ha_bias: bool = Field(default=False, validation_alias="USE_HA_BIAS")
    use_macd_color: bool = Field(default=False, validation_alias="USE_MACD_COLOR")
    use_psar: bool = Field(default=False, validation_alias="USE_PSAR")
    # --- Counter-trend vetoes ---
    veto_tp_points: bool = Field(default=False, validation_alias="VETO_TP_POINTS")
    veto_reversals: bool = Field(default=False, validation_alias="VETO_REVERSALS")

    # --- Order fields sent to mt5-trader ---
    mt5_symbol: str = Field(min_length=1, validation_alias="MT5_SYMBOL")
    volume: Decimal = Field(gt=0, validation_alias="VOLUME")
    deviation_points: int | None = Field(default=None, ge=0, validation_alias="DEVIATION_POINTS")
    ignore_signal_age: bool = Field(default=True, validation_alias="IGNORE_SIGNAL_AGE")

    # --- mt5-trader connection ---
    mt5_signal_api_url: str = Field(
        default="http://127.0.0.1:8000", validation_alias="MT5_SIGNAL_API_URL"
    )
    mt5_signal_api_key: SecretStr = Field(min_length=1, validation_alias="MT5_SIGNAL_API_KEY")
    mt5_timeout_seconds: float = Field(default=10.0, gt=0, validation_alias="MT5_TIMEOUT_SECONDS")
    require_ready: bool = Field(default=True, validation_alias="REQUIRE_READY")

    # --- Runtime ---
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    logs_dir: Path = Field(default=Path("logs"), validation_alias="LOGS_DIR")
    max_retries: int = Field(default=3, ge=0, validation_alias="MAX_RETRIES")
    retry_base_delay_ms: int = Field(default=500, gt=0, validation_alias="RETRY_BASE_DELAY_MS")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @field_validator("confluence_mode")
    @classmethod
    def validate_confluence_mode(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"unanimous", "threshold", "off"}:
            raise ValueError("CONFLUENCE_MODE must be unanimous, threshold, or off")
        return normalized

    @model_validator(mode="after")
    def validate_bucket_offset(self) -> Settings:
        if self.bucket_offset_minutes >= self.target_tf_minutes:
            raise ValueError("BUCKET_OFFSET_MINUTES must be less than TARGET_TF_MINUTES")
        return self

    @property
    def enabled_overlays(self) -> frozenset[str]:
        flags = {
            "range_filter": self.use_range_filter,
            "superichi": self.use_superichi,
            "tbo": self.use_tbo,
            "smart_trail": self.use_smart_trail,
            "ha_bias": self.use_ha_bias,
            "macd_color": self.use_macd_color,
            "psar": self.use_psar,
        }
        return frozenset(name for name, on in flags.items() if on)
