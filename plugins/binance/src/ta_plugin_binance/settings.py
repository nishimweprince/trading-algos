"""Binance configuration, as a settings mixin.

Mix ``BinanceSettingsMixin`` into a service's settings so BINANCE_* names bind
the same way wherever the plugin is used. Every field has a working default
except the symbol list: public market data needs no credentials.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["BinanceSettingsMixin"]


class BinanceSettingsMixin(BaseSettings):
    # Mirrors BaseServiceSettings; see ta_notify.NotificationSettings for why.
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    binance_rest_url: str = Field(
        default="https://api.binance.com", validation_alias="BINANCE_REST_URL"
    )
    binance_ws_url: str = Field(
        default="wss://stream.binance.com:9443", validation_alias="BINANCE_WS_URL"
    )
    binance_symbols_csv: str = Field(default="", validation_alias="BINANCE_SYMBOLS")
    # Binance allows 6000 weight per minute per IP; stay under it so a burst of
    # candle requests cannot get the IP banned (418).
    binance_request_weight_per_minute: int = Field(
        default=4800, gt=0, le=6000, validation_alias="BINANCE_REQUEST_WEIGHT_PER_MINUTE"
    )
    binance_timeout_seconds: float = Field(
        default=10.0, gt=0, validation_alias="BINANCE_TIMEOUT_SECONDS"
    )
    binance_reconnect_max_backoff_seconds: float = Field(
        default=60.0, gt=0, validation_alias="BINANCE_RECONNECT_MAX_BACKOFF_SECONDS"
    )

    @property
    def binance_symbols(self) -> tuple[str, ...]:
        """Upper-cased, de-duplicated, in configured order."""
        seen: dict[str, None] = {}
        for token in self.binance_symbols_csv.split(","):
            if token.strip():
                seen[token.strip().upper()] = None
        return tuple(seen)
