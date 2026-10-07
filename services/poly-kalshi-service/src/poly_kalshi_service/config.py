"""Settings: base service fields, NOTIFICATION_*, KALSHI_*, POLY_* and PK_*.

The strategy parameters and the risk limits are read once, at startup. Nothing
at runtime changes them: no endpoint, no Telegram command. Changing one means
editing ``.env`` and restarting, and the decision log records the values each
decision was made with.
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

from .decision import StrategyParams
from .shadow import DEFAULT_VARIANTS, Variant, parse_variants

__all__ = ["ExecutionMode", "Settings", "load_settings"]

SUPPORTED_ASSETS = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE")


class ExecutionMode(StrEnum):
    # Record, decide and log decisions; no simulated orders.
    OFF = "off"
    # Decisions go to the paper venue: simulated fills, real fees, cash ledger.
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseServiceSettings, NotificationSettings):
    port: int = Field(default=8050, gt=0, le=65535, validation_alias="PORT")

    execution_mode: ExecutionMode = Field(
        default=ExecutionMode.PAPER, validation_alias="PK_EXECUTION_MODE"
    )
    assets_csv: str = Field(default="BTC,ETH,SOL,XRP,BNB", validation_alias="PK_ASSETS")

    # The deployed kpc set (manual §3, 2026-09-13).
    gap_threshold: float = Field(default=0.20, gt=0, lt=1, validation_alias="PK_GAP_THRESHOLD")
    max_entry_px: float = Field(default=0.60, gt=0, le=1, validation_alias="PK_MAX_ENTRY_PX")
    notional_usd: float = Field(default=50.0, gt=0, validation_alias="PK_NOTIONAL_USD")
    entry_early_s: int = Field(default=20, ge=0, le=900, validation_alias="PK_ENTRY_EARLY_S")
    entry_late_s: int = Field(default=60, ge=0, le=900, validation_alias="PK_ENTRY_LATE_S")
    # Variants decided alongside the live rule, never ordered (shadow.py). JSON list of
    # {name, gap, cap, early, late, max_kalshi_spread}; empty disables them.
    shadow_variants_json: str = Field(
        default=DEFAULT_VARIANTS, validation_alias="PK_SHADOW_VARIANTS"
    )

    # Paper venue: the order meets the book this long after the decision.
    paper_latency_ms: int = Field(
        default=150, ge=0, le=5000, validation_alias="PK_PAPER_LATENCY_MS"
    )
    paper_start_cash_usd: float = Field(
        default=1000.0, gt=0, validation_alias="PK_PAPER_START_CASH_USD"
    )

    # Hard risk limits. Required: no default is safe for money, even paper money
    # that is meant to rehearse the real thing.
    max_open_notional_usd: float = Field(gt=0, validation_alias="PK_MAX_OPEN_NOTIONAL_USD")
    daily_loss_limit_usd: float = Field(gt=0, validation_alias="PK_DAILY_LOSS_LIMIT_USD")
    kill_file_path: Path = Field(default=Path("data/KILL"), validation_alias="PK_KILL_FILE_PATH")

    record_enabled: bool = Field(default=True, validation_alias="PK_RECORD_ENABLED")
    record_dir: Path = Field(default=Path("data/raw"), validation_alias="PK_RECORD_DIR")
    host_tag: str = Field(default="mac", min_length=1, validation_alias="PK_HOST_TAG")
    min_free_disk_gb: float = Field(default=5.0, ge=0, validation_alias="PK_MIN_FREE_DISK_GB")
    state_dir: Path = Field(default=Path("data/state"), validation_alias="PK_STATE_DIR")
    # Results are read per epoch. Bump it when a fix invalidates earlier paper
    # results: the page, read-out and export then start clean, and the ledger
    # starts again from PK_PAPER_START_CASH_USD (the old one is archived).
    results_epoch: str = Field(
        default="1", min_length=1, max_length=32, validation_alias="PK_RESULTS_EPOCH"
    )
    # Frames buffered per socket before the client stops reading. Polymarket
    # closes a connection whose reader falls behind (1013 "slow consumer"), so
    # bursts need room here; ~1 KB a frame.
    ws_max_queue: int = Field(default=32768, ge=1024, validation_alias="PK_WS_MAX_QUEUE")
    # Polymarket sockets, assets spread across them (0 = one per asset). Each has its
    # own reader and reconnect, and a reconnect drops only its own books.
    poly_sockets: int = Field(default=0, ge=0, le=16, validation_alias="PK_POLY_SOCKETS")
    # One summary log line this often (0 disables): silence then means a stall.
    heartbeat_seconds: float = Field(default=60.0, ge=0, validation_alias="PK_HEARTBEAT_SECONDS")
    # A venue with no message for this long is reported stale and alerted.
    stale_feed_seconds: float = Field(default=30.0, gt=0, validation_alias="PK_STALE_FEED_SECONDS")
    # Discover the next window this long before it opens.
    discover_lead_seconds: int = Field(
        default=120, ge=30, le=600, validation_alias="PK_DISCOVER_LEAD_SECONDS"
    )

    kalshi_api_key_id: str = Field(min_length=1, validation_alias="KALSHI_API_KEY_ID")
    kalshi_private_key_path: Path = Field(validation_alias="KALSHI_PRIVATE_KEY_PATH")
    kalshi_rest_url: str = Field(
        default="https://api.elections.kalshi.com/trade-api/v2", validation_alias="KALSHI_REST_URL"
    )
    kalshi_ws_url: str = Field(
        default="wss://api.elections.kalshi.com/trade-api/ws/v2", validation_alias="KALSHI_WS_URL"
    )
    poly_gamma_url: str = Field(
        default="https://gamma-api.polymarket.com", validation_alias="POLY_GAMMA_URL"
    )
    poly_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        validation_alias="POLY_WS_URL",
    )

    status_host: str = Field(default="127.0.0.1", validation_alias="PK_STATUS_HOST")
    status_port: int = Field(default=8051, gt=0, le=65535, validation_alias="PK_STATUS_PORT")

    # A bot of its own: two getUpdates pollers on one token steal each other's
    # updates, so this must not be notification-service's or ofi's token.
    telegram_bot_token: SecretStr | None = Field(
        default=None, validation_alias="PK_TELEGRAM_BOT_TOKEN"
    )
    telegram_admin_user_ids_csv: str = Field(
        default="", validation_alias="PK_TELEGRAM_ADMIN_USER_IDS"
    )

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("telegram_bot_token")
    @classmethod
    def reject_placeholder_telegram(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value().startswith(PLACEHOLDER_PREFIX):
            raise ValueError("still holds the .env.example placeholder value")
        return value

    @field_validator("kalshi_api_key_id")
    @classmethod
    def reject_placeholder_key_id(cls, value: str) -> str:
        if value.startswith(PLACEHOLDER_PREFIX):
            raise ValueError("still holds the .env.example placeholder value")
        return value

    @property
    def assets(self) -> tuple[str, ...]:
        return tuple(a.strip().upper() for a in self.assets_csv.split(",") if a.strip())

    @property
    def telegram_admin_user_ids(self) -> frozenset[int]:
        return frozenset(
            int(t.strip()) for t in self.telegram_admin_user_ids_csv.split(",") if t.strip()
        )

    @property
    def params(self) -> StrategyParams:
        return StrategyParams(
            gap_threshold=self.gap_threshold,
            max_entry_px=self.max_entry_px,
            notional_usd=self.notional_usd,
            entry_early_s=self.entry_early_s,
            entry_late_s=self.entry_late_s,
        )

    @property
    def shadow_variants(self) -> tuple[Variant, ...]:
        return parse_variants(self.shadow_variants_json, self.params)

    @model_validator(mode="after")
    def check_pk(self) -> Self:
        if self.execution_mode is ExecutionMode.LIVE:
            raise ValueError(
                "PK_EXECUTION_MODE=live is refused: real orders wait for the paper gates "
                "(plan §Gates) and a host in a jurisdiction Polymarket allows"
            )
        if not self.assets:
            raise ValueError("PK_ASSETS is empty")
        unknown = [a for a in self.assets if a not in SUPPORTED_ASSETS]
        if unknown:
            raise ValueError(f"PK_ASSETS has unsupported assets: {', '.join(unknown)}")
        if self.entry_early_s + self.entry_late_s >= 900:
            raise ValueError("PK_ENTRY_EARLY_S + PK_ENTRY_LATE_S leaves no entry window")
        if self.notional_usd > self.max_open_notional_usd:
            raise ValueError("PK_NOTIONAL_USD cannot exceed PK_MAX_OPEN_NOTIONAL_USD")
        try:
            parse_variants(self.shadow_variants_json, self.params)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"PK_SHADOW_VARIANTS: {exc}") from exc
        if self.telegram_bot_token is not None and not self.telegram_admin_user_ids:
            raise ValueError("PK_TELEGRAM_ADMIN_USER_IDS is required with PK_TELEGRAM_BOT_TOKEN")
        return self

    def check_paths(self) -> None:
        """Fail at startup with a fix, not a traceback."""
        key = self.kalshi_private_key_path.expanduser()
        if not key.is_file():
            raise ValueError(
                f"KALSHI_PRIVATE_KEY_PATH={self.kalshi_private_key_path} is not a file. Point it "
                "at the PEM private key downloaded with the Kalshi API key (chmod 600)."
            )
        if not self.record_enabled:
            return
        path = self.record_dir.expanduser().absolute()
        existing = path
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if existing.is_dir() and os.access(existing, os.W_OK | os.X_OK):
            return
        raise ValueError(
            f"PK_RECORD_DIR={self.record_dir} is not writable by this user ({existing}). "
            f"Create it with `sudo mkdir -p {path} && sudo chown $USER: {path}`."
        )


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
    settings.check_paths()
    return settings
