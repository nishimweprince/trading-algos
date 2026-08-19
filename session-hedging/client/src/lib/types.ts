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
}

export interface EngineEvent {
  kind: "signal" | "entry" | "lock" | "exit";
  session: string;
  ts: string;
  detail: Record<string, unknown>;
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
}

export interface BacktestReport {
  symbol: string;
  timeframe: Timeframe;
  source: CandleSource;
  bar_count: number;
  realized: number;
  unrealized: number;
  equity: number;
  long_wins: number;
  long_be: number;
  long_loss: number;
  short_wins: number;
  short_be: number;
  short_loss: number;
  locks: number;
  open_pairs: number;
  trades: ClosedLeg[];
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
