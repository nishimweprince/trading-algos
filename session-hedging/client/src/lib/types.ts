export const SESSIONS = ["tokyo", "london", "new_york"] as const;
export type SessionName = (typeof SESSIONS)[number];

export const TIMEFRAMES = [
  "M1",
  "M5",
  "M15",
  "M30",
  "H1",
  "H4",
  "D1",
] as const;
export type Timeframe = (typeof TIMEFRAMES)[number];

export type CandleSource = "local" | "ctrader";
export type PerformanceUnit = "pips" | "dollars";

export const ENTRY_MODES = [
  "hedge_pair",
  "synthetic_breakout",
  "contingent_hedge",
  "oco_bracket",
] as const;
export type EntryMode = (typeof ENTRY_MODES)[number];

export const ENTRY_MODE_LABEL: Record<EntryMode, string> = {
  hedge_pair: "Hedge pair",
  synthetic_breakout: "Synthetic breakout",
  contingent_hedge: "Contingent hedge",
  oco_bracket: "OCO bracket",
};

export type OcoBufferMode = "orb_frac" | "fixed_pips";

export const STOP_MODES = ["bar_range", "fixed_pips", "atr14", "orb_atr14_blend"] as const;
export type StopMode = (typeof STOP_MODES)[number];

export const STOP_MODE_LABEL: Record<StopMode, string> = {
  bar_range: "Range × multiplier",
  fixed_pips: "Fixed pips",
  atr14: "ATR14 × multiplier",
  orb_atr14_blend: "50/50 range–ATR14",
};

export type TpMode = "fixed_r" | "partial_trail";

export interface ServiceConfig {
  symbol: string;
  timeframe: Timeframe;
  sessions: string[];
  entry_mode: EntryMode;
  tp_mode: TpMode;
  partial_tp_r: number;
  partial_fraction: number;
  lock_mode: "absolute" | "none" | "breakeven" | "r_relative";
  lock_r: number;
  hedge_ratio_initial: number;
  hedge_trigger_mode: "failure_zone";
  hedge_failure_k: number;
  hedge_ratio_staged: number;
  oco_buffer_mode: OcoBufferMode;
  oco_buffer_value: number;
  oco_expiry_bars: number;
  allow_reentry: boolean;
  lock_pips: number;
  stop_mode: StopMode;
  sl_mult: number;
  fixed_stop_pips: number;
  rr: number;
  min_stop_pips: number;
  min_stop_cost_mult: number;
  filter_d1_ema50: boolean;
  filter_nr7: boolean;
  filter_orb_atr_min: number;
  filter_orb_atr_max: number;
  qty: number;
  pip_size: number;
  point_value: number;
  orb_minutes: number;
  entry_delay_minutes: number;
  anchor_tolerance_minutes: number;
  intrabar_mode: string;
  default_dollars_per_pip_per_qty: number;
  cost_model: "none" | "per_session";
  spread_pips_per_side: number;
  slippage_pips_per_side: number;
  commission_pips_per_side: number;
  swap_long_pips_per_rollover: number;
  swap_short_pips_per_rollover: number;
  swap_rollover_time: string;
  swap_timezone: string;
  swap_triple_weekday: string;
  session_cost_overrides: Record<string, Record<string, number>>;
  breakeven_cost_report: boolean;
  risk_mode: "fixed_qty" | "fixed_fractional";
  risk_pct_per_r: number;
  max_pair_risk_pct: number;
  max_open_risk_pct: number;
  max_concurrent_structures: number;
  one_open_per_session: boolean;
  contract_size: number;
  firm_profile: "none" | "custom";
  firm_initial_balance: number;
  firm_daily_loss_limit_pct: number;
  firm_total_loss_limit_pct: number;
  firm_timezone: string;
  firm_daily_reset_time: string;
  firm_breach_action: "block_new";
  time_exit_mode: "none" | "max_age";
  max_age_hours: number;
}

export interface Candle {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  provider?: string;
  source_instrument: string;
  spread?: number | null;
  spread_source?: string | null;
}

export interface CandlesResponse {
  symbol: string;
  timeframe: Timeframe;
  candles: Candle[];
  source: CandleSource;
}

export interface ClosedLeg {
  session: string;
  side: "long" | "short";
  entry: number;
  exit: number;
  pnl: number;
  bucket: "win" | "be" | "loss";
  ts: string;
  reason: string;
  pair_id?: string | null;
  role?: "primary" | "hedge" | "unknown";
  entry_ts?: string | null;
  pnl_pips?: number | null;
  pnl_dollars?: number | null;
  mae_pips?: number | null;
  mfe_pips?: number | null;
  mae_dollars?: number | null;
  mfe_dollars?: number | null;
  gross_pnl_pips?: number | null;
  cost_pips?: number;
  net_pnl_pips?: number | null;
  qty?: number;
}

export interface TradePairLeg {
  side: "long" | "short";
  role: "primary" | "hedge" | "unknown";
  status: "open" | "closed";
  exit: number | null;
  exit_ts: string | null;
  pnl_pips: number;
  pnl_dollars: number | null;
  mae_pips: number;
  mfe_pips: number;
  mae_dollars: number | null;
  mfe_dollars: number | null;
  bucket: "win" | "be" | "loss" | null;
  reason: string | null;
  gross_pnl_pips?: number | null;
  cost_pips?: number;
  net_pnl_pips?: number | null;
  qty?: number;
}

export interface TradePairResult {
  id: string;
  session: string;
  entry: number;
  entry_ts: string;
  qty?: number;
  initial_risk_pct?: number | null;
  initial_risk_cash?: number | null;
  status: "open" | "partial" | "closed";
  primary: TradePairLeg | null;
  hedge: TradePairLeg | null;
  unknown_legs: TradePairLeg[];
  pnl_pips: number;
  pnl_dollars: number | null;
  gross_pnl_pips?: number | null;
  cost_pips?: number;
  net_pnl_pips?: number | null;
  entry_mode?: EntryMode;
  reentry_index?: number;
  entry_gap?: boolean;
  exit_gap?: boolean;
  same_bar_resolved?: boolean;
  stop_pips?: number | null;
  gross_r?: number | null;
  cost_r?: number | null;
  net_r?: number | null;
  hold_hours?: number | null;
  weekday?: string | null;
}

export interface EngineEvent {
  kind:
    | "signal"
    | "entry"
    | "entry_order_staged"
    | "entry_order_cancelled"
    | "hedge_staged"
    | "lock"
    | "exit"
    | "signal_skipped_anchor_drift"
    | "bar_skipped_invalid"
    | "signal_suppressed_risk"
    | "prop_guard_breached";
  session: string;
  ts: string;
  detail: Record<string, unknown>;
}

export interface SessionAnchorStats {
  session: string;
  skip_count: number;
  signal_count: number;
  anchor_drift_p50: number | null;
  anchor_drift_max: number | null;
  anchor_drift_minutes: number[];
  same_bar_resolution_rate: number;
  same_bar_r: number;
}

export interface BacktestRequest {
  symbol: string;
  timeframe: Timeframe;
  date_from?: string | null;
  date_to?: string | null;
  source?: CandleSource | null;
  entry_mode?: EntryMode | null;
  hedge_ratio_initial?: number | null;
  hedge_trigger_mode?: "failure_zone" | null;
  hedge_failure_k?: number | null;
  hedge_ratio_staged?: number | null;
  oco_buffer_mode?: OcoBufferMode | null;
  oco_buffer_value?: number | null;
  oco_expiry_bars?: number | null;
  allow_reentry?: boolean | null;
  lock_pips?: number | null;
  lock_mode?: "absolute" | "none" | "breakeven" | "r_relative" | null;
  lock_r?: number | null;
  stop_mode?: StopMode | null;
  sl_mult?: number | null;
  fixed_stop_pips?: number | null;
  rr?: number | null;
  min_stop_pips?: number | null;
  min_stop_cost_mult?: number | null;
  qty?: number | null;
  sessions?: string[] | null;
  performance_unit?: PerformanceUnit | null;
  dollars_per_pip_per_qty?: number | null;
  orb_minutes?: number | null;
  entry_delay_minutes?: number | null;
  anchor_tolerance_minutes?: number | null;
  risk_mode?: "fixed_qty" | "fixed_fractional" | null;
  risk_pct_per_r?: number | null;
  max_pair_risk_pct?: number | null;
  max_open_risk_pct?: number | null;
  max_concurrent_structures?: number | null;
  one_open_per_session?: boolean | null;
  firm_profile?: "none" | "custom" | null;
  firm_initial_balance?: number | null;
  firm_daily_loss_limit_pct?: number | null;
  firm_total_loss_limit_pct?: number | null;
  firm_timezone?: string | null;
  firm_daily_reset_time?: string | null;
  time_exit_mode?: "none" | "max_age" | null;
  max_age_hours?: number | null;
}

/** Every additive metric of a run, restated once in the unit the client asked for. */
export interface PerformanceView {
  unit: PerformanceUnit;
  dollars_per_pip_per_qty: number | null;
  qty_ref: number;
  conversion_factor: number;
  unit_label: string;
  realized: number;
  unrealized: number;
  equity: number;
  gross_realized: number;
  realized_cost: number;
  net_realized: number;
  gross_unrealized: number;
  unrealized_cost: number;
  net_unrealized: number;
  gross_equity: number;
  equity_cost: number;
  net_equity: number;
  execution_cost: number;
  financing_cost: number;
  max_drawdown: number;
  gross_max_drawdown: number;
  net_max_drawdown: number;
  breakeven_per_completed_side: number | null;
  configured_spread_per_side: number;
  configured_execution_cost_per_side: number;
}

export interface ComparisonPerformanceView {
  unit: PerformanceUnit;
  dollars_per_pip_per_qty: number | null;
  conversion_factor: number;
  unit_label: string;
  gross: number;
  net: number;
  execution_cost: number;
  financing_cost: number;
  total_cost: number;
  gross_expectancy: number | null;
  net_expectancy: number | null;
  gross_max_drawdown: number;
  net_max_drawdown: number;
  breakeven_per_completed_side: number | null;
}

export interface BacktestReportHeader {
  entry_mode: EntryMode;
  session_anchors: string[];
  stop_mode: StopMode;
  tp_mode: TpMode;
  rr: number;
  partial_tp_r?: number;
  partial_fraction?: number;
  lock_mode: "absolute" | "none" | "breakeven" | "r_relative";
  lock_pips: number;
  lock_r?: number;
  min_stop_pips?: number;
  min_stop_cost_mult?: number;
  derived_min_stop_pips?: number | null;
  filter_d1_ema50?: boolean;
  filter_nr7?: boolean;
  filter_orb_atr_min?: number;
  filter_orb_atr_max?: number;
  time_exit_mode: "none" | "max_age";
  max_age_hours: number;
  risk_mode: "fixed_qty" | "fixed_fractional";
  cost_model: "none" | "per_session";
  intrabar_mode: "optimistic" | "pessimistic" | "m1" | "m1_conservative" | "tick";
  resolver_tier: number;
  qty_ref: number;
  firm_profile: "none" | "custom";
  firm_profile_name?: string;
  firm_profile_version?: string | null;
  first_bar_ts: string | null;
  last_bar_ts: string | null;
  warmup_bars: number;
  validation_summary: Record<string, number>;
  m1_bars_loaded: number;
  m1_resolver_calls?: number;
  m1_covered_resolver_calls?: number;
  m1_partial_coverage_count?: number;
  m1_fallback_count: number;
}

export interface EquityCurvePoint {
  ts: string;
  net_equity: number;
  net_drawdown: number;
}

export interface BacktestReport {
  symbol: string;
  timeframe: Timeframe;
  source: CandleSource;
  bar_count: number;
  performance_unit: PerformanceUnit;
  performance: PerformanceView;
  report_header: BacktestReportHeader;
  entry_mode: EntryMode;
  orb_minutes: number;
  entry_delay_minutes: number;
  anchor_tolerance_minutes: number;
  stop_mode: StopMode;
  fixed_stop_pips: number;
  realized: number;
  unrealized: number;
  equity: number;
  realized_pips: number;
  unrealized_pips: number;
  realized_r: number;
  unrealized_r: number;
  equity_pips: number;
  max_drawdown_pips: number;
  max_drawdown_r: number;
  gross_max_drawdown_pips?: number;
  net_max_drawdown_pips?: number;
  gross_max_drawdown_r?: number;
  net_max_drawdown_r?: number;
  gross_realized_pips?: number;
  realized_cost_pips?: number;
  net_realized_pips?: number;
  gross_unrealized_pips?: number;
  unrealized_cost_pips?: number;
  net_unrealized_pips?: number;
  gross_equity_pips?: number;
  equity_cost_pips?: number;
  net_equity_pips?: number;
  gross_realized_r?: number;
  realized_cost_r?: number;
  net_realized_r?: number;
  gross_unrealized_r?: number;
  unrealized_cost_r?: number;
  net_unrealized_r?: number;
  gross_equity_r?: number;
  equity_cost_r?: number;
  net_equity_r?: number;
  execution_cost_pips?: number;
  financing_cost_pips?: number;
  transaction_sides?: number;
  completed_transaction_sides?: number;
  cost_side_equivalents?: number;
  completed_cost_side_equivalents?: number;
  breakeven_pips_per_side?: number | null;
  configured_spread_pips_per_side?: number;
  configured_execution_cost_pips_per_side?: number;
  cost_headroom_ratio?: number | null;
  risk_mode?: "fixed_qty" | "fixed_fractional";
  suppressed_signal_count?: number;
  suppressed_signal_reasons?: Record<string, number>;
  trades_skipped_by_filter?: number;
  firm_profile?: "none" | "custom";
  firm_profile_name?: string;
  firm_profile_version?: string | null;
  prop_guard_breached?: boolean;
  prop_guard_breach_reason?: string | null;
  prop_guard_breached_at?: string | null;
  prop_guard_daily_reference_equity?: number | null;
  prop_guard_last_equity_cash?: number | null;
  time_exit_mode?: "none" | "max_age";
  max_age_hours?: number;
  realized_dollars: number | null;
  unrealized_dollars: number | null;
  equity_dollars: number | null;
  max_drawdown_dollars: number | null;
  long_wins: number;
  long_be: number;
  long_loss: number;
  short_wins: number;
  short_be: number;
  short_loss: number;
  locks: number;
  open_pairs: number;
  pending_entry_orders?: number;
  unresolved_structures?: number;
  session_anchor_stats: SessionAnchorStats[];
  same_bar_resolution_rate: number;
  same_bar_r: number;
  survivor_tp_rate: number | null;
  mean_loss_r: number | null;
  breakeven_tp_rate_required: number | null;
  tp_rate_margin_pp: number | null;
  tp_rate_margin_pp_ci_low: number | null;
  tp_rate_margin_pp_ci_high: number | null;
  outcome_mix: {
    tp: number;
    lock: number;
    breakeven: number;
    whipsaw: number;
    time_exit?: number;
  };
  max_concurrent_structures: number;
  median_concurrent: number | null;
  win_rate?: number | null;
  win_rate_excl_be?: number | null;
  median_hold_hours?: number | null;
  p95_hold_hours?: number | null;
  equity_curve: EquityCurvePoint[];
  trades: ClosedLeg[];
  trade_pairs: TradePairResult[];
  events: EngineEvent[];
}

export interface EntryModeComparisonRow {
  entry_mode: EntryMode;
  performance: ComparisonPerformanceView;
  completed_structures: number;
  gross_pips: number;
  net_pips: number;
  gross_r: number;
  net_r: number;
  execution_cost_pips: number;
  financing_cost_pips: number;
  total_cost_pips: number;
  gross_expectancy_pips: number | null;
  net_expectancy_pips: number | null;
  gross_expectancy_r: number | null;
  net_expectancy_r: number | null;
  gross_profit_factor: number | null;
  net_profit_factor: number | null;
  gross_win_rate?: number | null;
  net_win_rate?: number | null;
  gross_win_rate_excl_be: number | null;
  net_win_rate_excl_be: number | null;
  survivor_tp_rate: number | null;
  breakeven_tp_rate_required: number | null;
  gross_max_drawdown_pips: number;
  net_max_drawdown_pips: number;
  gross_max_drawdown_r: number;
  net_max_drawdown_r: number;
  breakeven_pips_per_completed_side: number | null;
  transaction_sides: number;
  cost_side_equivalents: number;
  entry_fill_sides: number;
  exit_fill_sides: number;
  cancelled_entry_orders: number;
  expired_entry_orders: number;
  median_hold_hours: number | null;
  p95_hold_hours: number | null;
  max_concurrent_structures: number;
  suppressed_signals: number;
  unresolved_structures: number;
  prop_guard_breached: boolean;
  prop_guard_breach_reason: string | null;
  prop_guard_breached_at: string | null;
  prop_guard_breach_events: number;
}

export interface HedgeSyntheticAttribution {
  basis: "hedge_pair_minus_synthetic_breakout";
  gross_difference_pips: number;
  gap_effect_pips: number;
  same_bar_effect_pips: number;
  gross_payoff_effect_pips: number;
  execution_cost_difference_pips: number;
  financing_cost_difference_pips: number;
  total_cost_difference_pips: number;
  net_difference_pips: number;
  reconciliation_error_pips: number;
  gross_difference_r: number;
  gap_effect_r: number;
  same_bar_effect_r: number;
  gross_payoff_effect_r: number;
  total_cost_difference_r: number;
  net_difference_r: number;
  reconciliation_error_r: number;
  hedge_gap_tagged_structures: number;
  synthetic_gap_tagged_structures: number;
  hedge_same_bar_tagged_structures: number;
  synthetic_same_bar_tagged_structures: number;
  hedge_entry_fill_sides: number;
  hedge_exit_fill_sides: number;
  synthetic_entry_fill_sides: number;
  synthetic_exit_fill_sides: number;
}

export interface EntryModeComparisonReport {
  symbol: string;
  timeframe: Timeframe;
  source: CandleSource;
  bar_count: number;
  first_bar_ts: string;
  last_bar_ts: string;
  candle_set_sha256: string;
  shared_params: Record<string, unknown>;
  rows: EntryModeComparisonRow[];
  hedge_vs_synthetic: HedgeSyntheticAttribution;
}

export const SESSION_LABEL: Record<string, string> = {
  tokyo: "Tokyo",
  london: "London",
  new_york: "New York",
};

export const SESSION_COLOR: Record<string, string> = {
  tokyo: "#ffffff",
  london: "#c8c8c8",
  new_york: "#8a8a8a",
};

export interface PercentileDistribution {
  min: number;
  p01: number;
  p05: number;
  p50: number;
  p95: number;
  p99: number;
  max: number;
  mean: number;
}

export interface S7BreachDays {
  limit_pct: number;
  breach_count: number;
  breach_probability: number;
  expected_days_to_breach_conditional: number | null;
  median_days_to_breach_conditional: number | null;
}

export interface S7ModePropPanel {
  entry_mode: EntryMode;
  complete_structure_count: number;
  cluster_count: number;
  worst_simulated_path_gross_pips: number;
  worst_simulated_path_net_pips: number;
  worst_simulated_path_gross_r: number;
  worst_simulated_path_net_r: number;
  daily_breach_days: Record<string, S7BreachDays>;
  total_breach_days: Record<string, S7BreachDays>;
  minimum_free_margin_pct_distribution: PercentileDistribution;
  headroom_path: PercentileDistribution;
}

export interface S7ResearchArtifact {
  source: {
    kind: "research_simulation";
    not_interactive_backtest: true;
    not_broker_fact: true;
    caveats: string[];
  };
  study: "s7_propguard_monte_carlo";
  seed: number;
  simulation_count_per_mode: number;
  horizon_days: number;
  candle_set_sha256: string;
  bar_count: number;
  modes: S7ModePropPanel[];
}
