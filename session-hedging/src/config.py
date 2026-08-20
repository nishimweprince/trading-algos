from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from anchors import SessionAnchor, anchor_from_window, parse_anchor_token
from models import EngineParams, IntrabarMode, PerformanceUnit, Timeframe
from sessions import DEFAULT_SESSION_SPECS, SessionWindow, build_windows

KNOWN_CHANNELS = frozenset({"TELEGRAM", "EMAIL", "SMS", "WHATSAPP"})
PLACEHOLDER_PREFIX = "replace-with-"


def resolve_env_file(profile: str | None) -> Path:
    if profile is None:
        return Path(".env")
    return Path(f".env.{profile}")


def load_settings(profile: str | None = None) -> Settings:
    env_file = resolve_env_file(profile)
    if not env_file.is_file():
        raise FileNotFoundError(f"Missing {env_file}. Copy .env.example and configure it.")
    return Settings(_env_file=env_file, _env_file_encoding="utf-8")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    ctrader_markets_url: str = Field(
        default="http://127.0.0.1:8010", min_length=1, validation_alias="CTRADER_MARKETS_URL"
    )
    ctrader_api_key: SecretStr | None = Field(default=None, validation_alias="CTRADER_API_KEY")
    api_key: SecretStr | None = Field(default=None, validation_alias="API_KEY")

    host: str = Field(default="0.0.0.0", validation_alias="HOST")
    port: int = Field(default=8012, gt=0, validation_alias="PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    symbol: str = Field(default="XAUUSD", min_length=1, validation_alias="SYMBOL")
    # Paper default only. Backtests send timeframe from the client.
    timeframe: Timeframe = Timeframe.M15
    pip_size: float = Field(default=0.1, gt=0, validation_alias="PIP_SIZE")
    lock_pips: float = Field(default=20.0, ge=0, validation_alias="LOCK_PIPS")
    sl_mult: float = Field(default=2.0, gt=0, validation_alias="SL_MULT")
    rr: float = Field(default=3.0, gt=0, validation_alias="RR")
    min_stop_pips: float = Field(default=0.0, ge=0, validation_alias="MIN_STOP_PIPS")
    qty: float = Field(default=1.0, gt=0, validation_alias="QTY")
    qty_ref: float | None = Field(default=None, gt=0, validation_alias="QTY_REF")
    point_value: float = Field(default=1.0, gt=0, validation_alias="POINT_VALUE")
    skip_doji: bool = Field(default=True, validation_alias="SKIP_DOJI")
    orb_minutes: int = Field(default=60, gt=0, validation_alias="ORB_MINUTES")
    entry_delay_minutes: int = Field(default=15, ge=0, validation_alias="ENTRY_DELAY_MINUTES")
    anchor_tolerance_minutes: int = Field(
        default=15, ge=0, validation_alias="ANCHOR_TOLERANCE_MINUTES"
    )
    intrabar_mode: IntrabarMode = Field(
        default=IntrabarMode.M1_CONSERVATIVE, validation_alias="INTRABAR_MODE"
    )
    session_anchors_csv: str = Field(default="", validation_alias="SESSION_ANCHORS")
    initial_capital: float = Field(default=100_000.0, gt=0, validation_alias="INITIAL_CAPITAL")
    performance_unit: PerformanceUnit = Field(
        default=PerformanceUnit.PIPS, validation_alias="PERFORMANCE_UNIT"
    )
    dollars_per_pip_per_qty: float | None = Field(
        default=None, gt=0, validation_alias="DOLLARS_PER_PIP_PER_QTY"
    )

    trading_sessions_csv: str = Field(
        default="tokyo,london,new_york", validation_alias="TRADING_SESSIONS"
    )
    session_tokyo: str = Field(
        default=DEFAULT_SESSION_SPECS["tokyo"], validation_alias="SESSION_TOKYO"
    )
    session_london: str = Field(
        default=DEFAULT_SESSION_SPECS["london"], validation_alias="SESSION_LONDON"
    )
    session_new_york: str = Field(
        default=DEFAULT_SESSION_SPECS["new_york"], validation_alias="SESSION_NEW_YORK"
    )

    paper_enabled: bool = Field(default=True, validation_alias="PAPER_ENABLED")
    poll_interval_seconds: float = Field(
        default=15.0, gt=0, validation_alias="POLL_INTERVAL_SECONDS"
    )
    paper_lookback: int = Field(default=200, gt=0, validation_alias="PAPER_LOOKBACK")

    data_dir: Path = Field(default=Path("data"), validation_alias="DATA_DIR")
    logs_dir: Path = Field(default=Path("logs"), validation_alias="LOGS_DIR")

    notifications_enabled: bool = Field(default=False, validation_alias="NOTIFICATIONS_ENABLED")
    notification_service_url: str = Field(
        default="http://127.0.0.1:3010", min_length=1, validation_alias="NOTIFICATION_SERVICE_URL"
    )
    notification_api_key: SecretStr | None = Field(
        default=None, validation_alias="NOTIFICATION_API_KEY"
    )
    notification_channels_csv: str = Field(
        default="TELEGRAM", validation_alias="NOTIFICATION_CHANNELS"
    )
    notification_timeout_seconds: float = Field(
        default=30.0, gt=0, validation_alias="NOTIFICATION_TIMEOUT_SECONDS"
    )

    @field_validator("ctrader_api_key", "api_key", "notification_api_key", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("ctrader_api_key")
    @classmethod
    def _reject_ctrader_placeholder(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and value.get_secret_value().startswith(PLACEHOLDER_PREFIX):
            raise ValueError(
                "still holds the .env.example placeholder; set it to the running "
                "ctrader-markets API_KEY"
            )
        return value

    @field_validator("notification_channels_csv")
    @classmethod
    def _known_channels(cls, value: str) -> str:
        for token in value.split(","):
            name = token.strip().upper()
            if name and name not in KNOWN_CHANNELS:
                raise ValueError(f"unknown NOTIFICATION_CHANNELS value {name!r}")
        return value

    @model_validator(mode="after")
    def _dollar_mode_has_conversion(self) -> Settings:
        if (
            self.performance_unit == PerformanceUnit.DOLLARS
            and self.dollars_per_pip_per_qty is None
        ):
            raise ValueError(
                "DOLLARS_PER_PIP_PER_QTY is required when PERFORMANCE_UNIT=dollars"
            )
        from models import TIMEFRAME_MINUTES

        bar_minutes = TIMEFRAME_MINUTES[self.timeframe]
        if self.orb_minutes % bar_minutes != 0:
            raise ValueError("ORB_MINUTES must be a multiple of the bar timeframe")
        return self

    @property
    def trading_sessions(self) -> list[str]:
        return [
            token.strip().lower()
            for token in self.trading_sessions_csv.split(",")
            if token.strip()
        ]

    @property
    def session_specs(self) -> dict[str, str]:
        return {
            "tokyo": self.session_tokyo,
            "london": self.session_london,
            "new_york": self.session_new_york,
        }

    def session_windows(self) -> list[SessionWindow]:
        return build_windows(self.trading_sessions, self.session_specs)

    def session_anchors(self) -> list[SessionAnchor]:
        windows = self.session_windows()
        tokens = [token.strip() for token in self.session_anchors_csv.split(",") if token.strip()]
        if not tokens:
            return [anchor_from_window(window) for window in windows]
        parsed = {anchor.name: anchor for anchor in (parse_anchor_token(token) for token in tokens)}
        anchors: list[SessionAnchor] = []
        for window in windows:
            anchors.append(parsed.get(window.name) or anchor_from_window(window))
        return anchors

    @property
    def notification_channels(self) -> frozenset[str]:
        return frozenset(
            token.strip().upper()
            for token in self.notification_channels_csv.split(",")
            if token.strip()
        )

    def engine_params(self) -> EngineParams:
        from models import TIMEFRAME_MINUTES

        return EngineParams(
            pip_size=self.pip_size,
            sl_mult=self.sl_mult,
            rr=self.rr,
            min_stop_pips=self.min_stop_pips,
            lock_pips=self.lock_pips,
            qty=self.qty,
            qty_ref=self.qty_ref if self.qty_ref is not None else self.qty,
            skip_doji=self.skip_doji,
            timeframe_minutes=TIMEFRAME_MINUTES[self.timeframe],
            orb_minutes=self.orb_minutes,
            entry_delay_minutes=self.entry_delay_minutes,
            anchor_tolerance_minutes=self.anchor_tolerance_minutes,
            intrabar_mode=self.intrabar_mode,
            initial_capital=self.initial_capital,
            point_value=self.point_value,
            performance_unit=self.performance_unit,
            dollars_per_pip_per_qty=self.dollars_per_pip_per_qty,
        )

    def local_candles_path(self, symbol: str, timeframe: Timeframe | str) -> Path:
        tf = timeframe.value if isinstance(timeframe, Timeframe) else timeframe
        return self.data_dir / "candles" / symbol.upper() / f"{tf}.jsonl"

    @property
    def paper_state_path(self) -> Path:
        return self.logs_dir / "paper_state.json"
