from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ObservedResult = Literal["win", "loss", "timeout", "ambiguous", "unsure"]
ObservedTrend = Literal["up", "down", "range", "choppy"]
OutcomeKind = Literal["traded", "skipped"]
SkipReason = Literal[
    "setup_poor_location",
    "wrong_session",
    "low_conviction",
    "news_risk",
    "rr_too_low",
    "missed_entry",
]


class CandleOut(BaseModel):
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class CandleBoundsOut(BaseModel):
    min_ts: str | None = None
    max_ts: str | None = None
    bar_count: int = 0
    htf_available: bool = False
    htf_timeframe: str | None = None


class SetupOut(BaseModel):
    setup_id: str
    name: str
    description: str | None = None
    default_side: int | None = None
    category: str | None = None
    active: bool = True


class SessionCreate(BaseModel):
    symbol: str
    timeframe: str
    date_from: datetime
    date_to: datetime
    blinded: bool = False
    notes: str | None = None


class SessionPatch(BaseModel):
    ended_at: datetime | None = None
    notes: str | None = None


class SessionOut(BaseModel):
    session_id: str
    started_at: str | None = None
    ended_at: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    blinded: bool = False
    notes: str | None = None


class TradeProvenance(BaseModel):
    """How the label was arrived at — the basis for trusting it later.

    `peeked` is the important one: the chart never renders future bars, but
    nothing stops the operator scrubbing forward, looking, and scrubbing back
    before marking a setup. A row that cannot be told apart from an honest one
    is worse than no row.
    """

    peeked: bool | None = None
    max_cursor_before_arm: int | None = None
    decision_ms: int | None = None
    level_revisions: int | None = None
    bars_visible_at_signal: int | None = None
    # The context base rate was on screen when this was decided. Not a leak — no
    # forward bar was shown — but the decision is then partly a function of the
    # model's own output, so the row cannot also serve as independent evidence
    # for it. Recorded rather than prevented, the way `peeked` is.
    saw_base_rate: bool | None = None


class TradeSubmit(BaseModel):
    session_id: str | None = None
    signal_id: str | None = None
    symbol: str
    timeframe: str
    signal_ts: datetime
    setup_id: str
    side: Literal[1, -1] = Field(..., description="+1 long, -1 short")
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    outcome_kind: OutcomeKind = "traded"
    skip_reason: SkipReason | None = None
    notes: str | None = None
    calendar_flag: bool | None = None
    calendar_tags: str | None = None
    observed_result: ObservedResult | None = None
    observed_trend: ObservedTrend | None = None
    confluence_tags: str | None = None
    session: str | None = None
    pips_captured: float | None = None
    screenshot_entry: str | None = None
    screenshot_exit: str | None = None
    metadata: dict | None = None
    blinded: bool | None = None
    provenance: TradeProvenance | None = None
    entry_convention: Literal["marked", "next_open"] | None = None
    at_key_level: bool | None = None
    level_type: Literal["prior_swing_high", "prior_swing_low", "round_number", "none"] | None = None
    consolidation_before: bool | None = None
    lifecycle: Literal["pending", "resolved"] | None = None
    # Accepted for backwards compatibility; the labeling window is a fixed bar
    # count around the signal, so these no longer influence anything.
    date_from: datetime | None = None
    date_to: datetime | None = None

    @model_validator(mode="after")
    def _check_levels(self) -> TradeSubmit:
        levels = (self.entry, self.sl, self.tp)
        if self.outcome_kind == "traded" and any(v is None for v in levels):
            raise ValueError("entry, sl and tp are required for a traded occurrence")
        if self.outcome_kind == "skipped" and self.skip_reason is None:
            raise ValueError("skip_reason is required when outcome_kind is 'skipped'")
        if any(v is None for v in levels):
            return self

        entry, sl, tp = levels
        if self.side == 1 and not (sl < entry < tp):
            raise ValueError(f"long requires sl < entry < tp, got sl={sl} entry={entry} tp={tp}")
        if self.side == -1 and not (tp < entry < sl):
            raise ValueError(f"short requires tp < entry < sl, got tp={tp} entry={entry} sl={sl}")
        return self


class OccurrencePatch(BaseModel):
    """Operator labels only. `result` and `realized_r` belong to the labeler."""

    notes: str | None = None
    observed_result: ObservedResult | None = None
    observed_trend: ObservedTrend | None = None
    confluence_tags: str | None = None
    calendar_flag: bool | None = None
    calendar_tags: str | None = None
    metadata: dict | None = None
    excluded: bool | None = None
    exclude_reason: str | None = None


class OccurrenceOut(BaseModel):
    id: str
    source: str
    session_id: str | None = None
    symbol: str
    timeframe: str
    ts: str
    setup_id: str
    side: int
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    max_bars: int | None = None
    atr_period: int | None = None
    atr_at_signal: float | None = None
    result: str | None = None
    realized_r: float | None = None
    bars_to_resolution: int | None = None
    observed_result: str | None = None
    trend_state: str | None = None
    atr_bucket: str | None = None
    session: str | None = None
    rsi_band: str | None = None
    calendar_flag: bool | None = None
    calendar_tags: str | None = None
    notes: str | None = None
    labeler_version: str | None = None
    pips_captured: float | None = None
    observed_trend: str | None = None
    confluence_tags: str | None = None
    screenshot_entry: str | None = None
    screenshot_exit: str | None = None
    metadata: dict | None = None
    exit_ts: str | None = None
    exit_price: float | None = None
    r_at_horizon: float | None = None
    net_r: float | None = None
    ambiguous_bar: bool | None = None
    entry_feasible: bool | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    mfe_pips: float | None = None
    mae_pips: float | None = None
    bars_to_mfe: int | None = None
    bars_to_mae: int | None = None
    r_grid: dict | None = None
    market_structure: str | None = None
    htf_alignment: str | None = None
    entry_quality: str | None = None
    confidence: int | None = None
    rr_bucket: str | None = None
    sl_atr_bucket: str | None = None
    outcome_kind: str | None = None
    skip_reason: str | None = None
    blinded: bool | None = None
    peeked: bool | None = None
    assisted: bool | None = None
    context_reliable: bool | None = None
    excluded: bool | None = None
    exclude_reason: str | None = None
    feature_version: str | None = None
    features: dict | None = None
    signal_id: str | None = None
    lifecycle: str | None = None
    compare_at_signal: dict | None = None
    base_rate_at_signal: dict | None = None
    context_fingerprint: str | None = None
    entry_convention: str | None = None
    day_of_week: str | None = None
    htf_trend_state: str | None = None
    at_key_level: bool | None = None
    level_type: str | None = None
    consolidation_before: bool | None = None
    tagger_confidence: str | None = None
    payload_hash: str | None = None
    tagger_model_version: str | None = None
    created_at: str | None = None


class CompareContext(BaseModel):
    """Every dimension /compare can filter on. All optional — supplying one opts
    into filtering on it, and it is dropped again by relaxation unless pinned."""

    # Computed at the signal bar
    trend_state: str | None = None
    session: str | None = None
    atr_bucket: str | None = None
    rsi_band: str | None = None
    side: Literal[1, -1] | None = None
    rr_bucket: str | None = None
    sl_atr_bucket: str | None = None
    calendar_flag: bool | None = None
    day_of_week: str | None = None
    htf_trend_state: str | None = None
    ema_slope_bucket: str | None = None
    atr_change_bucket: str | None = None
    entry_convention: Literal["marked", "next_open"] | None = None
    at_key_level: bool | None = None
    level_type: str | None = None
    consolidation_before: bool | None = None
    # Operator's read, recorded at resolution
    observed_trend: ObservedTrend | None = None
    market_structure: str | None = None
    htf_alignment: str | None = None
    entry_quality: str | None = None
    confidence_min: int | None = Field(None, ge=1, le=5)
    confluence_tags: list[str] | None = Field(None, description="all listed tags must be present")


class SignalAnnotation(BaseModel):
    confluence_tags: str | None = None
    calendar_flag: bool | None = None
    calendar_tags: str | None = None
    confidence: int | None = Field(None, ge=1, le=5)
    at_key_level: bool | None = None
    level_type: Literal["prior_swing_high", "prior_swing_low", "round_number", "none"] | None = None
    consolidation_before: bool | None = None
    annotation_sources: dict | None = None


class SignalSubmit(BaseModel):
    session_id: str | None = None
    symbol: str
    timeframe: str
    signal_ts: datetime
    setup_id: str | None = None
    side: Literal[1, -1] | None = None
    cursor_idx: int | None = None
    bars_visible: int | None = None
    peeked: bool | None = None
    blinded: bool | None = None
    screenshot_signal: str | None = None
    chart_render_spec: dict | None = None
    assisted: bool | None = None
    compare_context: CompareContext | None = None
    compare_pinned: list[str] = []
    compare_min_samples: int | None = None
    compare_source: str = "manual"
    annotations: SignalAnnotation | None = None


class SignalOut(BaseModel):
    id: str
    source: str
    session_id: str | None = None
    symbol: str
    timeframe: str
    ts: str
    setup_id: str | None = None
    side: int | None = None
    cursor_idx: int | None = None
    bars_visible: int | None = None
    peeked: bool | None = None
    blinded: bool | None = None
    feature_version: str | None = None
    context_snapshot: dict | None = None
    compare_context: dict | None = None
    compare_at_signal: dict | None = None
    base_rate_at_signal: dict | None = None
    assisted: bool | None = None
    context_fingerprint: str | None = None
    screenshot_signal: str | None = None
    chart_render_spec: dict | None = None
    confluence_tags: str | None = None
    calendar_flag: bool | None = None
    calendar_tags: str | None = None
    confidence: int | None = None
    at_key_level: bool | None = None
    level_type: str | None = None
    consolidation_before: bool | None = None
    annotation_sources: dict | None = None
    lifecycle: str | None = None
    occurrence_id: str | None = None
    created_at: str | None = None


class ContextOut(BaseModel):
    """Context computed at a signal bar, for pre-filling a comparison."""

    trend_state: str
    atr_bucket: str
    session: str
    rsi_band: str
    atr_at_signal: float
    rsi_value: float
    dist_ema_atr: float | None = None
    warmup_bars_available: int
    context_reliable: bool
    day_of_week: str | None = None
    htf_trend_state: str | None = None
    ema_slope_bucket: str | None = None
    atr_change_bucket: str | None = None
    dist_day_high_atr: float | None = None
    dist_day_low_atr: float | None = None
    signal_body_pct: float | None = None
    signal_range_atr: float | None = None


class CompareRequest(BaseModel):
    setup_id: str
    symbol: str
    timeframe: str
    context: CompareContext
    # Dimensions that must match. Never relaxed — if the pins alone cannot reach
    # min_samples the answer is no_signal rather than a quietly widened sample.
    pinned: list[str] = []
    source: str = "manual"
    min_samples: int | None = None
    # Labels where the operator had already seen past the signal bar are not
    # honest samples; excluded unless explicitly asked for.
    exclude_peeked: bool = True
    # Labels decided with the base rate on screen. Off by default, unlike
    # exclude_peeked: the sample is thin enough that dropping rows silently would
    # do more damage than the circularity it guards against.
    exclude_assisted: bool = False
    blinded_only: bool = False


class TargetOutcome(BaseModel):
    """How the same stop would have fared against a different target."""

    target_r: float
    wins: int
    decided: int
    win_rate: float | None = None
    expectancy_r: float | None = None


class CompareResponse(BaseModel):
    matched_count: int
    wins: int
    decided: int
    timeouts: int = 0
    win_rate: float | None = None
    wilson_low: float | None = None
    wilson_high: float | None = None
    expectancy_r: float | None = None
    expectancy_r_net: float | None = None
    level_used: str
    # Fraction of matched occurrences whose holding window overlaps another's.
    # Overlapping trades are correlated, so the Wilson interval below is
    # optimistic — this makes that visible rather than silently assuming it away.
    overlap_ratio: float | None = None
    # Outcome at each target multiple against the same stop, so "was my target
    # wrong?" can be answered from the same matched set.
    target_grid: list[TargetOutcome] = []
    median_mfe_r: float | None = None
    median_mae_r: float | None = None
    # Matching setups that were seen and passed on.
    skipped_count: int = 0
    skip_rate: float | None = None
    skip_reasons: dict[str, int] = {}
    excluded_peeked: int = 0
    # Populated when level_used is no_signal (and on success for context).
    min_samples_required: int | None = None
    decided_available: int | None = None
