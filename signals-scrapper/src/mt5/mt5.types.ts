export type Mt5ExecutionStatus =
  | 'pending'
  | 'submitting'
  | 'succeeded'
  | 'rejected'
  | 'unknown'
  | 'blocked'
  | 'skipped';

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
