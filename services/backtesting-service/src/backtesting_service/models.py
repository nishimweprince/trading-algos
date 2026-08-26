"""Candle contract matches ctrader-markets (interval-end ``ts``, closed bars)."""

from __future__ import annotations

import re
from datetime import datetime, time
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ta_contracts import Candle, Timeframe


class PerformanceUnit(StrEnum):
    PIPS = "pips"
    DOLLARS = "dollars"


class IntrabarMode(StrEnum):
    OPTIMISTIC = "optimistic"
    PESSIMISTIC = "pessimistic"
    M1 = "m1"
    M1_CONSERVATIVE = "m1_conservative"
    TICK = "tick"


class EntryMode(StrEnum):
    HEDGE_PAIR = "hedge_pair"
    SYNTHETIC_BREAKOUT = "synthetic_breakout"
    CONTINGENT_HEDGE = "contingent_hedge"
    OCO_BRACKET = "oco_bracket"


class TargetMode(StrEnum):
    FIXED_R = "fixed_r"
    PARTIAL_TRAIL = "partial_trail"


class LockMode(StrEnum):
    ABSOLUTE = "absolute"
    NONE = "none"
    BREAKEVEN = "breakeven"
    R_RELATIVE = "r_relative"


class SurvivorExitMode(StrEnum):
    """How a simultaneous hedge-pair survivor is managed after the first stop."""

    LEGACY_LOCK = "legacy_lock"
    UNLOCKED = "unlocked"
    MFE_TRAIL = "mfe_trail"


class HedgePathMode(StrEnum):
    """Resolver generation for simultaneous hedge-pair exits."""

    LEGACY_PARENT_BAR = "legacy_parent_bar"
    CHRONOLOGICAL_V2 = "chronological_v2"


class HedgeTriggerMode(StrEnum):
    FAILURE_ZONE = "failure_zone"


class OcoBufferMode(StrEnum):
    ORB_FRAC = "orb_frac"
    FIXED_PIPS = "fixed_pips"


class StopMode(StrEnum):
    """How ``S`` (one R) is sized.

    ``bar_range`` scales with the measured opening range over ``ORB_MINUTES``. ``fixed_pips``
    pins ``S`` to ``FIXED_STOP_PIPS`` regardless of the range, so R is constant across sessions.
    ``atr14`` uses Wilder ATR(14) of completed parent bars. ``orb_atr14_blend`` is the frozen
    50/50 mix of that opening range and ATR14. ``SL_MULT`` still scales the estimator.
    """

    BAR_RANGE = "bar_range"
    FIXED_PIPS = "fixed_pips"
    ATR14 = "atr14"
    ORB_ATR14_BLEND = "orb_atr14_blend"


class ExecutionMode(StrEnum):
    """How far a staged structure travels toward a real broker.

    ``off`` builds no bridge at all. ``shadow`` computes and records the exact order
    payload without contacting the broker, so payloads and the live view can be verified
    against real sessions at zero risk. ``live`` submits. Default is ``off``: a service
    that has never been deliberately configured must not be able to trade.
    """

    OFF = "off"
    SHADOW = "shadow"
    LIVE = "live"

    @property
    def sends_orders(self) -> bool:
        return self is ExecutionMode.LIVE

    @property
    def builds_payloads(self) -> bool:
        return self is not ExecutionMode.OFF


class CostModel(StrEnum):
    NONE = "none"
    PER_SESSION = "per_session"


class RiskMode(StrEnum):
    FIXED_QTY = "fixed_qty"
    FIXED_FRACTIONAL = "fixed_fractional"


class FirmProfileMode(StrEnum):
    NONE = "none"
    CUSTOM = "custom"


class TimeExitMode(StrEnum):
    NONE = "none"
    MAX_AGE = "max_age"


TIMEFRAME_MINUTES: dict[Timeframe, int] = {
    Timeframe.M1: 1,
    Timeframe.M2: 2,
    Timeframe.M3: 3,
    Timeframe.M4: 4,
    Timeframe.M5: 5,
    Timeframe.M10: 10,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.H4: 240,
    Timeframe.H12: 720,
    Timeframe.D1: 1440,
    Timeframe.W1: 10080,
}


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return value


class CandlesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    candles: list[Candle]
    source: Literal["local", "ctrader"] = "ctrader"


class EngineParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pip_size: float = Field(default=0.1, gt=0)
    entry_mode: EntryMode = EntryMode.HEDGE_PAIR
    stop_mode: StopMode = StopMode.BAR_RANGE
    sl_mult: float = Field(default=2.0, gt=0)
    fixed_stop_pips: float = Field(default=0.0, ge=0)
    rr: float = Field(default=3.0, gt=0)
    tp_mode: TargetMode = TargetMode.FIXED_R
    partial_tp_r: float = Field(default=1.0, gt=0)
    partial_fraction: float = Field(default=0.5, gt=0, le=1)
    min_stop_pips: float = Field(default=0.0, ge=0)
    min_stop_cost_mult: float = Field(default=0.0, ge=0)
    filter_d1_ema50: bool = False
    filter_nr7: bool = False
    filter_orb_atr_min: float = Field(default=0.0, ge=0)
    filter_orb_atr_max: float = Field(default=0.0, ge=0)
    entry_hours_utc_exclude: list[int] = Field(default_factory=list)
    lock_pips: float = Field(default=20.0, ge=0)
    lock_mode: LockMode = LockMode.ABSOLUTE
    lock_r: float = Field(default=0.0, ge=0)
    be_trigger_r: float = Field(default=0.0, ge=0)
    survivor_exit_mode: SurvivorExitMode = SurvivorExitMode.LEGACY_LOCK
    survivor_trail_activation_r: float = Field(default=1.5, gt=0)
    survivor_trail_gap_r: float = Field(default=1.0, gt=0)
    hedge_path_mode: HedgePathMode = HedgePathMode.LEGACY_PARENT_BAR
    hedge_ratio_initial: float = Field(default=0.0, ge=0, le=1)
    hedge_trigger_mode: HedgeTriggerMode = HedgeTriggerMode.FAILURE_ZONE
    hedge_failure_k: float = Field(default=0.5, ge=0)
    hedge_ratio_staged: float = Field(default=1.0, ge=0, le=1)
    oco_buffer_mode: OcoBufferMode = OcoBufferMode.ORB_FRAC
    oco_buffer_value: float = Field(default=0.10, ge=0)
    oco_expiry_bars: int = Field(default=4, gt=0)
    allow_reentry: bool = False
    qty: float = Field(default=1.0, gt=0)
    skip_doji: bool = True
    timeframe_minutes: int = Field(default=15, gt=0)
    orb_minutes: int = Field(default=60, gt=0)
    entry_delay_minutes: int = Field(default=15, ge=0)
    anchor_tolerance_minutes: int = Field(default=15, ge=0)
    intrabar_mode: IntrabarMode = IntrabarMode.M1_CONSERVATIVE
    initial_capital: float = Field(default=100_000.0, gt=0)
    point_value: float = Field(default=1.0, gt=0)
    performance_unit: PerformanceUnit = PerformanceUnit.PIPS
    dollars_per_pip_per_qty: float | None = Field(default=None, gt=0)
    qty_ref: float = Field(default=1.0, gt=0)
    cost_model: CostModel = CostModel.PER_SESSION
    spread_pips_per_side: float = Field(default=0.0, ge=0)
    slippage_pips_per_side: float = Field(default=0.0, ge=0)
    commission_pips_per_side: float = Field(default=0.0, ge=0)
    swap_long_pips_per_rollover: float = Field(default=0.0, ge=0)
    swap_short_pips_per_rollover: float = Field(default=0.0, ge=0)
    swap_rollover_time: str = "17:00"
    swap_timezone: str = "America/New_York"
    swap_triple_weekday: Literal["monday", "tuesday", "wednesday", "thursday", "friday"] = (
        "wednesday"
    )
    session_cost_overrides: dict[str, dict[str, float]] = Field(default_factory=dict)
    breakeven_cost_report: bool = True
    risk_mode: RiskMode = RiskMode.FIXED_QTY
    risk_pct_per_r: float = Field(default=0.10, gt=0, le=100)
    max_pair_risk_pct: float = Field(default=0.20, gt=0, le=100)
    max_open_risk_pct: float = Field(default=0.75, ge=0, le=100)
    max_concurrent_structures: int = Field(default=3, ge=0)
    one_open_per_session: bool = True
    contract_size: float = Field(default=100.0, gt=0)
    firm_profile: FirmProfileMode = FirmProfileMode.NONE
    firm_initial_balance: float | None = Field(default=None, gt=0)
    firm_daily_loss_limit_pct: float = Field(default=5.0, gt=0, le=100)
    firm_total_loss_limit_pct: float = Field(default=10.0, gt=0, le=100)
    firm_timezone: str = "America/New_York"
    firm_daily_reset_time: str = "00:00"
    firm_breach_action: Literal["block_new"] = "block_new"
    time_exit_mode: TimeExitMode = TimeExitMode.MAX_AGE
    max_age_hours: float = Field(default=24.0, gt=0)

    @model_validator(mode="after")
    def _orb_multiple_of_bar(self) -> EngineParams:
        if self.orb_minutes % self.timeframe_minutes != 0:
            raise ValueError("ORB_MINUTES must be a multiple of the bar timeframe")
        return self

    @model_validator(mode="after")
    def _fixed_stop_has_distance(self) -> EngineParams:
        if self.stop_mode == StopMode.FIXED_PIPS and self.fixed_stop_pips <= 0:
            raise ValueError("FIXED_STOP_PIPS must be greater than 0 when STOP_MODE=fixed_pips")
        return self

    @model_validator(mode="after")
    def _r_relative_lock_has_distance(self) -> EngineParams:
        if self.lock_mode == LockMode.R_RELATIVE and self.lock_r <= 0:
            raise ValueError("LOCK_R must be greater than 0 when LOCK_MODE=r_relative")
        return self

    @model_validator(mode="after")
    def _valid_entry_hours(self) -> EngineParams:
        for hour in self.entry_hours_utc_exclude:
            if not 0 <= hour <= 23:
                raise ValueError(f"ENTRY_HOURS_UTC_EXCLUDE must be 0-23, got {hour}")
        return self

    @model_validator(mode="after")
    def _be_ratchet_has_lock(self) -> EngineParams:
        """A ratchet with no lock distance would park the stop exactly on entry.

        ``LOCK_MODE=breakeven`` is the explicit way to ask for that; every other mode
        must supply a positive distance so the armed stop clears the entry price.
        """
        if self.be_trigger_r <= 0:
            return self
        if self.lock_mode is LockMode.NONE:
            raise ValueError("BE_TRIGGER_R requires LOCK_MODE other than 'none'")
        if self.lock_mode is LockMode.ABSOLUTE and self.lock_pips <= 0:
            raise ValueError("BE_TRIGGER_R with LOCK_MODE=absolute requires LOCK_PIPS > 0")
        if self.lock_mode is LockMode.R_RELATIVE and self.lock_r <= 0:
            raise ValueError("BE_TRIGGER_R with LOCK_MODE=r_relative requires LOCK_R > 0")
        return self

    @model_validator(mode="after")
    def _valid_survivor_trail(self) -> EngineParams:
        if (
            self.survivor_exit_mode is SurvivorExitMode.MFE_TRAIL
            and self.survivor_trail_activation_r < 1.0
        ):
            raise ValueError("SURVIVOR_TRAIL_ACTIVATION_R must be at least 1R")
        return self

    @model_validator(mode="after")
    def _valid_cost_surface(self) -> EngineParams:
        from .costs import COST_SESSION_NAMES, NUMERIC_COST_FIELDS

        if re.fullmatch(r"\d{2}:\d{2}", self.swap_rollover_time) is None:
            raise ValueError("SWAP_ROLLOVER_TIME must be HH:MM")
        try:
            time.fromisoformat(self.swap_rollover_time)
        except ValueError as exc:
            raise ValueError("SWAP_ROLLOVER_TIME must be HH:MM") from exc
        try:
            ZoneInfo(self.swap_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("SWAP_TIMEZONE must be a valid IANA timezone") from exc
        for session, override in self.session_cost_overrides.items():
            if not session.strip():
                raise ValueError("SESSION_COST_OVERRIDES session names must not be blank")
            if session not in COST_SESSION_NAMES:
                raise ValueError(f"SESSION_COST_OVERRIDES contains unknown session: {session}")
            unknown = set(override) - NUMERIC_COST_FIELDS
            if unknown:
                raise ValueError(
                    "SESSION_COST_OVERRIDES contains unknown keys: " + ", ".join(sorted(unknown))
                )
            if any(value < 0 for value in override.values()):
                raise ValueError("SESSION_COST_OVERRIDES values must be non-negative")
        return self

    @model_validator(mode="after")
    def _valid_risk_surface(self) -> EngineParams:
        if self.risk_mode is RiskMode.FIXED_FRACTIONAL and self.dollars_per_pip_per_qty is None:
            raise ValueError("DOLLARS_PER_PIP_PER_QTY is required when RISK_MODE=fixed_fractional")
        return self

    @model_validator(mode="after")
    def _valid_hedge_ratios(self) -> EngineParams:
        if self.hedge_ratio_staged < self.hedge_ratio_initial:
            raise ValueError("HEDGE_RATIO_STAGED must be at least HEDGE_RATIO_INITIAL")
        return self

    @model_validator(mode="after")
    def _valid_firm_profile(self) -> EngineParams:
        if re.fullmatch(r"\d{2}:\d{2}", self.firm_daily_reset_time) is None:
            raise ValueError("FIRM_DAILY_RESET_TIME must be HH:MM")
        try:
            time.fromisoformat(self.firm_daily_reset_time)
        except ValueError as exc:
            raise ValueError("FIRM_DAILY_RESET_TIME must be HH:MM") from exc
        try:
            ZoneInfo(self.firm_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("FIRM_TIMEZONE must be a valid IANA timezone") from exc
        if self.firm_profile is FirmProfileMode.CUSTOM and self.dollars_per_pip_per_qty is None:
            raise ValueError("DOLLARS_PER_PIP_PER_QTY is required when FIRM_PROFILE=custom")
        return self


class ClosedLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: str
    side: Literal["long", "short"]
    entry: float
    exit: float
    pnl: float
    bucket: Literal["win", "be", "loss"]
    ts: datetime
    reason: str
    pair_id: str | None = None
    role: Literal["primary", "hedge", "unknown"] = "unknown"
    entry_ts: datetime | None = None
    pnl_pips: float | None = None
    pnl_dollars: float | None = None
    mae_pips: float | None = None
    mfe_pips: float | None = None
    mae_dollars: float | None = None
    mfe_dollars: float | None = None
    gross_pnl_pips: float | None = None
    cost_pips: float = 0.0
    net_pnl_pips: float | None = None
    qty: float = 1.0
    episode: int = 0
    entry_fills: int = 1
    execution_cost_pips: float = 0.0
    financing_cost_pips: float = 0.0
    reentry_index: int = 0
    gap_fill: bool = False


class TradePairLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    side: Literal["long", "short"]
    role: Literal["primary", "hedge", "unknown"]
    status: Literal["open", "closed"]
    exit: float | None = None
    exit_ts: datetime | None = None
    pnl_pips: float
    pnl_dollars: float | None = None
    mae_pips: float = 0.0
    mfe_pips: float = 0.0
    mae_dollars: float | None = None
    mfe_dollars: float | None = None
    bucket: Literal["win", "be", "loss"] | None = None
    reason: str | None = None
    gross_pnl_pips: float | None = None
    cost_pips: float = 0.0
    net_pnl_pips: float | None = None
    qty: float = 1.0


class TradePairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session: str
    entry: float
    entry_ts: datetime
    qty: float = 1.0
    initial_risk_pct: float | None = None
    initial_risk_cash: float | None = None
    status: Literal["open", "partial", "closed"]
    primary: TradePairLeg | None = None
    hedge: TradePairLeg | None = None
    unknown_legs: list[TradePairLeg] = Field(default_factory=list)
    pnl_pips: float
    pnl_dollars: float | None = None
    gross_pnl_pips: float | None = None
    cost_pips: float = 0.0
    net_pnl_pips: float | None = None
    entry_mode: EntryMode = EntryMode.HEDGE_PAIR
    reentry_index: int = 0
    entry_gap: bool = False
    exit_gap: bool = False
    same_bar_resolved: bool = False
    stop_pips: float | None = None
    gross_r: float | None = None
    cost_r: float | None = None
    net_r: float | None = None
    hold_hours: float | None = None
    weekday: str | None = None
    first_stop_ts: datetime | None = None
    survivor_side: Literal["long", "short"] | None = None
    survivor_post_failure_mae_pips: float | None = None
    survivor_post_failure_mfe_pips: float | None = None
    survivor_post_failure_mae_r: float | None = None
    survivor_post_failure_mfe_r: float | None = None
    survivor_peak_giveback_pips: float | None = None
    survivor_peak_giveback_r: float | None = None
    survivor_ratchet_armed_ts: datetime | None = None
    survivor_ratchet_advances: int = 0
    survivor_exit_efficiency: float | None = None


class OpenPairView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session: str
    entry: float
    sl_dist: float
    long_open: bool
    short_open: bool
    locked: bool
    long_sl: float
    long_tp: float
    short_sl: float
    short_tp: float
    entry_ts: datetime
    qty: float = 1.0
    initial_risk_pct: float | None = None
    initial_risk_cash: float | None = None


class OpenEntryOrderView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session: str
    entry_mode: EntryMode
    reference_entry: float
    sl_dist: float
    upper_trigger: float
    lower_trigger: float
    staged_ts: datetime
    qty: float
    expiry_bars: int | None = None
    bars_seen: int = 0
    reentry_index: int = 0


class EngineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "signal",
        "entry",
        "entry_order_staged",
        "entry_order_cancelled",
        "hedge_staged",
        "lock",
        "be_ratchet_armed",
        "survivor_activated",
        "survivor_ratchet_armed",
        "survivor_ratchet_advanced",
        "resolver_fallback",
        "partial_tp",
        "exit",
        "signal_skipped_anchor_drift",
        "signal_skipped_filter",
        "signal_skipped_non_positive_stop",
        "bar_skipped_invalid",
        "signal_suppressed_risk",
        "prop_guard_breached",
    ]
    session: str
    ts: datetime
    detail: dict[str, object] = Field(default_factory=dict)


class OutcomeMix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tp: float = 0.0
    lock: float = 0.0
    breakeven: float = 0.0
    whipsaw: float = 0.0
    time_exit: float = 0.0


class SessionAnchorStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: str
    skip_count: int = 0
    signal_count: int = 0
    anchor_drift_p50: float | None = None
    anchor_drift_max: float | None = None
    anchor_drift_minutes: list[float] = Field(default_factory=list)
    same_bar_resolution_rate: float = 0.0
    same_bar_r: float = 0.0


class Stats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    realized: float = 0.0
    realized_pips: float = 0.0
    long_wins: int = 0
    long_be: int = 0
    long_loss: int = 0
    short_wins: int = 0
    short_be: int = 0
    short_loss: int = 0
    locks: int = 0


DEFAULT_DOLLARS_PER_PIP_PER_QTY = 10.0


class PerformanceView(BaseModel):
    """Every additive metric of a run, expressed once in the selected unit.

    The engine computes in pips because pips are the invariant the price data supports.
    This view is the presentation layer: `unit` names what the numbers are, and
    `conversion_factor` is the single multiplier applied to every one of them. R-denominated
    metrics are absent on purpose — R is a ratio and does not convert.
    """

    model_config = ConfigDict(extra="forbid")

    unit: PerformanceUnit
    dollars_per_pip_per_qty: float | None
    qty_ref: float
    conversion_factor: float
    unit_label: str
    realized: float
    unrealized: float
    equity: float
    gross_realized: float
    realized_cost: float
    net_realized: float
    gross_unrealized: float
    unrealized_cost: float
    net_unrealized: float
    gross_equity: float
    equity_cost: float
    net_equity: float
    execution_cost: float
    financing_cost: float
    max_drawdown: float
    gross_max_drawdown: float
    net_max_drawdown: float
    breakeven_per_completed_side: float | None
    configured_spread_per_side: float
    configured_execution_cost_per_side: float


class ComparisonPerformanceView(BaseModel):
    """One comparison row's additive metrics in the selected unit."""

    model_config = ConfigDict(extra="forbid")

    unit: PerformanceUnit
    dollars_per_pip_per_qty: float | None
    conversion_factor: float
    unit_label: str
    gross: float
    net: float
    execution_cost: float
    financing_cost: float
    total_cost: float
    gross_expectancy: float | None
    net_expectancy: float | None
    gross_max_drawdown: float
    net_max_drawdown: float
    breakeven_per_completed_side: float | None


class BacktestReportHeader(BaseModel):
    """Auditable configuration and data-quality context for every report surface."""

    model_config = ConfigDict(extra="forbid")

    entry_mode: EntryMode
    session_anchors: list[str] = Field(default_factory=list)
    stop_mode: StopMode
    tp_mode: TargetMode
    rr: float
    partial_tp_r: float = 1.0
    partial_fraction: float = 0.5
    lock_mode: LockMode
    lock_pips: float
    lock_r: float = 0.0
    be_trigger_r: float = 0.0
    survivor_exit_mode: SurvivorExitMode = SurvivorExitMode.LEGACY_LOCK
    survivor_trail_activation_r: float = 1.5
    survivor_trail_gap_r: float = 1.0
    hedge_path_mode: HedgePathMode = HedgePathMode.LEGACY_PARENT_BAR
    min_stop_pips: float = 0.0
    min_stop_cost_mult: float = 0.0
    derived_min_stop_pips: float | None = None
    filter_d1_ema50: bool = False
    filter_nr7: bool = False
    filter_orb_atr_min: float = 0.0
    filter_orb_atr_max: float = 0.0
    entry_hours_utc_exclude: list[int] = Field(default_factory=list)
    time_exit_mode: TimeExitMode
    max_age_hours: float
    risk_mode: RiskMode
    cost_model: CostModel
    costs_are_zero: bool = False
    intrabar_mode: IntrabarMode
    resolver_tier: int
    qty_ref: float
    firm_profile: FirmProfileMode
    firm_profile_name: str = "none"
    firm_profile_version: str | None = None
    first_bar_ts: datetime | None = None
    last_bar_ts: datetime | None = None
    warmup_bars: int = 0
    validation_summary: dict[str, int] = Field(default_factory=dict)
    m1_bars_loaded: int = 0
    m1_resolver_calls: int = 0
    m1_covered_resolver_calls: int = 0
    m1_partial_coverage_count: int = 0
    m1_fallback_count: int = 0


class EquityCurvePoint(BaseModel):
    """One marked net-equity observation in the report's selected unit."""

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    net_equity: float
    net_drawdown: float


class BacktestReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    source: Literal["local", "ctrader"]
    bar_count: int
    performance_unit: PerformanceUnit
    entry_mode: EntryMode = EntryMode.HEDGE_PAIR
    orb_minutes: int
    entry_delay_minutes: int
    anchor_tolerance_minutes: int
    stop_mode: StopMode = StopMode.BAR_RANGE
    fixed_stop_pips: float = 0.0
    realized: float
    unrealized: float
    equity: float
    realized_pips: float
    unrealized_pips: float
    realized_r: float
    unrealized_r: float
    equity_pips: float
    max_drawdown_pips: float
    max_drawdown_r: float
    gross_max_drawdown_pips: float
    net_max_drawdown_pips: float
    gross_max_drawdown_r: float
    net_max_drawdown_r: float
    gross_realized_pips: float
    realized_cost_pips: float
    net_realized_pips: float
    gross_unrealized_pips: float
    unrealized_cost_pips: float
    net_unrealized_pips: float
    gross_equity_pips: float
    equity_cost_pips: float
    net_equity_pips: float
    gross_realized_r: float
    realized_cost_r: float
    net_realized_r: float
    gross_unrealized_r: float
    unrealized_cost_r: float
    net_unrealized_r: float
    gross_equity_r: float
    equity_cost_r: float
    net_equity_r: float
    execution_cost_pips: float
    financing_cost_pips: float
    transaction_sides: int
    completed_transaction_sides: int
    cost_side_equivalents: float
    completed_cost_side_equivalents: float
    breakeven_pips_per_side: float | None
    configured_spread_pips_per_side: float
    configured_execution_cost_pips_per_side: float
    cost_headroom_ratio: float | None
    risk_mode: RiskMode
    suppressed_signal_count: int
    suppressed_signal_reasons: dict[str, int] = Field(default_factory=dict)
    trades_skipped_by_filter: int = 0
    non_positive_stop_count: int = 0
    firm_profile: FirmProfileMode
    firm_profile_name: str = "none"
    firm_profile_version: str | None = None
    prop_guard_breached: bool
    prop_guard_breach_reason: str | None
    prop_guard_breached_at: datetime | None
    prop_guard_daily_reference_equity: float | None
    prop_guard_last_equity_cash: float | None
    time_exit_mode: TimeExitMode
    max_age_hours: float
    realized_dollars: float | None
    unrealized_dollars: float | None
    equity_dollars: float | None
    max_drawdown_dollars: float | None
    long_wins: int
    long_be: int
    long_loss: int
    short_wins: int
    short_be: int
    short_loss: int
    locks: int
    open_pairs: int
    pending_entry_orders: int = 0
    unresolved_structures: int = 0
    session_anchor_stats: list[SessionAnchorStats] = Field(default_factory=list)
    same_bar_resolution_rate: float = 0.0
    same_bar_r: float = 0.0
    survivor_tp_rate: float | None = None
    mean_loss_r: float | None = None
    breakeven_tp_rate_required: float | None = None
    tp_rate_margin_pp: float | None = None
    tp_rate_margin_pp_ci_low: float | None = None
    tp_rate_margin_pp_ci_high: float | None = None
    outcome_mix: OutcomeMix = Field(default_factory=OutcomeMix)
    performance: PerformanceView
    report_header: BacktestReportHeader
    effective_settings: dict[str, object] = Field(default_factory=dict)
    candle_set_sha256: str | None = None
    max_concurrent_structures: int = 0
    median_concurrent: float | None = None
    win_rate: float | None = None
    win_rate_excl_be: float | None = None
    median_hold_hours: float | None = None
    p95_hold_hours: float | None = None
    equity_curve: list[EquityCurvePoint] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    trades: list[ClosedLeg]
    trade_pairs: list[TradePairResult]
    events: list[EngineEvent]


class EntryModeComparisonRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_mode: EntryMode
    completed_structures: int
    gross_pips: float
    net_pips: float
    gross_r: float
    net_r: float
    execution_cost_pips: float
    financing_cost_pips: float
    total_cost_pips: float
    gross_expectancy_pips: float | None
    net_expectancy_pips: float | None
    gross_expectancy_r: float | None
    net_expectancy_r: float | None
    gross_profit_factor: float | None
    net_profit_factor: float | None
    gross_win_rate: float | None
    net_win_rate: float | None
    gross_win_rate_excl_be: float | None
    net_win_rate_excl_be: float | None
    survivor_tp_rate: float | None
    breakeven_tp_rate_required: float | None
    gross_max_drawdown_pips: float
    net_max_drawdown_pips: float
    gross_max_drawdown_r: float
    net_max_drawdown_r: float
    breakeven_pips_per_completed_side: float | None
    transaction_sides: int
    cost_side_equivalents: float
    entry_fill_sides: int
    exit_fill_sides: int
    cancelled_entry_orders: int
    expired_entry_orders: int
    median_hold_hours: float | None
    p95_hold_hours: float | None
    max_concurrent_structures: int
    suppressed_signals: int
    unresolved_structures: int
    prop_guard_breached: bool
    prop_guard_breach_reason: str | None
    prop_guard_breached_at: datetime | None
    prop_guard_breach_events: int
    performance: ComparisonPerformanceView


class HedgeSyntheticAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    basis: Literal["hedge_pair_minus_synthetic_breakout"] = "hedge_pair_minus_synthetic_breakout"
    gross_difference_pips: float
    gap_effect_pips: float
    same_bar_effect_pips: float
    gross_payoff_effect_pips: float
    execution_cost_difference_pips: float
    financing_cost_difference_pips: float
    total_cost_difference_pips: float
    net_difference_pips: float
    reconciliation_error_pips: float
    gross_difference_r: float
    gap_effect_r: float
    same_bar_effect_r: float
    gross_payoff_effect_r: float
    total_cost_difference_r: float
    net_difference_r: float
    reconciliation_error_r: float
    hedge_gap_tagged_structures: int
    synthetic_gap_tagged_structures: int
    hedge_same_bar_tagged_structures: int
    synthetic_same_bar_tagged_structures: int
    hedge_entry_fill_sides: int
    hedge_exit_fill_sides: int
    synthetic_entry_fill_sides: int
    synthetic_exit_fill_sides: int


class EntryModeComparisonReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    source: Literal["local", "ctrader"]
    bar_count: int
    first_bar_ts: datetime
    last_bar_ts: datetime
    candle_set_sha256: str
    shared_params: dict[str, object]
    rows: list[EntryModeComparisonRow]
    hedge_vs_synthetic: HedgeSyntheticAttribution


class HoldBucketAttribution(BaseModel):
    """One fixed, non-overlapping holding-horizon bucket of completed structures."""

    model_config = ConfigDict(extra="forbid")

    label: str
    lower_hours: float
    upper_hours: float | None
    lower_inclusive: bool
    upper_inclusive: bool
    structures: int
    gross_pips: float
    net_pips: float
    gross_r: float
    net_r: float


class M1CoverageReport(BaseModel):
    """Whether covering M1 data existed, and the fallback used when it did not."""

    model_config = ConfigDict(extra="forbid")

    intrabar_mode: IntrabarMode
    status: Literal["absent", "partial", "complete"]
    m1_bars_loaded: int
    m1_first_bar_ts: datetime | None
    m1_last_bar_ts: datetime | None
    covered_parent_bars: int
    total_parent_bars: int
    covered_parent_fraction: float
    subpath_used: bool
    subpath_fallback: str | None
    fallback_description: str


class ScaleSweepCell(BaseModel):
    """One validated S8 grid cell: paired gross/net metrics plus hold attribution."""

    model_config = ConfigDict(extra="forbid")

    cell_index: int
    entry_mode: EntryMode
    orb_minutes: int
    entry_delay_minutes: int
    max_age_hours: float
    time_exit_mode: TimeExitMode
    completed_structures: int
    gross_pips: float
    net_pips: float
    gross_r: float
    net_r: float
    completed_gross_pips: float
    completed_net_pips: float
    completed_gross_r: float
    completed_net_r: float
    execution_cost_pips: float
    financing_cost_pips: float
    total_cost_pips: float
    gross_expectancy_pips: float | None
    net_expectancy_pips: float | None
    gross_expectancy_r: float | None
    net_expectancy_r: float | None
    gross_profit_factor: float | None
    net_profit_factor: float | None
    gross_win_rate: float | None = None
    net_win_rate: float | None = None
    gross_win_rate_excl_be: float | None
    net_win_rate_excl_be: float | None
    survivor_tp_rate: float | None
    breakeven_tp_rate_required: float | None
    tp_rate_margin_pp: float | None
    tp_rate_margin_pp_ci_low: float | None
    tp_rate_margin_pp_ci_high: float | None
    gross_max_drawdown_pips: float
    net_max_drawdown_pips: float
    gross_max_drawdown_r: float
    net_max_drawdown_r: float
    breakeven_pips_per_completed_side: float | None
    transaction_sides: int
    cost_side_equivalents: float
    entry_fill_sides: int
    exit_fill_sides: int
    cancelled_entry_orders: int
    expired_entry_orders: int
    median_hold_hours: float | None
    p95_hold_hours: float | None
    max_concurrent_structures: int
    suppressed_signals: int
    unresolved_structures: int
    prop_guard_breached: bool
    prop_guard_breach_reason: str | None
    prop_guard_breached_at: datetime | None
    prop_guard_breach_events: int
    hold_buckets: list[HoldBucketAttribution]
    unbucketed_structures: int


class ScaleSweepReport(BaseModel):
    """The complete S8 surface: every cell of the §8.1 grid on one candle set."""

    model_config = ConfigDict(extra="forbid")

    study: Literal["s8_scale_decomposition"] = "s8_scale_decomposition"
    symbol: str
    timeframe: Timeframe
    source: Literal["local", "ctrader"]
    bar_count: int
    first_bar_ts: datetime
    last_bar_ts: datetime
    candle_set_sha256: str
    shared_params: dict[str, object]
    sessions: list[str]
    entry_modes: list[EntryMode]
    orb_minutes_grid: list[int]
    entry_delay_minutes_grid: list[int]
    max_age_hours_grid: list[float]
    expected_cell_count: int
    hold_bucket_labels: list[str]
    m1_coverage: M1CoverageReport
    cells: list[ScaleSweepCell]


class ReachRate(BaseModel):
    """One conditional reach probability with its Wilson interval."""

    model_config = ConfigDict(extra="forbid")

    reached: int
    n: int
    rate: float | None
    ci_low: float | None
    ci_high: float | None


class S1ReachCell(BaseModel):
    """P(survivor reaches kR within a horizon | the first stop occurred)."""

    model_config = ConfigDict(extra="forbid")

    group_kind: Literal["all", "session", "atr_tercile"]
    group_key: str
    horizon_hours: float
    k: float
    unconditional: ReachRate
    lock_survived: ReachRate


class S1ExcursionCell(BaseModel):
    """MFE and MAE distributions in pips, R, and opening-range units."""

    model_config = ConfigDict(extra="forbid")

    group_kind: Literal["all", "session", "atr_tercile"]
    group_key: str
    horizon_hours: float
    n: int
    mfe_pips_median: float | None
    mfe_pips_p95: float | None
    mfe_pips_mean: float | None
    mae_pips_median: float | None
    mae_pips_p95: float | None
    mae_pips_mean: float | None
    mfe_r_median: float | None
    mfe_r_p95: float | None
    mae_r_median: float | None
    mae_r_p95: float | None
    mfe_orb_units_median: float | None
    mfe_orb_units_p95: float | None
    mae_orb_units_median: float | None
    mae_orb_units_p95: float | None


class S1Structure(BaseModel):
    """One conditioned structure: a survivor whose hedge stopped first."""

    model_config = ConfigDict(extra="forbid")

    pair_id: str
    session: str
    survivor_side: Literal["long", "short"]
    entry_ts: datetime
    first_stop_ts: datetime
    survivor_entry: float
    s_pips: float
    orb_range_pips: float | None
    atr_pips: float | None
    atr_tercile: str
    lock_price: float
    lock_touched_ts: datetime | None
    forward_bars: int
    mfe_r_by_horizon: dict[str, float]
    mae_r_by_horizon: dict[str, float]
    mfe_r_before_lock_by_horizon: dict[str, float]
    realized_outcome: Literal["tp", "lock", "breakeven", "whipsaw", "time_exit"]


class S1ConditioningSummary(BaseModel):
    """How the conditioned sample was formed. Exclusions are counted, not hidden."""

    model_config = ConfigDict(extra="forbid")

    structures_total: int
    conditioned: int
    excluded_no_stop: int
    excluded_simultaneous_stop: int
    excluded_not_two_legs: int
    excluded_missing_entry: int
    excluded_no_forward_bars: int
    lock_touched: int
    lock_distance_pips: float
    lock_collapsed_to_entry: int


class S1TargetHitReport(BaseModel):
    """S1: conditional target-hit, the study that actually selects RR."""

    model_config = ConfigDict(extra="forbid")

    study: Literal["s1_conditional_target_hit"] = "s1_conditional_target_hit"
    symbol: str
    timeframe: Timeframe
    source: Literal["local", "ctrader"]
    bar_count: int
    first_bar_ts: datetime
    last_bar_ts: datetime
    candle_set_sha256: str
    reference_entry_mode: EntryMode
    shared_params: dict[str, object]
    sessions: list[str]
    k_values: list[float]
    horizon_hours: list[float]
    atr_period: int
    atr_tercile_edges_pips: list[float]
    m1_coverage: M1CoverageReport
    conditioning: S1ConditioningSummary
    reach_cells: list[S1ReachCell]
    excursions: list[S1ExcursionCell]
    structures: list[S1Structure]


class S2Episode(BaseModel):
    """One session-day opening range and what price did to its two sides."""

    model_config = ConfigDict(extra="forbid")

    session: str
    anchor_ts: datetime
    weekday: str
    orb_high: float
    orb_low: float
    orb_range_pips: float
    atr_pips: float | None
    contraction_ratio: float | None
    contraction_tercile: str
    bullish: bool
    horizon_hours: float
    classification: Literal[
        "no_break",
        "single_break_up",
        "single_break_down",
        "double_break_up_first",
        "double_break_down_first",
        "ambiguous_same_bar",
    ]
    first_break_side: Literal["up", "down", "both", "none"]
    first_break_hours: float | None
    opposite_break_hours: float | None
    forward_bars: int


class S2Cell(BaseModel):
    """Break frequencies for one group at one horizon. Rates sum to one over classes."""

    model_config = ConfigDict(extra="forbid")

    group_kind: Literal["all", "session", "weekday", "contraction_tercile"]
    group_key: str
    horizon_hours: float
    n: int
    no_break: int
    single_break_up: int
    single_break_down: int
    double_break_up_first: int
    double_break_down_first: int
    ambiguous_same_bar: int
    single_break_rate: float | None
    single_break_ci_low: float | None
    single_break_ci_high: float | None
    double_break_rate: float | None
    double_break_ci_low: float | None
    double_break_ci_high: float | None
    no_break_rate: float | None
    median_first_break_hours: float | None
    median_opposite_break_hours: float | None


class S2ModeCompanion(BaseModel):
    """What the engine actually paid for these breaks, per entry mode."""

    model_config = ConfigDict(extra="forbid")

    entry_mode: EntryMode
    completed_structures: int
    whipsaw_structures: int
    whipsaw_rate: float | None
    whipsaw_ci_low: float | None
    whipsaw_ci_high: float | None
    tp_structures: int
    lock_structures: int
    breakeven_structures: int
    time_exit_structures: int
    triggered_entry_orders: int
    cancelled_entry_orders: int
    expired_entry_orders: int
    loss_closed_structures: int
    false_break_rate: float | None
    false_break_definition: str
    gross_pips: float
    net_pips: float
    gross_r: float
    net_r: float


class S2BreakFrequencyReport(BaseModel):
    """S2: how often one side breaks and the other is never tested."""

    model_config = ConfigDict(extra="forbid")

    study: Literal["s2_break_frequency"] = "s2_break_frequency"
    symbol: str
    timeframe: Timeframe
    source: Literal["local", "ctrader"]
    bar_count: int
    first_bar_ts: datetime
    last_bar_ts: datetime
    candle_set_sha256: str
    shared_params: dict[str, object]
    sessions: list[str]
    horizon_hours: list[float]
    walk_starts_at: Literal["opening_range_close"]
    contraction_tercile_edges: list[float]
    m1_coverage: M1CoverageReport
    episodes_total: int
    episodes_without_forward_bars: int
    cells: list[S2Cell]
    mode_companions: list[S2ModeCompanion]
    episodes: list[S2Episode]


class S3AnchorCell(BaseModel):
    """One anchor variant, run as the only session, everything else held fixed."""

    model_config = ConfigDict(extra="forbid")

    session: str
    anchor_label: str
    anchor_spec: str
    is_incumbent: bool
    basis: str
    signals: int
    anchor_skips: int
    anchor_drift_p50: float | None
    anchor_drift_max: float | None
    episodes: int
    completed_structures: int
    gross_pips: float
    net_pips: float
    gross_r: float
    net_r: float
    gross_expectancy_pips: float | None
    net_expectancy_pips: float | None
    gross_expectancy_r: float | None
    net_expectancy_r: float | None
    gross_profit_factor: float | None
    net_profit_factor: float | None
    survivor_tp_rate: float | None
    breakeven_tp_rate_required: float | None
    tp_rate_margin_pp: float | None
    tp_rate_margin_pp_ci_low: float | None
    tp_rate_margin_pp_ci_high: float | None
    gross_max_drawdown_r: float
    net_max_drawdown_r: float
    median_orb_range_pips: float | None
    median_range_expansion: float | None
    median_volume_expansion: float | None
    range_expansion_episodes: int
    volume_expansion_episodes: int
    suppressed_signals: int
    unresolved_structures: int
    prop_guard_breached: bool


class S3AnchorStudyReport(BaseModel):
    """S3: are these anchors marking the right events, and is New York's loss an anchor bug?"""

    model_config = ConfigDict(extra="forbid")

    study: Literal["s3_anchor_study"] = "s3_anchor_study"
    symbol: str
    timeframe: Timeframe
    source: Literal["local", "ctrader"]
    bar_count: int
    first_bar_ts: datetime
    last_bar_ts: datetime
    candle_set_sha256: str
    shared_params: dict[str, object]
    entry_mode: EntryMode
    m1_coverage: M1CoverageReport
    expansion_baseline: Literal["equal_length_window_before_the_anchor"]
    cells: list[S3AnchorCell]


class S4CostCell(BaseModel):
    """One entry mode at one modelled cost per side, gross and net side by side."""

    model_config = ConfigDict(extra="forbid")

    entry_mode: EntryMode
    spread_pips_per_side: float
    slippage_pips_per_side: float
    commission_pips_per_side: float
    configured_execution_cost_pips_per_side: float
    completed_structures: int
    gross_pips: float
    net_pips: float
    gross_r: float
    net_r: float
    execution_cost_pips: float
    financing_cost_pips: float
    total_cost_pips: float
    gross_expectancy_pips: float | None
    net_expectancy_pips: float | None
    gross_expectancy_r: float | None
    net_expectancy_r: float | None
    gross_profit_factor: float | None
    net_profit_factor: float | None
    transaction_sides: int
    cost_side_equivalents: float
    breakeven_pips_per_completed_side: float | None
    cost_headroom_ratio: float | None
    meets_two_times_headroom: bool
    net_pips_positive: bool
    net_r_positive: bool
    pips_and_r_agree_in_sign: bool


class S4CostSensitivityReport(BaseModel):
    """S4: cost sensitivity and break-even, in pips per side."""

    model_config = ConfigDict(extra="forbid")

    study: Literal["s4_cost_sensitivity"] = "s4_cost_sensitivity"
    symbol: str
    timeframe: Timeframe
    source: Literal["local", "ctrader"]
    bar_count: int
    first_bar_ts: datetime
    last_bar_ts: datetime
    candle_set_sha256: str
    shared_params: dict[str, object]
    entry_modes: list[EntryMode]
    spread_grid: list[float]
    slippage_grid: list[float]
    commission_grid: list[float]
    expected_cell_count: int
    headroom_gate: float
    m1_coverage: M1CoverageReport
    cells: list[S4CostCell]


class S9RegimeCell(BaseModel):
    """One entry mode inside one regime split, gross and net side by side."""

    model_config = ConfigDict(extra="forbid")

    entry_mode: EntryMode
    split_kind: Literal["all", "calendar_half", "trend_regime", "session"]
    split_key: str
    completed_structures: int
    gross_pips: float
    net_pips: float
    gross_r: float
    net_r: float
    gross_expectancy_r: float | None
    net_expectancy_r: float | None
    gross_profit_factor: float | None
    net_profit_factor: float | None
    win_rate_excl_be: float | None
    tp_structures: int
    long_winners: int
    short_winners: int
    long_winner_share: float | None
    long_winner_ci_low: float | None
    long_winner_ci_high: float | None
    net_r_from_long: float
    net_r_from_short: float
    long_net_r_share: float | None


class S9DirectionalFlag(BaseModel):
    """A configuration whose result leans on one direction or one regime."""

    model_config = ConfigDict(extra="forbid")

    entry_mode: EntryMode
    reason: str
    detail: str


class S9RegimeReport(BaseModel):
    """S9: is the edge a regime bet wearing a hedge costume?"""

    model_config = ConfigDict(extra="forbid")

    study: Literal["s9_regime_attribution"] = "s9_regime_attribution"
    symbol: str
    timeframe: Timeframe
    source: Literal["local", "ctrader"]
    bar_count: int
    first_bar_ts: datetime
    last_bar_ts: datetime
    candle_set_sha256: str
    shared_params: dict[str, object]
    entry_modes: list[EntryMode]
    trend_lookback_days: int
    trend_deadband_pips_per_day: float
    calendar_split_ts: datetime
    price_first: float
    price_last: float
    price_change_pips: float
    trend_day_counts: dict[str, int]
    concentration_threshold: float
    m1_coverage: M1CoverageReport
    cells: list[S9RegimeCell]
    flags: list[S9DirectionalFlag]


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    sessions: list[str]
    lock_pips: float
    entry_mode: EntryMode
    tp_mode: TargetMode
    partial_tp_r: float
    partial_fraction: float
    lock_mode: LockMode
    lock_r: float
    be_trigger_r: float = 0.0
    survivor_exit_mode: SurvivorExitMode
    survivor_trail_activation_r: float
    survivor_trail_gap_r: float
    hedge_path_mode: HedgePathMode
    hedge_ratio_initial: float
    hedge_trigger_mode: HedgeTriggerMode
    hedge_failure_k: float
    hedge_ratio_staged: float
    oco_buffer_mode: OcoBufferMode
    oco_buffer_value: float
    oco_expiry_bars: int
    allow_reentry: bool
    stop_mode: StopMode
    sl_mult: float
    fixed_stop_pips: float
    rr: float
    min_stop_pips: float
    min_stop_cost_mult: float
    filter_d1_ema50: bool
    filter_nr7: bool
    filter_orb_atr_min: float
    filter_orb_atr_max: float
    qty: float
    pip_size: float
    point_value: float
    orb_minutes: int
    entry_delay_minutes: int
    anchor_tolerance_minutes: int
    intrabar_mode: IntrabarMode
    default_dollars_per_pip_per_qty: float
    cost_model: CostModel
    spread_pips_per_side: float
    slippage_pips_per_side: float
    commission_pips_per_side: float
    swap_long_pips_per_rollover: float
    swap_short_pips_per_rollover: float
    swap_rollover_time: str
    swap_timezone: str
    swap_triple_weekday: str
    session_cost_overrides: dict[str, dict[str, float]]
    breakeven_cost_report: bool
    risk_mode: RiskMode
    risk_pct_per_r: float
    max_pair_risk_pct: float
    max_open_risk_pct: float
    max_concurrent_structures: int
    one_open_per_session: bool
    contract_size: float
    firm_profile: FirmProfileMode
    firm_initial_balance: float
    firm_daily_loss_limit_pct: float
    firm_total_loss_limit_pct: float
    firm_timezone: str
    firm_daily_reset_time: str
    firm_breach_action: str
    time_exit_mode: TimeExitMode
    max_age_hours: float


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Which strategy to run. Defaults to session_hedge so every existing caller
    # and every stored request keeps working unchanged; see registry.py.
    strategy: str | None = None
    symbol: str = "XAUUSD"
    timeframe: Timeframe = Timeframe.M15
    date_from: datetime | None = None
    date_to: datetime | None = None
    source: Literal["local", "ctrader"] | None = None
    entry_mode: EntryMode | None = None
    hedge_ratio_initial: float | None = Field(default=None, ge=0, le=1)
    hedge_trigger_mode: HedgeTriggerMode | None = None
    hedge_failure_k: float | None = Field(default=None, ge=0)
    hedge_ratio_staged: float | None = Field(default=None, ge=0, le=1)
    oco_buffer_mode: OcoBufferMode | None = None
    oco_buffer_value: float | None = Field(default=None, ge=0)
    oco_expiry_bars: int | None = Field(default=None, gt=0)
    allow_reentry: bool | None = None
    lock_pips: float | None = Field(default=None, ge=0)
    lock_mode: LockMode | None = None
    lock_r: float | None = Field(default=None, ge=0)
    be_trigger_r: float | None = Field(default=None, ge=0)
    survivor_exit_mode: SurvivorExitMode | None = None
    survivor_trail_activation_r: float | None = Field(default=None, gt=0)
    survivor_trail_gap_r: float | None = Field(default=None, gt=0)
    hedge_path_mode: HedgePathMode | None = None
    stop_mode: StopMode | None = None
    sl_mult: float | None = Field(default=None, gt=0)
    fixed_stop_pips: float | None = Field(default=None, ge=0)
    rr: float | None = Field(default=None, gt=0)
    tp_mode: TargetMode | None = None
    partial_tp_r: float | None = Field(default=None, gt=0)
    partial_fraction: float | None = Field(default=None, gt=0, le=1)
    min_stop_pips: float | None = Field(default=None, ge=0)
    min_stop_cost_mult: float | None = Field(default=None, ge=0)
    filter_d1_ema50: bool | None = None
    filter_nr7: bool | None = None
    filter_orb_atr_min: float | None = Field(default=None, ge=0)
    filter_orb_atr_max: float | None = Field(default=None, ge=0)
    entry_hours_utc_exclude: list[int] | None = None
    qty: float | None = Field(default=None, gt=0)
    sessions: list[str] | None = None
    performance_unit: PerformanceUnit | None = None
    dollars_per_pip_per_qty: float | None = Field(default=None, gt=0)
    orb_minutes: int | None = Field(default=None, gt=0)
    entry_delay_minutes: int | None = Field(default=None, ge=0)
    anchor_tolerance_minutes: int | None = Field(default=None, ge=0)
    intrabar_mode: IntrabarMode | None = None
    cost_model: CostModel | None = None
    spread_pips_per_side: float | None = Field(default=None, ge=0)
    slippage_pips_per_side: float | None = Field(default=None, ge=0)
    commission_pips_per_side: float | None = Field(default=None, ge=0)
    swap_long_pips_per_rollover: float | None = Field(default=None, ge=0)
    swap_short_pips_per_rollover: float | None = Field(default=None, ge=0)
    swap_rollover_time: str | None = None
    swap_timezone: str | None = None
    swap_triple_weekday: Literal["monday", "tuesday", "wednesday", "thursday", "friday"] | None = (
        None
    )
    session_cost_overrides: dict[str, dict[str, float]] | None = None
    breakeven_cost_report: bool | None = None
    risk_mode: RiskMode | None = None
    risk_pct_per_r: float | None = Field(default=None, gt=0, le=100)
    max_pair_risk_pct: float | None = Field(default=None, gt=0, le=100)
    max_open_risk_pct: float | None = Field(default=None, ge=0, le=100)
    max_concurrent_structures: int | None = Field(default=None, ge=0)
    one_open_per_session: bool | None = None
    firm_profile: FirmProfileMode | None = None
    firm_initial_balance: float | None = Field(default=None, gt=0)
    firm_daily_loss_limit_pct: float | None = Field(default=None, gt=0, le=100)
    firm_total_loss_limit_pct: float | None = Field(default=None, gt=0, le=100)
    firm_timezone: str | None = None
    firm_daily_reset_time: str | None = None
    time_exit_mode: TimeExitMode | None = None
    max_age_hours: float | None = Field(default=None, gt=0)

    @field_validator("date_from", "date_to")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_timezone(value)


class PaperExecutionObservation(BaseModel):
    """Observed paper fill. Not a broker order and not live execution."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["paper_fill_observation"] = "paper_fill_observation"
    not_broker_order: Literal[True] = True
    observed_at: datetime
    bar_ts: datetime
    event_kind: str
    session: str
    fill_price: float | None = None
    modeled_slippage_pips_per_side: float
    pair_id: str | None = None


class PaperStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    last_ts: datetime | None
    open_pairs: list[OpenPairView]
    pending_entry_orders: list[OpenEntryOrderView] = Field(default_factory=list)
    stats: Stats
    events: list[EngineEvent]
    prop_guard_breached: bool = False
    prop_guard_breach_reason: str | None = None
    execution_observations: list[PaperExecutionObservation] = Field(default_factory=list)
    trade_pairs: list[TradePairResult] = Field(default_factory=list)
    equity_curve: list[EquityCurvePoint] = Field(default_factory=list)
    # These were Literal[False] while no bridge existed. They are now real state: a reader
    # must be able to tell a simulation from a service that is sending orders.
    execution_mode: ExecutionMode = ExecutionMode.OFF
    sends_broker_orders: bool = False


class TrackedOrderView(BaseModel):
    """One structure leg as the bridge believes the broker holds it."""

    model_config = ConfigDict(extra="forbid")

    pair_id: str
    side: str
    operation_id: str
    submitted_at: str
    state: str
    order_id: int | None = None
    position_id: int | None = None
    fill_price: float | None = None
    entry_price: float | None = None
    reason: str | None = None
    shadow: bool = False
    payload: dict[str, object] = Field(default_factory=dict)


class BrokerOrderView(BaseModel):
    model_config = ConfigDict(extra="allow")

    account: str | None = None
    order_id: int | None = None
    instrument: str | None = None
    volume_lots: str | None = None
    state: str | None = None


class BrokerPositionView(BaseModel):
    model_config = ConfigDict(extra="allow")

    account: str | None = None
    position_id: int | None = None
    instrument: str | None = None
    volume_lots: str | None = None
    direction: str | None = None
    price: str | None = None
    stop_loss: str | None = None
    take_profit: str | None = None


class ExecutionDivergence(BaseModel):
    """Where the engine's belief and the broker's records disagree.

    This is the number that decides whether the model is tradable. Everything else on the
    live page is descriptive; this is the part that can invalidate the backtest.
    """

    model_config = ConfigDict(extra="forbid")

    engine_open_structures: int
    broker_open_positions: int
    engine_resting_orders: int
    broker_resting_orders: int
    positions_matched: bool
    orders_matched: bool
    unmatched_broker_positions: list[int] = Field(default_factory=list)
    unmatched_engine_orders: list[str] = Field(default_factory=list)
    slippage_pips: list[float] = Field(default_factory=list)
    mean_slippage_pips: float | None = None
    notes: list[str] = Field(default_factory=list)


class ExecutionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ExecutionMode
    sends_broker_orders: bool
    account: str
    volume_lots: float
    halted_reason: str | None = None
    consecutive_failures: int = 0
    gateway_ready: bool = False
    gateway_reason: str = "not checked"
    tracked_orders: list[TrackedOrderView] = Field(default_factory=list)
    broker_orders: list[BrokerOrderView] = Field(default_factory=list)
    broker_positions: list[BrokerPositionView] = Field(default_factory=list)
    divergence: ExecutionDivergence | None = None


class ResearchSimulationLabel(BaseModel):
    """Marks S7 panels as research simulation, not interactive-backtest or broker facts."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["research_simulation"] = "research_simulation"
    not_interactive_backtest: Literal[True] = True
    not_broker_fact: Literal[True] = True
    caveats: list[str]


class PercentileDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: float
    p01: float
    p05: float
    p50: float
    p95: float
    p99: float
    max: float
    mean: float


class S7BreachDays(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit_pct: float
    breach_count: int
    breach_probability: float
    expected_days_to_breach_conditional: float | None
    median_days_to_breach_conditional: float | None


class S7ModePropPanel(BaseModel):
    """One incumbent mode's S7 prop panel. Research simulation only."""

    model_config = ConfigDict(extra="forbid")

    entry_mode: EntryMode
    complete_structure_count: int
    cluster_count: int
    worst_simulated_path_gross_pips: float
    worst_simulated_path_net_pips: float
    worst_simulated_path_gross_r: float
    worst_simulated_path_net_r: float
    daily_breach_days: dict[str, S7BreachDays]
    total_breach_days: dict[str, S7BreachDays]
    minimum_free_margin_pct_distribution: PercentileDistribution
    headroom_path: PercentileDistribution


class S7ResearchArtifact(BaseModel):
    """Read-only projection of the committed S7 Monte Carlo artifact."""

    model_config = ConfigDict(extra="forbid")

    source: ResearchSimulationLabel
    study: Literal["s7_propguard_monte_carlo"]
    seed: int
    simulation_count_per_mode: int
    horizon_days: int
    candle_set_sha256: str
    bar_count: int
    modes: list[S7ModePropPanel]
