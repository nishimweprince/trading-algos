"""Application configuration loaded from environment / .env via pydantic-settings.

Only the settings consumed by current modules are listed here. Step 2 extends
this with strategy / feed / risk fields.
"""
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore',
    )

    # ── App ────────────────────────────────────────────────────────────────
    log_level: str = 'INFO'
    database_url: str = 'sqlite+aiosqlite:///./fu_strategy.db'
    notifications_log_path: str = './logs/notifications.jsonl'
    signal_log_path: str = './logs/signals.jsonl'

    # ── WhatsApp Cloud API ─────────────────────────────────────────────────
    whatsapp_access_token: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_business_account_id: Optional[str] = None
    whatsapp_api_version: str = 'v21.0'
    whatsapp_verify_token: Optional[str] = None
    whatsapp_app_secret: Optional[str] = None
    whatsapp_template_name: Optional[str] = None
    whatsapp_template_language: str = 'en_US'

    # ── Notifications ──────────────────────────────────────────────────────
    notifications_enabled: bool = True
    notification_numbers: List[str] = Field(default_factory=list)

    @field_validator('notification_numbers', mode='before')
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [n.strip() for n in v.split(',') if n.strip()]
        return v

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.whatsapp_access_token and self.whatsapp_phone_number_id)


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
