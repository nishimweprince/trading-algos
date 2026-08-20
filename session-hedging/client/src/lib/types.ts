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

export interface ServiceConfig {
  symbol: string;
  timeframe: Timeframe;
  sessions: string[];
  lock_pips: number;
  sl_mult: number;
  rr: number;
  min_stop_pips: number;
  qty: number;
  pip_size: number;
  orb_minutes: number;
  entry_delay_minutes: number;
  anchor_tolerance_minutes: number;
  intrabar_mode: string;
  performance_unit: PerformanceUnit;
  dollars_per_pip_per_qty: number | null;
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
}

export interface TradePairResult {
  id: string;
  session: string;
  entry: number;
  entry_ts: string;
  status: "open" | "partial" | "closed";
  primary: TradePairLeg | null;
  hedge: TradePairLeg | null;
  unknown_legs: TradePairLeg[];
  pnl_pips: number;
  pnl_dollars: number | null;
}

export interface EngineEvent {
  kind:
    | "signal"
    | "entry"
    | "lock"
    | "exit"
    | "signal_skipped_anchor_drift"
    | "bar_skipped_invalid";
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
}

export interface BacktestRequest {
  symbol: string;
  timeframe: Timeframe;
  date_from?: string | null;
  date_to?: string | null;
  source?: CandleSource | null;
  lock_pips?: number | null;
  sl_mult?: number | null;
  rr?: number | null;
  min_stop_pips?: number | null;
  qty?: number | null;
  sessions?: string[] | null;
  performance_unit?: PerformanceUnit | null;
  orb_minutes?: number | null;
  entry_delay_minutes?: number | null;
  anchor_tolerance_minutes?: number | null;
}

export interface BacktestReport {
  symbol: string;
  timeframe: Timeframe;
  source: CandleSource;
  bar_count: number;
  performance_unit: PerformanceUnit;
  orb_minutes: number;
  entry_delay_minutes: number;
  anchor_tolerance_minutes: number;
  realized: number;
  unrealized: number;
  equity: number;
  realized_pips: number;
  unrealized_pips: number;
  max_drawdown_pips: number;
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
  session_anchor_stats: SessionAnchorStats[];
  same_bar_resolution_rate: number;
  same_bar_r: number;
  trades: ClosedLeg[];
  trade_pairs: TradePairResult[];
  events: EngineEvent[];
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
