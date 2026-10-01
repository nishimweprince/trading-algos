"""The MT5 terminal configuration, as a settings mixin.

Mix ``MT5TerminalSettingsMixin`` into a service's ``BaseServiceSettings``
subclass so MT5_* names bind identically in execution-service and
market-data-service. It satisfies ``terminal.MT5TerminalSettings``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from ta_core import PLACEHOLDER_PREFIX

__all__ = ["MT5TerminalSettingsMixin"]


class MT5TerminalSettingsMixin(BaseSettings):
    # Mirrors BaseServiceSettings; see ta_notify.NotificationSettings for why.
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # Optional at the type level: a host without a terminal has none of these.
    # ProviderFactory.missing_settings reports them when mt5 is enabled.
    terminal_path: Path | None = Field(default=None, validation_alias="MT5_TERMINAL_PATH")
    login: int | None = Field(default=None, gt=0, validation_alias="MT5_LOGIN")
    password: SecretStr | None = Field(default=None, validation_alias="MT5_PASSWORD")
    server: str | None = Field(default=None, min_length=1, validation_alias="MT5_SERVER")
    mt5_timeout_ms: int = Field(default=60_000, gt=0, validation_alias="MT5_TIMEOUT_MS")
    # The strategy-compatible manifest: canonical quote name to exact broker symbol.
    symbols_file: Path | None = Field(default=None, validation_alias="SYMBOLS_FILE")
    # Broker-server time minus UTC. MT5 stamps bars and ticks in server time,
    # which is rarely UTC (often UTC+2/+3 with DST), so every timestamp leaving
    # the plugin subtracts this. Set it per terminal; startup warns when the
    # newest tick disagrees by more than a minute.
    mt5_server_utc_offset_seconds: int = Field(
        default=0, ge=-50400, le=50400, validation_alias="MT5_SERVER_UTC_OFFSET_SECONDS"
    )
    # MT5 pushes nothing to Python, so quotes for the stream are polled.
    mt5_quote_poll_seconds: float = Field(
        default=1.0, gt=0, le=60, validation_alias="MT5_QUOTE_POLL_SECONDS"
    )

    @field_validator("password")
    @classmethod
    def reject_mt5_password_placeholder(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value().startswith(PLACEHOLDER_PREFIX):
            raise ValueError(
                "still holds the .env.example placeholder value; replace it with a real secret"
            )
        return value

    @field_validator("terminal_path")
    @classmethod
    def reject_mt5_terminal_placeholder(cls, value: Path | None) -> Path | None:
        if value is not None and str(value).startswith(PLACEHOLDER_PREFIX):
            raise ValueError(
                "still holds the .env.example placeholder value; replace it with the absolute "
                "path to terminal64.exe"
            )
        return value

    @field_validator("server")
    @classmethod
    def reject_mt5_server_placeholder(cls, value: str | None) -> str | None:
        if value is not None and value.startswith(PLACEHOLDER_PREFIX):
            raise ValueError(
                "still holds the .env.example placeholder value; replace it with the exact "
                "server name shown by MetaTrader 5"
            )
        return value
