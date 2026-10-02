"""Settings: base service fields, NOTIFICATION_*, BINANCE_FUTURES_*, and OFI_*.

The hard risk limits are read here once, at startup, and frozen into
``risk.RiskLimits``. Nothing at runtime can change them: no endpoint, no
Telegram command, no model output. Changing a limit means editing ``.env``
and restarting.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from ta_core import PLACEHOLDER_PREFIX, BaseServiceSettings
from ta_core import load_settings as _load_settings
from ta_notify import NotificationSettings
from ta_plugin_api import MARKET_DATA_GROUP, load_providers
from ta_plugin_binance_futures.settings import BinanceFuturesSettingsMixin

__all__ = ["PROVIDER", "ExecutionMode", "Settings", "load_settings"]

PROVIDER = "binance_futures"


class ExecutionMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    LIVE = "live"


class Settings(BaseServiceSettings, NotificationSettings, BinanceFuturesSettingsMixin):
    port: int = Field(default=8030, gt=0, le=65535, validation_alias="PORT")

    execution_mode: ExecutionMode = Field(
        default=ExecutionMode.OFF, validation_alias="OFI_EXECUTION_MODE"
    )
    grid_ms: int = Field(default=100, ge=10, le=1000, validation_alias="OFI_GRID_MS")
    # Extra off-grid samples when this many trades land inside one grid step.
    burst_trades: int = Field(default=20, ge=2, validation_alias="OFI_BURST_TRADES")
    record_enabled: bool = Field(default=True, validation_alias="OFI_RECORD_ENABLED")
    record_dir: Path = Field(default=Path("data/raw"), validation_alias="OFI_RECORD_DIR")
    host_tag: str = Field(default="mac", min_length=1, validation_alias="OFI_HOST_TAG")
    stale_book_ms: int = Field(default=750, ge=50, validation_alias="OFI_STALE_BOOK_MS")

    maker_fee_bp: float | None = Field(default=None, ge=0, validation_alias="OFI_MAKER_FEE_BP")
    taker_fee_bp: float | None = Field(default=None, ge=0, validation_alias="OFI_TAKER_FEE_BP")
    bnb_fee_discount: bool = Field(default=False, validation_alias="OFI_BNB_FEE_DISCOUNT")

    # Hard risk limits (risk.RiskLimits). Required: no default is safe for money.
    max_position_notional_usd: float = Field(gt=0, validation_alias="OFI_MAX_POSITION_NOTIONAL_USD")
    max_total_notional_usd: float = Field(gt=0, validation_alias="OFI_MAX_TOTAL_NOTIONAL_USD")
    daily_loss_limit_usd: float = Field(gt=0, validation_alias="OFI_DAILY_LOSS_LIMIT_USD")
    max_drawdown_pct: float = Field(
        default=10.0, gt=0, le=50, validation_alias="OFI_MAX_DRAWDOWN_PCT"
    )
    manual_approval_notional_usd: float = Field(
        gt=0, validation_alias="OFI_MANUAL_APPROVAL_NOTIONAL_USD"
    )
    order_rate_fraction: float = Field(
        default=0.5, gt=0, le=1, validation_alias="OFI_ORDER_RATE_FRACTION"
    )
    kill_file_path: Path = Field(default=Path("data/KILL"), validation_alias="OFI_KILL_FILE_PATH")

    # Deterministic regime gate. Empty disables that rule.
    funding_blackout_minutes: float = Field(
        default=2.0, ge=0, validation_alias="OFI_FUNDING_BLACKOUT_MINUTES"
    )
    gate_spread_pctl: float | None = Field(
        default=95.0, gt=0, le=100, validation_alias="OFI_GATE_SPREAD_PCTL"
    )
    gate_vol_1m_bp: float | None = Field(default=None, gt=0, validation_alias="OFI_GATE_VOL_1M_BP")
    gate_liq_burst_usd: float | None = Field(
        default=None, gt=0, validation_alias="OFI_GATE_LIQ_BURST_USD"
    )

    # A bot of its own: two getUpdates pollers on one token steal each other's
    # updates, so this must not be notification-service's TG_BOT_TOKEN.
    telegram_bot_token: SecretStr | None = Field(
        default=None, validation_alias="OFI_TELEGRAM_BOT_TOKEN"
    )
    telegram_admin_user_ids_csv: str = Field(
        default="", validation_alias="OFI_TELEGRAM_ADMIN_USER_IDS"
    )

    @field_validator(
        "gate_spread_pctl",
        "gate_vol_1m_bp",
        "gate_liq_burst_usd",
        "maker_fee_bp",
        "taker_fee_bp",
        "telegram_bot_token",
        mode="before",
    )
    @classmethod
    def blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("telegram_bot_token")
    @classmethod
    def reject_placeholder_telegram(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value().startswith(PLACEHOLDER_PREFIX):
            raise ValueError("still holds the .env.example placeholder value")
        return value

    @property
    def telegram_admin_user_ids(self) -> frozenset[int]:
        ids = set()
        for token in self.telegram_admin_user_ids_csv.split(","):
            if token.strip():
                ids.add(int(token.strip()))
        return frozenset(ids)

    @model_validator(mode="after")
    def check_ofi(self) -> Self:
        if self.execution_mode is ExecutionMode.LIVE:
            raise ValueError(
                "OFI_EXECUTION_MODE=live is refused: no order path exists until the "
                "research gates pass (ofi-scalper-plan.md §1.7)"
            )
        if self.max_position_notional_usd > self.max_total_notional_usd:
            raise ValueError(
                "OFI_MAX_POSITION_NOTIONAL_USD cannot exceed OFI_MAX_TOTAL_NOTIONAL_USD"
            )
        if self.telegram_bot_token is not None and not self.telegram_admin_user_ids:
            raise ValueError("OFI_TELEGRAM_ADMIN_USER_IDS is required with OFI_TELEGRAM_BOT_TOKEN")
        self.telegram_admin_user_ids  # noqa: B018 - parse now so a bad id fails at startup
        return self

    def validate_provider(self) -> None:
        """Discover the plugin and check its configuration. Imports plugins."""
        factory = load_providers(MARKET_DATA_GROUP, [PROVIDER])[PROVIDER]
        missing = factory.missing_settings(self)
        if missing:
            raise ValueError(f"provider {PROVIDER} requires: {', '.join(missing)}")


def load_settings(profile: str | None = None) -> Settings:
    settings = _load_settings(
        Settings,
        profile,
        default_example=".env.example.dev",
        profile_scoped_paths={
            "events_log_path": "logs/events.{profile}.jsonl",
            "record_dir": "data/raw/{profile}",
            "kill_file_path": "data/KILL.{profile}",
        },
    )
    settings.validate_provider()
    return settings
