"""Process settings for ``zt shadow`` / ``zt live``: environment only, no secrets in files.

Strategy parameters stay in the YAML config; this holds where things live, who
gets alerted, how much capital the bot accounts for, and the rollout stage.
Live mode is refused unless ``ZT_LIVE_ACK`` carries the exact acknowledgement
string, so a misconfigured host cannot start trading real money by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from ta_notify import NotificationSettings
from ta_plugin_binance_futures.settings import BinanceFuturesSettingsMixin

from zone_trend.config import DEFAULT_PATH

__all__ = ["LIVE_ACK", "STAGES", "LiveSettings", "Stage"]

LIVE_ACK = "real-money-zone-trend"

Stage = Literal["micro", "half", "full"]

# Rollout stages (spec: Shadow -> Micro -> Half -> Full). Code, not configuration.
STAGES: dict[str, dict] = {
    "micro": {
        "symbols": ("BTCUSDT", "ETHUSDT"),
        "max_positions": 1,
        "min_size": True,
        "size_mult": 1.0,
    },
    "half": {"top_n": 5, "max_positions": 2, "min_size": False, "size_mult": 0.5},
    "full": {"min_size": False, "size_mult": 1.0},
}


class LiveSettings(BinanceFuturesSettingsMixin, NotificationSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8", extra="ignore", case_sensitive=False, populate_by_name=True
    )

    profile: str | None = None
    zt_config: Path = Field(default=DEFAULT_PATH, validation_alias="ZT_CONFIG")
    zt_data_dir: Path | None = Field(default=None, validation_alias="ZT_DATA_DIR")
    zt_db_url: str | None = Field(default=None, validation_alias="ZT_DB_URL")
    zt_symbols_csv: str = Field(default="", validation_alias="ZT_SYMBOLS")
    zt_capital: float = Field(default=500.0, gt=0, validation_alias="ZT_CAPITAL")
    zt_stage: Stage = Field(default="micro", validation_alias="ZT_STAGE")
    zt_live_ack: str = Field(default="", validation_alias="ZT_LIVE_ACK")
    zt_lock_file: Path = Field(default=Path("state/zt.lock"), validation_alias="ZT_LOCK_FILE")
    zt_max_clock_drift_ms: int = Field(default=500, validation_alias="ZT_MAX_CLOCK_DRIFT_MS")
    zt_critical_channels_csv: str = Field(
        default="TELEGRAM,EMAIL,SMS,WHATSAPP", validation_alias="ZT_CRITICAL_CHANNELS"
    )
    zt_warning_channels_csv: str = Field(
        default="TELEGRAM,EMAIL", validation_alias="ZT_WARNING_CHANNELS"
    )

    @property
    def zt_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.zt_symbols_csv.split(",") if s.strip()]

    def live_allowed(self) -> str | None:
        """None when live trading may start, else the reason it may not."""
        if self.zt_live_ack != LIVE_ACK:
            return f"set ZT_LIVE_ACK={LIVE_ACK} to trade real money"
        if self.binance_futures_env != "mainnet":
            return "BINANCE_FUTURES_ENV must be mainnet for live mode (there is no testnet stage)"
        if self.binance_futures_trading_api_key is None:
            return "BINANCE_FUTURES_TRADING_API_KEY / _SECRET are not set"
        return None
