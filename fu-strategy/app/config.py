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

    # ── Capital.com auto-execution ────────────────────────────────────────
    capital_execution_enabled: bool = True
    capital_execution_timeframe: str = '1M'
    capital_execution_size: float = 1.0
    capital_execution_guaranteed_stop: bool = True
    capital_execution_safety_multiplier: float = 1
    capital_execution_use_market_distance: bool = True
    capital_execution_sl_pct: float = 0.01

    # ── WhatsApp Cloud API ─────────────────────────────────────────────────
    whatsapp_access_token: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_business_account_id: Optional[str] = None
    whatsapp_api_version: str = 'v21.0'
    whatsapp_verify_token: Optional[str] = None
    whatsapp_app_secret: Optional[str] = None
    whatsapp_template_name: Optional[str] = None
    whatsapp_template_language: str = 'en_US'

    # ── Pindo SMS API ─────────────────────────────────────────────────────
    pindo_api_url: str = 'https://api.pindo.io/v1/sms/'
    pindo_token: Optional[str] = None
    pindo_sender_id: str = 'FUStrategy'

    # ── Notifications ──────────────────────────────────────────────────────
    notifications_enabled: bool = True
    notification_numbers: Annotated[List[str], NoDecode] = Field(
        default_factory=list
    )
    notification_channels: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ['whatsapp', 'sms']
    )

    # ── Strategy / market ─────────────────────────────────────────────────
    symbols: Annotated[List[str], NoDecode] = Field(default_factory=list)
    htf_timeframes: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ['4H', '1H']
    )
    ltf_timeframes: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ['15M', '5M', '1M']
    )
    rr_target: float = 2.0
    paper_mode: bool = True
    risk_per_trade_pct: float = 0.5
    backfill_candles: int = 500
    polling_enabled: bool = False
    polling_interval_seconds: int = 60
    polling_tail_candles: int = 5

    # ── FU candle ─────────────────────────────────────────────────────────
    fu_use_doji_filter: bool = False
    fu_use_ma_filter: bool = False
    fu_sma_length: int = 50
    fu_doji_body_ratio: float = 0.3
    fu_only_mode: bool = True
    fu_fire_on_forming: bool = True

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

    @field_validator('notification_numbers', 'notification_channels', 'symbols',
                     'htf_timeframes', 'ltf_timeframes', mode='before')
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [n.strip() for n in v.split(',') if n.strip()]
        return v

    @field_validator('notification_channels', mode='after')
    @classmethod
    def _normalize_channels(cls, v: List[str]) -> List[str]:
        allowed = {'whatsapp', 'sms'}
        channels = [channel.lower() for channel in v]
        return [channel for channel in channels if channel in allowed]

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.whatsapp_access_token and self.whatsapp_phone_number_id)

    @property
    def sms_configured(self) -> bool:
        return bool(self.pindo_token)

    @property
    def whatsapp_notifications_allowed(self) -> bool:
        return 'whatsapp' in self.notification_channels

    @property
    def sms_notifications_allowed(self) -> bool:
        return 'sms' in self.notification_channels


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
