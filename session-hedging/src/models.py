"""Candle contract matches ctrader-markets (interval-end ``ts``, closed bars)."""

from __future__ import annotations

import re
from datetime import datetime, time
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Timeframe(StrEnum):
    M1 = "M1"
    M2 = "M2"
    M3 = "M3"
    M4 = "M4"
    M5 = "M5"
    M10 = "M10"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    H12 = "H12"
    D1 = "D1"
    W1 = "W1"


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


class LockMode(StrEnum):
    ABSOLUTE = "absolute"


class HedgeTriggerMode(StrEnum):
    FAILURE_ZONE = "failure_zone"


class OcoBufferMode(StrEnum):
    ORB_FRAC = "orb_frac"
    FIXED_PIPS = "fixed_pips"


class StopMode(StrEnum):
    """How ``S`` (one R) is sized.

    ``bar_range`` scales with the measured opening range over ``ORB_MINUTES``. ``fixed_pips``
    pins ``S`` to ``FIXED_STOP_PIPS`` regardless of the range, so R is constant across sessions.
    """

    BAR_RANGE = "bar_range"
    FIXED_PIPS = "fixed_pips"


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


class Candle(BaseModel):
    """One closed candle, timestamped at the END of its UTC interval."""

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    provider: str = "ctrader"
    source_instrument: str
    spread: float | None = None
    spread_source: str | None = None

    _check_ts = field_validator("ts")(_require_timezone)


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
    min_stop_pips: float = Field(default=0.0, ge=0)
    lock_pips: float = Field(default=20.0, ge=0)
    lock_mode: LockMode = LockMode.ABSOLUTE
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
    def _valid_cost_surface(self) -> EngineParams:
        from costs import COST_SESSION_NAMES, NUMERIC_COST_FIELDS

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
        "exit",
        "signal_skipped_anchor_drift",
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
    firm_profile: FirmProfileMode
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
    max_concurrent_structures: int = 0
    median_concurrent: float | None = None
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


class HedgeSyntheticAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    basis: Literal["hedge_pair_minus_synthetic_breakout"] = (
        "hedge_pair_minus_synthetic_breakout"
    )
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

class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    sessions: list[str]
    lock_pips: float
    entry_mode: EntryMode
    tp_mode: TargetMode
    lock_mode: LockMode
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
    qty: float
    pip_size: float
    point_value: float
    orb_minutes: int
    entry_delay_minutes: int
    anchor_tolerance_minutes: int
    intrabar_mode: IntrabarMode
    performance_unit: PerformanceUnit
    dollars_per_pip_per_qty: float | None
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
    stop_mode: StopMode | None = None
    sl_mult: float | None = Field(default=None, gt=0)
    fixed_stop_pips: float | None = Field(default=None, ge=0)
    rr: float | None = Field(default=None, gt=0)
    min_stop_pips: float | None = Field(default=None, ge=0)
    qty: float | None = Field(default=None, gt=0)
    sessions: list[str] | None = None
    performance_unit: PerformanceUnit | None = None
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
