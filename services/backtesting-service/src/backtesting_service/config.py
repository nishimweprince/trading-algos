from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from ta_core.settings import resolve_env_file as resolve_env_file_in_workspace

from .anchors import SessionAnchor, anchor_from_window, parse_anchor_token
from .filters import parse_hours
from .models import (
    CostModel,
    EngineParams,
    EntryMode,
    ExecutionMode,
    FirmProfileMode,
    HedgePathMode,
    HedgeTriggerMode,
    IntrabarMode,
    LockMode,
    OcoBufferMode,
    RiskMode,
    StopMode,
    SurvivorExitMode,
    TargetMode,
    TimeExitMode,
    Timeframe,
)
from .sessions import DEFAULT_SESSION_SPECS, SessionWindow, build_windows

KNOWN_CHANNELS = frozenset({"TELEGRAM", "EMAIL", "SMS", "WHATSAPP"})
PLACEHOLDER_PREFIX = "replace-with-"


def resolve_env_file(profile: str | None) -> Path:
    """CWD first, then `services/backtesting-service/` when started from the workspace root."""
    return resolve_env_file_in_workspace(profile, settings_class=Settings)


def load_settings(profile: str | None = None) -> Settings:
    env_file = resolve_env_file(profile)
    if not env_file.is_file():
        raise FileNotFoundError(f"Missing {env_file}. Copy .env.example and configure it.")
    env_parent = env_file.expanduser().resolve().parent
    if env_parent != Path.cwd().resolve():
        os.chdir(env_parent)
    return Settings(_env_file=env_file, _env_file_encoding="utf-8")


class Settings(BaseSettings):
    """Deliberately NOT ta_core.BaseServiceSettings.

    The shared base makes API_KEY required with a 16-character minimum, which is
    right for a service that reaches a broker. This one is a research and
    backtest surface that is routinely run locally with no key at all, and it
    binds 0.0.0.0:8012 rather than 127.0.0.1:8000. Inheriting the base would
    turn "no API_KEY" from a supported mode into a startup failure, so the small
    amount of duplication below is the correct trade.

    The NOTIFICATION_* fields are likewise kept local because this service
    validates channels with its own field_validator; ta_notify.Notifier only
    needs the attributes, which these satisfy structurally.
    """

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
    entry_mode: EntryMode = Field(default=EntryMode.HEDGE_PAIR, validation_alias="ENTRY_MODE")
    lock_pips: float = Field(default=20.0, ge=0, validation_alias="LOCK_PIPS")
    lock_mode: LockMode = Field(default=LockMode.ABSOLUTE, validation_alias="LOCK_MODE")
    lock_r: float = Field(default=0.0, ge=0, validation_alias="LOCK_R")
    be_trigger_r: float = Field(default=0.0, ge=0, validation_alias="BE_TRIGGER_R")
    survivor_exit_mode: SurvivorExitMode = Field(
        default=SurvivorExitMode.LEGACY_LOCK, validation_alias="SURVIVOR_EXIT_MODE"
    )
    survivor_trail_activation_r: float = Field(
        default=1.5, gt=0, validation_alias="SURVIVOR_TRAIL_ACTIVATION_R"
    )
    survivor_trail_gap_r: float = Field(default=1.0, gt=0, validation_alias="SURVIVOR_TRAIL_GAP_R")
    hedge_path_mode: HedgePathMode = Field(
        default=HedgePathMode.LEGACY_PARENT_BAR, validation_alias="HEDGE_PATH_MODE"
    )
    hedge_ratio_initial: float = Field(
        default=0.0, ge=0, le=1, validation_alias="HEDGE_RATIO_INITIAL"
    )
    hedge_trigger_mode: HedgeTriggerMode = Field(
        default=HedgeTriggerMode.FAILURE_ZONE, validation_alias="HEDGE_TRIGGER_MODE"
    )
    hedge_failure_k: float = Field(default=0.5, ge=0, validation_alias="HEDGE_FAILURE_K")
    hedge_ratio_staged: float = Field(
        default=1.0, ge=0, le=1, validation_alias="HEDGE_RATIO_STAGED"
    )
    oco_buffer_mode: OcoBufferMode = Field(
        default=OcoBufferMode.ORB_FRAC, validation_alias="OCO_BUFFER_MODE"
    )
    oco_buffer_value: float = Field(default=0.10, ge=0, validation_alias="OCO_BUFFER_VALUE")
    oco_expiry_bars: int = Field(default=4, gt=0, validation_alias="OCO_EXPIRY_BARS")
    allow_reentry: bool = Field(default=False, validation_alias="ALLOW_REENTRY")
    stop_mode: StopMode = Field(default=StopMode.BAR_RANGE, validation_alias="STOP_MODE")
    sl_mult: float = Field(default=2.0, gt=0, validation_alias="SL_MULT")
    fixed_stop_pips: float = Field(default=0.0, ge=0, validation_alias="FIXED_STOP_PIPS")
    rr: float = Field(default=3.0, gt=0, validation_alias="RR")
    tp_mode: TargetMode = Field(default=TargetMode.FIXED_R, validation_alias="TP_MODE")
    partial_tp_r: float = Field(default=1.0, gt=0, validation_alias="PARTIAL_TP_R")
    partial_fraction: float = Field(default=0.5, gt=0, le=1, validation_alias="PARTIAL_FRACTION")
    min_stop_pips: float = Field(default=0.0, ge=0, validation_alias="MIN_STOP_PIPS")
    min_stop_cost_mult: float = Field(default=0.0, ge=0, validation_alias="MIN_STOP_COST_MULT")
    filter_d1_ema50: bool = Field(default=False, validation_alias="FILTER_D1_EMA50")
    filter_nr7: bool = Field(default=False, validation_alias="FILTER_NR7")
    filter_orb_atr_min: float = Field(default=0.0, ge=0, validation_alias="FILTER_ORB_ATR_MIN")
    filter_orb_atr_max: float = Field(default=0.0, ge=0, validation_alias="FILTER_ORB_ATR_MAX")
    entry_hours_utc_exclude_csv: str = Field(default="", validation_alias="ENTRY_HOURS_UTC_EXCLUDE")
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
    cost_model: CostModel = Field(default=CostModel.PER_SESSION, validation_alias="COST_MODEL")
    spread_pips_per_side: float = Field(default=0.0, ge=0, validation_alias="SPREAD_PIPS_PER_SIDE")
    slippage_pips_per_side: float = Field(
        default=0.0, ge=0, validation_alias="SLIPPAGE_PIPS_PER_SIDE"
    )
    commission_pips_per_side: float = Field(
        default=0.0, ge=0, validation_alias="COMMISSION_PIPS_PER_SIDE"
    )
    swap_long_pips_per_rollover: float = Field(
        default=0.0, ge=0, validation_alias="SWAP_LONG_PIPS_PER_ROLLOVER"
    )
    swap_short_pips_per_rollover: float = Field(
        default=0.0, ge=0, validation_alias="SWAP_SHORT_PIPS_PER_ROLLOVER"
    )
    swap_rollover_time: str = Field(default="17:00", validation_alias="SWAP_ROLLOVER_TIME")
    swap_timezone: str = Field(default="America/New_York", validation_alias="SWAP_TIMEZONE")
    swap_triple_weekday: Literal["monday", "tuesday", "wednesday", "thursday", "friday"] = Field(
        default="wednesday", validation_alias="SWAP_TRIPLE_WEEKDAY"
    )
    session_cost_overrides: dict[str, dict[str, float]] = Field(
        default_factory=dict, validation_alias="SESSION_COST_OVERRIDES"
    )
    breakeven_cost_report: bool = Field(default=True, validation_alias="BREAKEVEN_COST_REPORT")
    risk_mode: RiskMode = Field(default=RiskMode.FIXED_QTY, validation_alias="RISK_MODE")
    risk_pct_per_r: float = Field(default=0.10, gt=0, le=100, validation_alias="RISK_PCT_PER_R")
    max_pair_risk_pct: float = Field(
        default=0.20, gt=0, le=100, validation_alias="MAX_PAIR_RISK_PCT"
    )
    max_open_risk_pct: float = Field(
        default=0.75, ge=0, le=100, validation_alias="MAX_OPEN_RISK_PCT"
    )
    max_concurrent_structures: int = Field(
        default=3, ge=0, validation_alias="MAX_CONCURRENT_STRUCTURES"
    )
    one_open_per_session: bool = Field(default=True, validation_alias="ONE_OPEN_PER_SESSION")
    contract_size: float = Field(default=100.0, gt=0, validation_alias="CONTRACT_SIZE")
    firm_profile: FirmProfileMode = Field(
        default=FirmProfileMode.NONE, validation_alias="FIRM_PROFILE"
    )
    firm_initial_balance: float | None = Field(
        default=None, gt=0, validation_alias="FIRM_INITIAL_BALANCE"
    )
    firm_daily_loss_limit_pct: float = Field(
        default=5.0, gt=0, le=100, validation_alias="FIRM_DAILY_LOSS_LIMIT_PCT"
    )
    firm_total_loss_limit_pct: float = Field(
        default=10.0, gt=0, le=100, validation_alias="FIRM_TOTAL_LOSS_LIMIT_PCT"
    )
    firm_timezone: str = Field(default="America/New_York", validation_alias="FIRM_TIMEZONE")
    firm_daily_reset_time: str = Field(default="00:00", validation_alias="FIRM_DAILY_RESET_TIME")
    firm_breach_action: Literal["block_new"] = Field(
        default="block_new", validation_alias="FIRM_BREACH_ACTION"
    )
    time_exit_mode: TimeExitMode = Field(
        default=TimeExitMode.MAX_AGE, validation_alias="TIME_EXIT_MODE"
    )
    max_age_hours: float = Field(default=24.0, gt=0, validation_alias="MAX_AGE_HOURS")

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
    paper_closed_pair_retention: int = Field(
        default=200, gt=0, validation_alias="PAPER_CLOSED_PAIR_RETENTION"
    )
    paper_event_retention: int = Field(default=500, gt=0, validation_alias="PAPER_EVENT_RETENTION")
    paper_trade_retention: int = Field(default=400, gt=0, validation_alias="PAPER_TRADE_RETENTION")
    paper_bar_retention: int = Field(default=500, gt=0, validation_alias="PAPER_BAR_RETENTION")
    market_execution_mode: ExecutionMode = Field(
        default=ExecutionMode.OFF, validation_alias="MARKET_EXECUTION_MODE"
    )
    execution_account: str = Field(
        default="", validation_alias="EXECUTION_CTRADER_ACCOUNT", max_length=63
    )
    execution_volume_lots: float = Field(
        default=0.01, gt=0, validation_alias="EXECUTION_VOLUME_LOTS"
    )
    execution_source: str = Field(default="session_hedging", validation_alias="EXECUTION_SOURCE")
    execution_timeout_seconds: float = Field(
        default=10.0, gt=0, validation_alias="EXECUTION_TIMEOUT_SECONDS"
    )
    execution_max_consecutive_failures: int = Field(
        default=5, gt=0, validation_alias="EXECUTION_MAX_CONSECUTIVE_FAILURES"
    )
    live_trading_authorized: bool = Field(default=False, validation_alias="LIVE_TRADING_AUTHORIZED")
    trading_enabled: bool = Field(default=False, validation_alias="TRADING_ENABLED")

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
    def _cross_field_rules(self) -> Settings:
        from ta_contracts import TIMEFRAME_MINUTES

        bar_minutes = TIMEFRAME_MINUTES[self.timeframe]
        if self.orb_minutes % bar_minutes != 0:
            raise ValueError("ORB_MINUTES must be a multiple of the bar timeframe")
        if self.stop_mode == StopMode.FIXED_PIPS and self.fixed_stop_pips <= 0:
            raise ValueError("FIXED_STOP_PIPS is required when STOP_MODE=fixed_pips")
        # Validate the complete engine surface at startup as well as on per-request overrides.
        self.engine_params()
        self._validate_execution_surface()
        return self

    def _validate_execution_surface(self) -> None:
        """Reject an execution configuration the gateway would refuse at order time.

        The alias and source patterns are ctrader-markets' own (``OrderTarget.account`` and
        ``OperationBase.source``); failing here turns a 422 on the first live signal into a
        startup error while somebody is still watching.
        """
        if self.market_execution_mode is ExecutionMode.OFF:
            return
        if not self.execution_account:
            raise ValueError(
                "EXECUTION_CTRADER_ACCOUNT is required when MARKET_EXECUTION_MODE is not 'off'"
            )
        if re.fullmatch(r"[a-z][a-z0-9_-]*", self.execution_account) is None:
            raise ValueError(
                "EXECUTION_CTRADER_ACCOUNT must match ^[a-z][a-z0-9_-]*$ (a ctrader-markets alias)"
            )
        if re.fullmatch(r"[a-z][a-z0-9_]{0,30}", self.execution_source) is None:
            raise ValueError(
                "EXECUTION_SOURCE must match ^[a-z][a-z0-9_]*$ and be 31 characters or fewer"
            )
        if not self.ctrader_markets_url:
            raise ValueError("CTRADER_MARKETS_URL is required to execute orders")
        if self.market_execution_mode is ExecutionMode.LIVE and self.ctrader_api_key is None:
            raise ValueError("CTRADER_API_KEY is required when MARKET_EXECUTION_MODE=live")

    @property
    def trading_sessions(self) -> list[str]:
        return [
            token.strip().lower() for token in self.trading_sessions_csv.split(",") if token.strip()
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
        """Engine parameters from the environment.

        The reporting unit is deliberately absent: pips versus dollars is a client choice,
        sent per request. A cash rate is still supplied when the configuration itself needs
        one — fixed-fractional sizing and a custom firm profile size in account currency —
        and the client's own rate replaces it on any request that sends one.
        """
        from ta_contracts import TIMEFRAME_MINUTES

        from .models import DEFAULT_DOLLARS_PER_PIP_PER_QTY

        needs_cash = (
            self.risk_mode is RiskMode.FIXED_FRACTIONAL
            or self.firm_profile is FirmProfileMode.CUSTOM
        )
        return EngineParams(
            dollars_per_pip_per_qty=(DEFAULT_DOLLARS_PER_PIP_PER_QTY if needs_cash else None),
            pip_size=self.pip_size,
            entry_mode=self.entry_mode,
            stop_mode=self.stop_mode,
            sl_mult=self.sl_mult,
            fixed_stop_pips=self.fixed_stop_pips,
            rr=self.rr,
            tp_mode=self.tp_mode,
            partial_tp_r=self.partial_tp_r,
            partial_fraction=self.partial_fraction,
            min_stop_pips=self.min_stop_pips,
            min_stop_cost_mult=self.min_stop_cost_mult,
            filter_d1_ema50=self.filter_d1_ema50,
            filter_nr7=self.filter_nr7,
            filter_orb_atr_min=self.filter_orb_atr_min,
            filter_orb_atr_max=self.filter_orb_atr_max,
            entry_hours_utc_exclude=sorted(parse_hours(self.entry_hours_utc_exclude_csv)),
            lock_pips=self.lock_pips,
            lock_mode=self.lock_mode,
            lock_r=self.lock_r,
            be_trigger_r=self.be_trigger_r,
            survivor_exit_mode=self.survivor_exit_mode,
            survivor_trail_activation_r=self.survivor_trail_activation_r,
            survivor_trail_gap_r=self.survivor_trail_gap_r,
            hedge_path_mode=self.hedge_path_mode,
            hedge_ratio_initial=self.hedge_ratio_initial,
            hedge_trigger_mode=self.hedge_trigger_mode,
            hedge_failure_k=self.hedge_failure_k,
            hedge_ratio_staged=self.hedge_ratio_staged,
            oco_buffer_mode=self.oco_buffer_mode,
            oco_buffer_value=self.oco_buffer_value,
            oco_expiry_bars=self.oco_expiry_bars,
            allow_reentry=self.allow_reentry,
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
            cost_model=self.cost_model,
            spread_pips_per_side=self.spread_pips_per_side,
            slippage_pips_per_side=self.slippage_pips_per_side,
            commission_pips_per_side=self.commission_pips_per_side,
            swap_long_pips_per_rollover=self.swap_long_pips_per_rollover,
            swap_short_pips_per_rollover=self.swap_short_pips_per_rollover,
            swap_rollover_time=self.swap_rollover_time,
            swap_timezone=self.swap_timezone,
            swap_triple_weekday=self.swap_triple_weekday,
            session_cost_overrides=self.session_cost_overrides,
            breakeven_cost_report=self.breakeven_cost_report,
            risk_mode=self.risk_mode,
            risk_pct_per_r=self.risk_pct_per_r,
            max_pair_risk_pct=self.max_pair_risk_pct,
            max_open_risk_pct=self.max_open_risk_pct,
            max_concurrent_structures=self.max_concurrent_structures,
            one_open_per_session=self.one_open_per_session,
            contract_size=self.contract_size,
            firm_profile=self.firm_profile,
            firm_initial_balance=(
                self.firm_initial_balance
                if self.firm_initial_balance is not None
                else self.initial_capital
            ),
            firm_daily_loss_limit_pct=self.firm_daily_loss_limit_pct,
            firm_total_loss_limit_pct=self.firm_total_loss_limit_pct,
            firm_timezone=self.firm_timezone,
            firm_daily_reset_time=self.firm_daily_reset_time,
            firm_breach_action=self.firm_breach_action,
            time_exit_mode=self.time_exit_mode,
            max_age_hours=self.max_age_hours,
        )

    def local_candles_path(self, symbol: str, timeframe: Timeframe | str) -> Path:
        tf = timeframe.value if isinstance(timeframe, Timeframe) else timeframe
        return self.data_dir / "candles" / symbol.upper() / f"{tf}.jsonl"

    @property
    def paper_state_path(self) -> Path:
        return self.logs_dir / "paper_state.json"
