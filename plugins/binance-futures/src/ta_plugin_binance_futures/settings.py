"""Binance USDⓈ-M futures configuration, as a settings mixin.

Mix ``BinanceFuturesSettingsMixin`` into a service's settings so the
BINANCE_FUTURES_* names bind the same way wherever the plugin is used. Public
market data needs only the symbol list; the key and secret are optional and
are only ever used for read-only signed calls (fee rates, account config).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from ta_core import PLACEHOLDER_PREFIX

__all__ = ["BinanceFuturesSettingsMixin"]


class BinanceFuturesSettingsMixin(BaseSettings):
    # Mirrors BaseServiceSettings; see ta_notify.NotificationSettings for why.
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    binance_futures_rest_url: str = Field(
        default="https://fapi.binance.com", validation_alias="BINANCE_FUTURES_REST_URL"
    )
    # The root only: the plugin appends /public (depth, bookTicker) and /market
    # (aggTrade, markPrice, forceOrder). Unrouted URLs were retired 2026-04-23.
    binance_futures_ws_url: str = Field(
        default="wss://fstream.binance.com", validation_alias="BINANCE_FUTURES_WS_URL"
    )
    binance_futures_symbols_csv: str = Field(default="", validation_alias="BINANCE_FUTURES_SYMBOLS")
    # USDⓈ-M allows 2400 weight per minute per IP; half of it leaves room for
    # resync storms and for other processes on the same IP.
    binance_futures_request_weight_per_minute: int = Field(
        default=1200, gt=0, le=2400, validation_alias="BINANCE_FUTURES_REQUEST_WEIGHT_PER_MINUTE"
    )
    binance_futures_timeout_seconds: float = Field(
        default=10.0, gt=0, validation_alias="BINANCE_FUTURES_TIMEOUT_SECONDS"
    )
    binance_futures_reconnect_max_backoff_seconds: float = Field(
        default=30.0, gt=0, validation_alias="BINANCE_FUTURES_RECONNECT_MAX_BACKOFF_SECONDS"
    )
    binance_futures_depth_snapshot_limit: int = Field(
        default=1000, validation_alias="BINANCE_FUTURES_DEPTH_SNAPSHOT_LIMIT"
    )
    # 0ms is the unthrottled diff stream (undocumented for USDⓈ-M but served;
    # hftbacktest's collector uses it). 100/250/500ms are the documented speeds.
    binance_futures_depth_speed: str = Field(
        default="0ms", validation_alias="BINANCE_FUTURES_DEPTH_SPEED"
    )
    # diff: the full local book from <s>@depth@<speed> diffs plus REST snapshots
    # (needs a link that keeps up with the diff stream; Tokyo). partial: complete
    # top-N snapshots from <s>@depth<N>@<speed>, stateless and a few KB/s, so a
    # slow link only makes the book older, never wrong.
    binance_futures_book_mode: Literal["diff", "partial"] = Field(
        default="diff", validation_alias="BINANCE_FUTURES_BOOK_MODE"
    )
    binance_futures_partial_levels: int = Field(
        default=10, validation_alias="BINANCE_FUTURES_PARTIAL_LEVELS"
    )
    binance_futures_partial_speed: Literal["100ms", "250ms", "500ms"] = Field(
        default="100ms", validation_alias="BINANCE_FUTURES_PARTIAL_SPEED"
    )
    # bookTicker is the heaviest stream (every touch change). Unset = on in diff
    # mode, off in partial mode, where the snapshot already carries the touch.
    binance_futures_book_ticker: bool | None = Field(
        default=None, validation_alias="BINANCE_FUTURES_BOOK_TICKER"
    )
    binance_futures_agg_trades: bool = Field(
        default=True, validation_alias="BINANCE_FUTURES_AGG_TRADES"
    )
    binance_futures_api_key: SecretStr | None = Field(
        default=None, validation_alias="BINANCE_FUTURES_API_KEY"
    )
    binance_futures_api_secret: SecretStr | None = Field(
        default=None, validation_alias="BINANCE_FUTURES_API_SECRET"
    )
    # Spot host, used only for GET /sapi/v1/account/apiRestrictions (what the key
    # itself may do). Empty disables the check.
    binance_sapi_url: str = Field(
        default="https://api.binance.com", validation_alias="BINANCE_SAPI_URL"
    )
    binance_futures_recv_window_ms: int = Field(
        default=5000, gt=0, le=60000, validation_alias="BINANCE_FUTURES_RECV_WINDOW_MS"
    )

    @field_validator("binance_futures_depth_snapshot_limit")
    @classmethod
    def valid_depth_limit(cls, value: int) -> int:
        if value not in {5, 10, 20, 50, 100, 500, 1000}:
            raise ValueError("must be one of 5, 10, 20, 50, 100, 500, 1000")
        return value

    @field_validator("binance_futures_partial_levels")
    @classmethod
    def valid_partial_levels(cls, value: int) -> int:
        if value not in {5, 10, 20}:
            raise ValueError("must be one of 5, 10, 20")
        return value

    @field_validator("binance_futures_depth_speed")
    @classmethod
    def valid_depth_speed(cls, value: str) -> str:
        if value not in {"0ms", "100ms", "250ms", "500ms"}:
            raise ValueError("must be one of 0ms, 100ms, 250ms, 500ms")
        return value

    @field_validator("binance_futures_api_key", "binance_futures_api_secret")
    @classmethod
    def reject_placeholder_binance_futures(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value().startswith(PLACEHOLDER_PREFIX):
            raise ValueError("still holds the .env.example placeholder value")
        if value is not None and not value.get_secret_value():
            return None
        return value

    @field_validator("binance_futures_book_ticker", mode="before")
    @classmethod
    def blank_book_ticker_is_auto(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def binance_futures_symbols(self) -> tuple[str, ...]:
        """Upper-cased, de-duplicated, in configured order."""
        seen: dict[str, None] = {}
        for token in self.binance_futures_symbols_csv.split(","):
            if token.strip():
                seen[token.strip().upper()] = None
        return tuple(seen)
