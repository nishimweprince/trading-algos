export interface Candle {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
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
  context_reliable?: boolean | null;
  excluded?: boolean | null;
  exclude_reason?: string | null;
  feature_version?: string | null;
  features?: TradeFeatures | null;
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
}

/** How the same stop would have fared against a different target. */
export interface TargetOutcome {
  target_r: number;
  wins: number;
  decided: number;
  win_rate: number | null;
  expectancy_r: number | null;
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
  level_used: string;
  /** Share of matched occurrences overlapping another; high means the CI is optimistic. */
  overlap_ratio?: number | null;
  target_grid: TargetOutcome[];
  median_mfe_r: number | null;
  median_mae_r: number | null;
  skipped_count: number;
  skip_reasons: Record<string, number>;
  excluded_peeked: number;
}

export interface TradeProvenance {
  peeked: boolean;
  max_cursor_before_arm: number;
  decision_ms: number;
  level_revisions: number;
  bars_visible_at_signal: number;
}

export interface TradeSubmit {
  session_id?: string | null;
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
  date_from?: string | null;
  date_to?: string | null;
}

export interface SessionCreate {
  symbol: string;
  timeframe: string;
  date_from: string;
  date_to: string;
  blinded?: boolean;
  notes?: string | null;
}
