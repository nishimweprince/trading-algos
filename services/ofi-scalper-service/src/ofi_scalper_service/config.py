"""Settings: base service fields, NOTIFICATION_*, BINANCE_FUTURES_*, and OFI_*.

The hard risk limits are read here once, at startup, and frozen into
``risk.RiskLimits``. Nothing at runtime can change them: no endpoint, no
Telegram command, no model output. Changing a limit means editing ``.env``
and restarting.
"""

from __future__ import annotations

import os
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
    # Orders built and logged, fills simulated; nothing is sent.
    SHADOW = "shadow"
    # Orders sent to execution-service on Binance demo trading.
    TESTNET = "testnet"
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
    # Opt-in: log every live feature sample here, for `research.replay compare`.
    sample_log_dir: Path | None = Field(default=None, validation_alias="OFI_SAMPLE_LOG_DIR")
    # One summary log line this often (0 disables): silence then means a stall.
    heartbeat_seconds: float = Field(default=60.0, ge=0, validation_alias="OFI_HEARTBEAT_SECONDS")
    # The recorder stops below this much free space on its volume, and alerts.
    min_free_disk_gb: float = Field(default=10.0, ge=0, validation_alias="OFI_MIN_FREE_DISK_GB")

    # Pinned model (model.py). Empty: no model, no entries.
    model_dir: Path = Field(default=Path("models"), validation_alias="OFI_MODEL_DIR")
    model_version: str | None = Field(default=None, validation_alias="OFI_MODEL_VERSION")

    # The bridge's link to execution-service (testnet mode).
    execution_url: str = Field(default="http://127.0.0.1:8010", validation_alias="EXECUTION_URL")
    execution_api_key: SecretStr | None = Field(default=None, validation_alias="EXECUTION_API_KEY")
    execution_account: str = Field(
        default="binance_testnet", min_length=1, validation_alias="EXECUTION_ACCOUNT"
    )
    execution_timeout_seconds: float = Field(
        default=2.0, gt=0, le=30, validation_alias="EXECUTION_TIMEOUT_SECONDS"
    )
    # Fixed order size until Kelly sizing is enabled (after calibration on own fills).
    order_notional_usd: float = Field(
        default=100.0, gt=0, validation_alias="OFI_ORDER_NOTIONAL_USD"
    )
    max_consecutive_unknown: int = Field(
        default=3, ge=1, validation_alias="OFI_MAX_CONSECUTIVE_UNKNOWN"
    )
    dead_man_ms: int = Field(default=15_000, ge=5_000, validation_alias="OFI_DEAD_MAN_MS")
    fill_poll_ms: int = Field(default=200, ge=20, validation_alias="OFI_FILL_POLL_MS")
    # Demo trading's own book: testnet orders are priced from it (signals are not).
    testnet_quotes_ws_url: str = Field(
        default="wss://demo-fstream.binance.com", validation_alias="OFI_TESTNET_QUOTES_WS_URL"
    )
    # Shadow mode's simulated account, so the loss and drawdown halts are exercised.
    shadow_equity_usd: float = Field(default=500.0, gt=0, validation_alias="OFI_SHADOW_EQUITY_USD")

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
    # Stage 2 research outputs: features, labels, candidates, scores, backtests.
    research_dir: Path = Field(default=Path("research/data"), validation_alias="OFI_RESEARCH_DIR")
    # Bridge state, trades and signals (one directory per profile).
    state_dir: Path = Field(default=Path("data/state"), validation_alias="OFI_STATE_DIR")

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
        "sample_log_dir",
        "gate_vol_1m_bp",
        "gate_liq_burst_usd",
        "maker_fee_bp",
        "taker_fee_bp",
        "telegram_bot_token",
        "model_version",
        "execution_api_key",
        mode="before",
    )
    @classmethod
    def blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("execution_api_key")
    @classmethod
    def reject_placeholder_execution_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value().startswith(PLACEHOLDER_PREFIX):
            raise ValueError("still holds the .env.example placeholder value")
        return value

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
                "OFI_EXECUTION_MODE=live is refused: mainnet orders wait for a binding "
                "research gates pass and explicit approval (ofi-scalper-plan.md §1.7, Stage 5)"
            )
        if self.execution_mode is ExecutionMode.SHADOW and self.model_version is None:
            raise ValueError(
                "OFI_EXECUTION_MODE=shadow needs OFI_MODEL_VERSION: shadow runs the live "
                "model, and only a model whose research gates passed"
            )
        if self.execution_mode is ExecutionMode.TESTNET and self.execution_api_key is None:
            raise ValueError("OFI_EXECUTION_MODE=testnet needs EXECUTION_API_KEY")
        if self.order_notional_usd > self.max_position_notional_usd:
            raise ValueError("OFI_ORDER_NOTIONAL_USD cannot exceed OFI_MAX_POSITION_NOTIONAL_USD")
        if self.order_notional_usd > self.manual_approval_notional_usd:
            raise ValueError(
                "OFI_ORDER_NOTIONAL_USD is above OFI_MANUAL_APPROVAL_NOTIONAL_USD: every "
                "order would wait for approval, and a 1-30 s signal cannot wait"
            )
        if self.max_position_notional_usd > self.max_total_notional_usd:
            raise ValueError(
                "OFI_MAX_POSITION_NOTIONAL_USD cannot exceed OFI_MAX_TOTAL_NOTIONAL_USD"
            )
        if self.telegram_bot_token is not None and not self.telegram_admin_user_ids:
            raise ValueError("OFI_TELEGRAM_ADMIN_USER_IDS is required with OFI_TELEGRAM_BOT_TOKEN")
        self.telegram_admin_user_ids  # noqa: B018 - parse now so a bad id fails at startup
        return self

    def check_record_dir(self) -> None:
        """Fail at startup with a fix, not a traceback, if recordings cannot be written.

        The directory may not exist yet (the recorder creates it), but its
        nearest existing ancestor must be writable by this user.
        """
        if not self.record_enabled:
            return
        path = self.record_dir.expanduser().absolute()
        existing = path
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if existing.is_dir() and os.access(existing, os.W_OK | os.X_OK):
            return
        missing = f" ({path} does not exist yet)" if existing != path else ""
        raise ValueError(
            f"OFI_RECORD_DIR={self.record_dir} is not writable by this user: {existing} "
            f"is not a writable directory{missing}. Create it once with "
            f"`sudo mkdir -p {path} && sudo chown $USER: {path}`, or point OFI_RECORD_DIR "
            "somewhere this user owns (or set OFI_RECORD_ENABLED=false)."
        )

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
            "state_dir": "data/state/{profile}",
        },
    )
    settings.validate_provider()
    settings.check_record_dir()
    return settings
