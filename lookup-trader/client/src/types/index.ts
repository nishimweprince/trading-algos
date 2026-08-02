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
  created_at?: string | null;
}

export interface CompareContext {
  trend_state?: string;
  session?: string;
  atr_bucket?: string;
  rsi_band?: string;
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
}

export interface TradeSubmit {
  session_id?: string | null;
  symbol: string;
  timeframe: string;
  signal_ts: string;
  setup_id: string;
  side: number;
  entry: number;
  sl: number;
  tp: number;
  notes?: string | null;
  calendar_flag?: boolean | null;
  calendar_tags?: string | null;
  observed_result?: string | null;
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
