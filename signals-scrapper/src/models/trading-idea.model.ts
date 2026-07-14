export type ProviderType = 'TRADING_CENTRAL' | 'AUTOCHARTIST';
export type Direction = 'UP' | 'DOWN' | 'NEUTRAL';

export interface TradingIdea {
  provider: ProviderType;
  instrument: string;
  timeframe: string;
  direction: Direction;
  /** Current/black chart marker. It is intentionally excluded from dedup hashes. */
  entry?: number | null;
  /** Trading Central "Pivot" compatibility alias. */
  stopLoss?: number;
  /** Trading Central "Target" compatibility alias. */
  takeProfit?: number;
  expectedMovePips?: [number, number];
  target?: number;
  pivot?: number;
  levels?: { support: number[]; resistance: number[] };
  ideaTimestamp: string | null;
  capturedAt: string;
  sourceUrl: string;
  screenshotPath?: string;
  raw?: unknown;
  hash: string;
}

/** Fields used to compute the stable dedup hash (before hash is assigned). */
export type IdeaHashInput = Pick<
  TradingIdea,
  | 'provider'
  | 'instrument'
  | 'timeframe'
  | 'ideaTimestamp'
  | 'direction'
  | 'target'
  | 'pivot'
  | 'stopLoss'
  | 'takeProfit'
>;
