"""Application configuration loaded from environment / .env via pydantic-settings.

Only the settings consumed by current modules are listed here. Step 2 extends
this with strategy / feed / risk fields.
"""
from typing import Annotated, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # ── Capital.com ───────────────────────────────────────────────────────
    capital_api_key: Optional[str] = None
    capital_identifier: Optional[str] = None
    capital_password: Optional[str] = None
    capital_environment: str = 'demo'

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
    notification_numbers: Annotated[List[str], NoDecode] = Field(
        default_factory=list
    )

    # ── Strategy / market ─────────────────────────────────────────────────
    symbols: Annotated[List[str], NoDecode] = Field(default_factory=list)
    htf_timeframes: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ['4H', '1H']
    )
    ltf_timeframes: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ['15M', '5M']
    )
    rr_target: float = 2.0
    paper_mode: bool = True
    risk_per_trade_pct: float = 0.5
    backfill_candles: int = 500

    # ── FU candle ─────────────────────────────────────────────────────────
    fu_use_doji_filter: bool = False
    fu_use_ma_filter: bool = False
    fu_sma_length: int = 9
    fu_doji_body_ratio: float = 0.3

    # ── FVG ───────────────────────────────────────────────────────────────
    fvg_threshold_pct: float = 0.0
    fvg_auto_threshold: bool = False
    fvg_mtf: str = ''

    # ── SMC structure ─────────────────────────────────────────────────────
    smc_structure_type: str = 'Choch without IDM'
    smc_poi_type: str = '---'
    smc_merge_ratio: float = 0.0
    smc_max_bar_history: int = 2000

    # ── Master Pattern ────────────────────────────────────────────────────
    mp_indi_type: int = 1
    mp_max_bars: int = 500

    # ── Pivot swing ───────────────────────────────────────────────────────
    swing_size_l: int = 15
    swing_size_r: int = 10
    swing_hide_filled: bool = False
    swing_extend_til_filled: bool = True

    # ── Event log ─────────────────────────────────────────────────────────
    indicator_event_log_path: str = './logs/indicator_events.jsonl'

    @field_validator('notification_numbers', 'symbols', 'htf_timeframes',
                     'ltf_timeframes', mode='before')
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
