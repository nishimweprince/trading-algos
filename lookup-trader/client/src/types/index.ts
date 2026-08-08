export interface Candle {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface CandlePage {
  items: Candle[];
  offset: number;
  limit: number;
  total: number;
  has_previous: boolean;
  has_next: boolean;
  min_ts: string | null;
  max_ts: string | null;
}

export type MetaEventVerdict = "valid" | "invalid" | "uncertain";

export interface MetaEventListItem {
  event_id: string;
  symbol: string;
  timeframe: string;
  signal_ts: string;
  side: 1 | -1;
  primary_setup_id: string;
  setup_ids: string[];
  confidence: number;
  tag_source: string;
  data_quality_reliable: boolean;
  context_reliable: boolean;
  verdict: MetaEventVerdict | null;
  reviewed_at: string | null;
  revealed_at: string | null;
}

export interface MetaEventPage {
  items: MetaEventListItem[];
  offset: number;
  limit: number;
  total: number;
  has_next: boolean;
}

export interface MetaEventSummary {
  total: number;
  by_year: Record<string, number>;
  by_setup: Record<string, number>;
  by_side: Record<string, number>;
  by_quality: Record<string, number>;
  by_review: Record<string, number>;
}

export interface MetaEventDetail extends MetaEventListItem {
  history_candles: Candle[];
  signal_close: number;
  atr_at_signal: number;
  pre_notes: string | null;
  post_notes: string | null;
  [key: string]: unknown;
}

export interface MetaEventOutcome {
  event_id: string;
  entry_ts: string;
  exit_ts: string;
  entry_price: number;
  stop_price: number;
  target_price: number;
  exit_price: number;
  outcome: "win" | "loss" | "timeout";
  gross_r: number;
  bars_to_resolution: number;
  ambiguous_bar: boolean;
  cost_r_3: number;
  net_r_3: number;
  y_meta_3: number;
  cost_r_5: number;
  net_r_5: number;
  y_meta_5: number;
  cost_r_8: number;
  net_r_8: number;
  y_meta_8: number;
  y_meta: number;
  forward_candles: Candle[];
  revealed_at: string;
}

export interface MetaEventQuery {
  symbol: string;
  timeframe: string;
  offset?: number;
  limit?: number;
  year?: number;
  setup?: string;
  side?: 1 | -1;
  confidenceMin?: number;
  quality?: "reliable" | "unreliable";
  reviewStatus?: string;
}

export type MetaLiveEventLifecycle = "awaiting_entry" | "open" | "resolved" | "ineligible";

export interface MetaShadowPrediction {
  artifact_version: string;
  meta_feature_version: 1 | 2;
  probability: number;
  threshold: number;
  would_take: boolean;
  created_at: string;
}

export interface MetaReplayPrediction {
  artifact_version: string;
  role: "active" | "challenger";
  meta_feature_version: 1 | 2;
  probability: number;
  threshold: number;
  would_take: boolean;
  target_take_rate: number | null;
}

export interface MetaReplayInference {
  symbol: string;
  timeframe: string;
  signal_ts: string;
  side: 1 | -1;
  status: "research_shadow";
  orders_enabled: false;
  calendar_coverage_ok: boolean;
  predictions: MetaReplayPrediction[];
  contract: {
    entry: "next_h1_open";
    stop_atr: number;
    target_atr: number;
    horizon_bars: number;
  };
}

export interface MetaShadowEvent {
  event_id: string;
  symbol: string;
  timeframe: string;
  signal_ts: string;
  side: 1 | -1;
  primary_setup_id: string;
  setup_ids: string[];
  confidence: number;
  state: MetaLiveEventLifecycle;
  ineligible_reason: string | null;
  forward_evaluation_eligible: boolean;
  calendar_coverage_ok: boolean;
  calendar_manifest_sha256: string | null;
  entry_ts: string | null;
  exit_ts: string | null;
  outcome: "win" | "loss" | "timeout" | null;
  net_r_3: number | null;
  net_r_5: number | null;
  net_r_8: number | null;
  predictions: MetaShadowPrediction[];
}

export interface MetaShadowPage {
  items: MetaShadowEvent[];
  offset: number;
  limit: number;
  total: number;
}

export interface MetaShadowArtifactStatus {
  pointer_version: number;
  active_version: string;
  reference_version: string;
  challenger_version: string | null;
  previous_active_version: string | null;
  status: "research_shadow";
  orders_enabled: false;
  activated_at: string;
  challenger_started_at?: string | null;
}

export interface WeeklyMetaPromotionReport {
  status:
    | "not_due"
    | "no_change"
    | "challenger_created"
    | "insufficient_forward_evidence"
    | "rejected"
    | "promoted";
  snapshot_sha256?: string;
  active_version?: string;
  challenger_version?: string | null;
  gates?: Record<string, boolean>;
  orders_enabled?: false;
  [key: string]: unknown;
}

export interface MetaModelStatus {
  status: string;
  orders_enabled: false;
  active_shadow: MetaShadowArtifactStatus | null;
  ledger: {
    status: string;
    events: number;
    unresolved: number;
    forward_events?: number;
    forward_shadow_start_ts?: string | null;
    last_promotion?: WeeklyMetaPromotionReport | null;
    [key: string]: unknown;
  };
  capital_boundary: Record<string, unknown> | null;
  capital_publish: Record<string, unknown> | null;
  calendar_manifest: Record<string, unknown> | null;
}

export interface CandleBounds {
  min_ts: string | null;
  max_ts: string | null;
  bar_count: number;
  htf_available?: boolean;
  htf_timeframe?: string | null;
}

export interface Setup {
  setup_id: string;
  name: string;
  description?: string | null;
  default_side?: number | null;
  category?: string | null;
  active?: boolean;
}

export interface Session {
  session_id: string;
  started_at?: string | null;
  ended_at?: string | null;
  symbol?: string | null;
  timeframe?: string | null;
  date_from?: string | null;
  date_to?: string | null;
  blinded?: boolean;
  notes?: string | null;
}

export interface Occurrence {
  id: string;
  source: string;
  session_id?: string | null;
  symbol: string;
  timeframe: string;
  ts: string;
  setup_id: string;
  side: number;
  entry?: number | null;
  sl?: number | null;
  tp?: number | null;
  max_bars?: number | null;
  atr_period?: number | null;
  atr_at_signal?: number | null;
  result?: string | null;
  realized_r?: number | null;
  bars_to_resolution?: number | null;
  observed_result?: string | null;
  trend_state?: string | null;
  atr_bucket?: string | null;
  session?: string | null;
  rsi_band?: string | null;
  calendar_flag?: boolean | null;
  calendar_tags?: string | null;
  notes?: string | null;
  labeler_version?: string | null;
  pips_captured?: number | null;
  observed_trend?: string | null;
  confluence_tags?: string | null;
  screenshot_entry?: string | null;
  screenshot_exit?: string | null;
  metadata?: TradeMetadata | null;
  exit_ts?: string | null;
  exit_price?: number | null;
  r_at_horizon?: number | null;
  net_r?: number | null;
  ambiguous_bar?: boolean | null;
  entry_feasible?: boolean | null;
  mfe_r?: number | null;
  mae_r?: number | null;
  mfe_pips?: number | null;
  mae_pips?: number | null;
  bars_to_mfe?: number | null;
  bars_to_mae?: number | null;
  r_grid?: Record<string, RGridEntry> | null;
  market_structure?: string | null;
  htf_alignment?: string | null;
  entry_quality?: string | null;
  confidence?: number | null;
  rr_bucket?: string | null;
  sl_atr_bucket?: string | null;
  outcome_kind?: string | null;
  skip_reason?: string | null;
  blinded?: boolean | null;
  peeked?: boolean | null;
  /** Decided with the base rate visible — see TradeProvenance.saw_base_rate. */
  assisted?: boolean | null;
  /** The prior frozen at annotation time, inherited from the linked signal. */
  base_rate_at_signal?: BaseRateGrid | null;
  context_reliable?: boolean | null;
  excluded?: boolean | null;
  exclude_reason?: string | null;
  feature_version?: string | null;
  features?: TradeFeatures | null;
  signal_id?: string | null;
  lifecycle?: string | null;
  compare_at_signal?: CompareResult | null;
  context_fingerprint?: string | null;
  entry_convention?: string | null;
  day_of_week?: string | null;
  htf_trend_state?: string | null;
  at_key_level?: boolean | null;
  level_type?: string | null;
  consolidation_before?: boolean | null;
  tagger_confidence?: string | null;
  payload_hash?: string | null;
  tagger_model_version?: string | null;
  created_at?: string | null;
}

/** What the same stop would have produced against a different target. */
export interface RGridEntry {
  result: "win" | "loss" | "timeout" | "ambiguous";
  bars: number | null;
  ambiguous: boolean;
}

export interface TradeMetadata {
  market_structure?: string;
  htf_alignment?: string;
  entry_quality?: string;
  confidence?: number;
}

/** Machine-computed, reproducible from the candles. Operator labels go in metadata. */
export interface TradeFeatures {
  rsi_value?: number;
  ema_value?: number;
  atr_pct?: number;
  dist_ema_atr?: number | null;
  atr_terciles?: [number, number];
  warmup_bars_available?: number;
  pip_size?: number;
  spread_pips_assumed?: number;
  entry_next_open?: number | null;
  entry_fill_bars?: number | null;
  touched_1r_before_sl?: boolean | null;
  sl_pips?: number | null;
  sl_atr_mult?: number | null;
  rr_planned?: number;
  peeked?: boolean;
  max_cursor_before_arm?: number;
  decision_ms?: number;
  level_revisions?: number;
  bars_visible_at_signal?: number;
  next_open_label?: {
    entry: number;
    sl: number;
    tp: number;
    result: string;
    realized_r: number | null;
    bars_to_resolution: number;
  };
}

export type BarTagState = "complete" | "forming" | "invalidated";

/** A pattern the server detected on a bar. */
export interface BarTag {
  setup_id: string;
  state: BarTagState;
  /**
   * Match quality — how good an example of the pattern this bar is. Not a
   * probability that the trade works; the base rate answers that.
   */
  confidence: number;
  source: "rule" | "algorithm";
  /** Set per-instance, because a break's direction is not in the setup itself. */
  side?: 1 | -1 | null;
  model_version?: string | null;
}

/** Context features at a signal bar, as the server computes them. */
export interface SignalContext {
  trend_state: string;
  atr_bucket: string;
  session: string;
  rsi_band: string;
  atr_at_signal: number;
  rsi_value: number;
  dist_ema_atr?: number | null;
  warmup_bars_available: number;
  context_reliable: boolean;
  day_of_week?: string | null;
  htf_trend_state?: string | null;
  ema_slope_bucket?: string | null;
  atr_change_bucket?: string | null;
  dist_day_high_atr?: number | null;
  dist_day_low_atr?: number | null;
  signal_body_pct?: number | null;
  signal_range_atr?: number | null;
  bar_tags?: BarTag[];
  tag_primary_setup_id?: string | null;
  /** Whether the tags came from the feature store or were computed on request. */
  tag_source?: "store" | "live";
}

/** Every dimension /compare can filter on. Supplying one opts into filtering. */
export interface CompareContext {
  trend_state?: string;
  session?: string;
  atr_bucket?: string;
  rsi_band?: string;
  side?: 1 | -1;
  rr_bucket?: string;
  sl_atr_bucket?: string;
  calendar_flag?: boolean;
  observed_trend?: string;
  market_structure?: string;
  htf_alignment?: string;
  entry_quality?: string;
  confidence_min?: number;
  confluence_tags?: string[];
  day_of_week?: string;
  htf_trend_state?: string;
  ema_slope_bucket?: string;
  atr_change_bucket?: string;
  entry_convention?: "marked" | "next_open";
  at_key_level?: boolean;
  level_type?: string;
  consolidation_before?: boolean;
}

/** How the same stop would have fared against a different target. */
export interface TargetOutcome {
  target_r: number;
  wins: number;
  decided: number;
  win_rate: number | null;
  expectancy_r: number | null;
}

export type RecommendationVerdict =
  | "buy"
  | "sell"
  | "lean_long"
  | "lean_short"
  | "wait"
  | "insufficient_data";

export interface RecommendationPayload {
  verdict: RecommendationVerdict;
  headline: string;
  rationale: string;
  caveats: string[];
  policy_version: string;
}

export interface CompareResult {
  matched_count: number;
  wins: number;
  decided: number;
  timeouts: number;
  win_rate: number | null;
  wilson_low: number | null;
  wilson_high: number | null;
  expectancy_r: number | null;
  expectancy_r_net?: number | null;
  level_used: string;
  /** Share of matched occurrences overlapping another; high means the CI is optimistic. */
  overlap_ratio?: number | null;
  target_grid: TargetOutcome[];
  median_mfe_r: number | null;
  median_mae_r: number | null;
  skipped_count: number;
  skip_rate?: number | null;
  skip_reasons: Record<string, number>;
  excluded_peeked: number;
  scored_side?: number | null;
  scored_direction?: "long" | "short" | null;
  recommendation?: RecommendationPayload | null;
  min_samples_required?: number | null;
  decided_available?: number | null;
}

/**
 * What price did next from every historical bar sharing this bar's context —
 * the prior a setup's win rate has to beat to be evidence of anything. Unlike
 * CompareResult this needs no marked trade, so the sample is the whole candle
 * history rather than the handful of occurrences labelled so far.
 */
export interface BaseRate {
  matched_count: number;
  resolved_count: number;
  wins: number;
  losses: number;
  decided: number;
  timeouts: number;
  win_rate: number | null;
  wilson_low: number | null;
  wilson_high: number | null;
  expectancy_r: number | null;
  expectancy_r_net?: number | null;
  /**
   * Independent observations, about decided / horizon. Adjacent bars share all
   * but one of their forward bars, so the row count badly overstates the sample
   * and the interval is computed from this instead.
   */
  effective_n: number | null;
  net_expectancy_ci_low_r: number | null;
  net_expectancy_ci_high_r: number | null;
  confidence_level: number | null;
  confidence_method: string | null;
  independent_periods: number | null;
  level_used: string;
  dimensions_used: string[];
  requested_dimensions: string[];
  dropped_dimensions: string[];
  fallback_used: boolean;
  median_mfe_atr: number | null;
  median_mae_atr: number | null;
  /** Every target against the same stop and the same matched bars. */
  target_grid: BaseRateCell[];
  horizon: number;
  target_atr: number | null;
  stop_atr: number | null;
  side: number | null;
  scored_side?: number | null;
  scored_direction?: "long" | "short" | null;
  recommendation?: RecommendationPayload | null;
  gross_break_even_win_rate?: number | null;
  spread_adjusted_break_even_win_rate?: number | null;
  recommendation_policy_version?: string | null;
  min_samples_required?: number | null;
  decided_available?: number | null;
  min_periods_required?: number | null;
  periods_available?: number | null;
}

export interface OutcomeDirection {
  direction: "long" | "short";
  side: 1 | -1;
  p_win: number;
  p_loss: number;
  p_timeout: number;
  expected_gross_r: number;
  estimated_spread_cost_r: number;
  expected_net_r: number;
  gross_break_even_p_win: number;
  spread_adjusted_break_even_p_win: number;
  edge_over_break_even: number;
}

export interface OutcomeContract {
  timeframe: "H1";
  horizon_bars: 24;
  target_atr: 1.5;
  stop_atr: 1.0;
  spread_pips_assumed: number;
  atr_at_signal: number;
}

/** Pilot-only model output. It is deliberately not a recommendation input. */
export interface OutcomeShadow {
  long: OutcomeDirection;
  short: OutcomeDirection;
  contract: OutcomeContract;
  model_version: string;
  artifact_version: string;
  schema_sha256: string;
  outcome_feature_version: string;
  feature_version: string;
  bar_feature_version: string;
  status: "pilot_shadow";
  pilot: true;
  promoted: false;
}

export interface ShadowPrediction {
  artifact_version: string;
  model_version: string;
  symbol: string;
  timeframe: string;
  ts: string;
  side: 1 | -1;
  direction: "long" | "short";
  p_win: number;
  p_loss: number;
  p_timeout: number;
  expected_gross_r: number;
  expected_net_r: number;
  observed_spread?: number | null;
  action_threshold_r: number;
  would_trade: boolean;
  empirical_base_rate?: unknown;
  tags?: unknown;
  schema_sha256: string;
  feature_version: string;
  bar_feature_version: string;
  training_source: "histdata";
  live_source: "capital";
  source_boundary: string;
  created_at: string;
  outcome?: "win" | "loss" | "timeout" | null;
  resolution_as_of_ts?: string | null;
  resolved_at?: string | null;
}

/** One target/stop pair scored over the same matched bars. */
export interface BaseRateCell {
  target_atr: number;
  stop_atr: number;
  wins: number;
  decided: number;
  win_rate: number | null;
  expectancy_r: number | null;
  expectancy_r_net?: number | null;
  effective_n: number | null;
}

/**
 * Every target/stop pair the store can price, frozen at annotation time. A
 * signal has no marked levels yet, so a single ratio would describe a trade the
 * operator may not end up taking.
 */
export interface BaseRateGrid {
  level_used: string;
  dimensions_used: string[];
  matched_count: number;
  median_mfe_atr: number | null;
  median_mae_atr: number | null;
  horizon: number;
  side: number;
  min_samples_required: number;
  cells: BaseRateCell[];
}

/**
 * One bar's features, for chart overlays. The forward half arrives null unless
 * that bar's whole forward window is already behind the reveal boundary — the
 * server decides, not the client.
 */
export interface BarFeatureRow {
  ts: string;
  trend_state: string | null;
  atr_bucket: string | null;
  session: string | null;
  rsi_band: string | null;
  atr_at_bar: number | null;
  atr_pct: number | null;
  efficiency_ratio: number | null;
  close_range_pct: number | null;
  realized_vol_atr: number | null;
  context_reliable: boolean | null;
  max_atr: number | null;
  min_atr: number | null;
  bars_to_max: number | null;
  bars_to_min: number | null;
  max_first: boolean | null;
  forward_visible: boolean;
}

export interface BarSeriesQuery {
  symbol: string;
  timeframe: string;
  dateFrom: string;
  dateTo: string;
  /** Reveal boundary. Omit and the server returns no forward data at all. */
  revealedThrough?: string | null;
  horizon?: number;
}

export interface BaseRateQuery {
  symbol: string;
  timeframe: string;
  signalTs: string;
  horizon?: number;
  targetAtr?: number;
  stopAtr?: number;
  side?: number;
  pinned?: string[];
  tagSetupId?: string;
  tagState?: "complete" | "forming" | "any";
}

export interface TradeProvenance {
  peeked: boolean;
  max_cursor_before_arm: number;
  decision_ms: number;
  level_revisions: number;
  bars_visible_at_signal: number;
  /** The context base rate was on screen when this was decided. */
  saw_base_rate?: boolean;
}

export interface TradeSubmit {
  session_id?: string | null;
  signal_id?: string | null;
  symbol: string;
  timeframe: string;
  signal_ts: string;
  setup_id: string;
  side: number;
  /** Optional only for a skip — a traded occurrence must carry all three. */
  entry?: number | null;
  sl?: number | null;
  tp?: number | null;
  outcome_kind?: "traded" | "skipped";
  skip_reason?: string | null;
  notes?: string | null;
  calendar_flag?: boolean | null;
  calendar_tags?: string | null;
  observed_result?: string | null;
  observed_trend?: string | null;
  confluence_tags?: string | null;
  session?: string | null;
  pips_captured?: number | null;
  screenshot_entry?: string | null;
  screenshot_exit?: string | null;
  metadata?: TradeMetadata | null;
  blinded?: boolean | null;
  provenance?: TradeProvenance | null;
  entry_convention?: "marked" | "next_open" | null;
  at_key_level?: boolean | null;
  level_type?: string | null;
  consolidation_before?: boolean | null;
  lifecycle?: "pending" | "resolved" | null;
  date_from?: string | null;
  date_to?: string | null;
}

export interface SignalAnnotation {
  confluence_tags?: string | null;
  calendar_flag?: boolean | null;
  calendar_tags?: string | null;
  confidence?: number | null;
  at_key_level?: boolean | null;
  level_type?: "prior_swing_high" | "prior_swing_low" | "round_number" | "none" | null;
  consolidation_before?: boolean | null;
  annotation_sources?: Record<string, string> | null;
}

export interface SignalSubmit {
  session_id?: string | null;
  symbol: string;
  timeframe: string;
  signal_ts: string;
  setup_id?: string | null;
  side?: 1 | -1 | null;
  cursor_idx?: number | null;
  bars_visible?: number | null;
  peeked?: boolean | null;
  blinded?: boolean | null;
  screenshot_signal?: string | null;
  chart_render_spec?: Record<string, unknown> | null;
  compare_context?: CompareContext | null;
  compare_pinned?: string[];
  compare_min_samples?: number | null;
  compare_source?: string;
  annotations?: SignalAnnotation | null;
}

export interface Signal {
  id: string;
  source: string;
  session_id?: string | null;
  symbol: string;
  timeframe: string;
  ts: string;
  setup_id?: string | null;
  side?: number | null;
  cursor_idx?: number | null;
  bars_visible?: number | null;
  peeked?: boolean | null;
  blinded?: boolean | null;
  feature_version?: string | null;
  context_snapshot?: Record<string, unknown> | null;
  compare_context?: CompareContext | null;
  compare_at_signal?: CompareResult | null;
  context_fingerprint?: string | null;
  screenshot_signal?: string | null;
  chart_render_spec?: Record<string, unknown> | null;
  confluence_tags?: string | null;
  calendar_flag?: boolean | null;
  calendar_tags?: string | null;
  confidence?: number | null;
  at_key_level?: boolean | null;
  level_type?: string | null;
  consolidation_before?: boolean | null;
  annotation_sources?: Record<string, string> | null;
  lifecycle?: string | null;
  occurrence_id?: string | null;
  created_at?: string | null;
}

export interface SessionCreate {
  symbol: string;
  timeframe: string;
  date_from: string;
  date_to: string;
  blinded?: boolean;
  notes?: string | null;
}
