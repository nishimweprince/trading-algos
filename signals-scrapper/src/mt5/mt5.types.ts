export type Mt5ExecutionStatus =
  | 'pending'
  | 'submitting'
  | 'succeeded'
  | 'rejected'
  | 'unknown'
  | 'blocked'
  | 'skipped';

export type Mt5SignalSource = 'trading_central' | 'autochartist';

/**
 * Mirror of SignalRequest in packages/ta-contracts/src/ta_contracts/signals.py,
 * served by services/execution-service (MT5 compat API, POST /v1/signals).
 * That Pydantic model is the source of truth (extra fields are forbidden).
 */
export interface Mt5SignalRequest {
  signal_id: string;
  occurred_at: string;
  execution_type: 'market';
  symbol: string;
  direction: 'buy' | 'sell';
  volume: string;
  stop_loss: string;
  take_profit: string;
  note: string;
  source: Mt5SignalSource;
  ignore_signal_age?: boolean;
}

export interface Mt5ExecutionError {
  code: string;
  message: string;
  details?: unknown;
}

export interface Mt5ExecutionRecord {
  signalId: string;
  ideaHash: string;
  status: Mt5ExecutionStatus;
  request?: Mt5SignalRequest;
  attempts: number;
  createdAt: string;
  updatedAt: string;
  lastAttemptAt?: string;
  response?: unknown;
  error?: Mt5ExecutionError;
}

export interface Mt5DeliverySummary {
  reconciled: number;
  submitted: number;
  succeeded: number;
  rejected: number;
  unknown: number;
  blocked: number;
  pending: number;
}
