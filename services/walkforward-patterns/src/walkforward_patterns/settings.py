from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict
from ta_notify import NotificationSettings

ROOT = Path(__file__).resolve().parents[2]

DIGITS = {"XAUUSD": 2, "USDJPY": 3, "AUDUSD": 5}
CONTRACT = {"XAUUSD": 100.0, "USDJPY": 100_000.0, "AUDUSD": 100_000.0}  # units per lot (FTMO cTrader)


class Settings(NotificationSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      case_sensitive=False, populate_by_name=True)

    hub_url: str = Field(default="http://127.0.0.1:8010", validation_alias="WFP_HUB_URL")
    hub_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="WFP_HUB_API_KEY")
    account: str = Field(default="ftmo_wf_demo", validation_alias="WFP_ACCOUNT")
    config_path: Path = Field(default=ROOT / "config" / "strategies.yaml", validation_alias="WFP_CONFIG")
    db_path: Path = Field(default=ROOT / "data" / "journal.sqlite3", validation_alias="WFP_DB")
    status_host: str = Field(default="127.0.0.1", validation_alias="WFP_STATUS_HOST")
    status_port: int = Field(default=8083, validation_alias="WFP_STATUS_PORT")
    warmup_days: int = Field(default=175, ge=60, validation_alias="WFP_WARMUP_DAYS")
    # Orders are logged, not sent. The owner chose to go straight to demo; this exists for local runs and tests.
    dry_run: bool = Field(default=False, validation_alias="WFP_DRY_RUN")
    bar_wait_seconds: float = Field(default=25.0, validation_alias="WFP_BAR_WAIT_SECONDS")
    # FTMO limits: RECORD ONLY (owner, 2026-11-17). Breaches are logged and notified, never acted on.
    initial_balance: float = Field(default=100_000.0, validation_alias="WFP_INITIAL_BALANCE")
    daily_loss_pct: float = Field(default=5.0, validation_alias="WFP_DAILY_LOSS_PCT")
    max_loss_pct: float = Field(default=10.0, validation_alias="WFP_MAX_LOSS_PCT")
