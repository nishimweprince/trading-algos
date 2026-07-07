/**
 * Shared domain types. Kept dependency-free so every module can import them
 * without pulling in Solana SDKs. Richer types (transactions, keypairs) live
 * in the modules that own them.
 */

export type Mint = string; // base58 mint address
export type Address = string; // base58 pubkey

/** Which feed surfaced a graduation first. */
export type FeedSource = 'grpc' | 'pumpportal';

/** Where the graduated liquidity landed. */
export type Venue = 'pumpswap' | 'raydium';

export interface GraduationEvent {
  mint: Mint;
  venue: Venue;
  poolAddress: Address;
  slot: number;
  feedSource: FeedSource;
  /** process.hrtime.bigint() at receipt, for latency accounting. */
  receivedAtNs: bigint;
  /** Detection latency vs slot time, filled once slot time is known. */
  detectionLatencyMs?: number;
}

/**
 * Raw graduation as surfaced by a detection feed, before dedupe / on-chain
 * confirmation. Feeds fill what they know; the detector confirms and enriches
 * into a full GraduationEvent.
 */
export interface FeedGraduation {
  mint: Mint;
  feedSource: FeedSource;
  /** process.hrtime.bigint() at message receipt. */
  receivedAtNs: bigint;
  venue?: Venue;
  poolAddress?: Address;
  /** Migration transaction signature, when the feed provides it. */
  signature?: string;
  slot?: number;
  /** Raw feed payload, retained for schema spot-checks / debugging. */
  raw?: unknown;
}

/** Guardrail verdict for a candidate (Section 6). */
export type CheckStatus = 'pass' | 'fail' | 'unknown';

export interface CheckResult {
  id: string; // e.g. "H1"
  label: string;
  status: CheckStatus;
  detail?: string;
}

export type Verdict = 'accept' | 'veto';

export interface CandidateVerdict {
  mint: Mint;
  verdict: Verdict;
  hardChecks: CheckResult[];
  softScore: number;
  vetoReasons: string[];
  /** Set when a soft signal warrants a tighter trailing stop. */
  highVolatility: boolean;
  /** baseSize multiplier from soft score (0 when below minEntryScore). */
  sizeMultiplier: number;
}

/**
 * Everything the pricing layer needs to compute a pool's local price from its
 * vault balances (Section 7.2). Passed on the openPosition event so the position
 * manager can price without re-fetching the pool.
 */
export interface PoolPricingRef {
  poolAddress: Address;
  baseMint: Mint;
  baseVault: Address;
  quoteVault: Address;
  baseDecimals: number;
  baseReserve: bigint;
  quoteReserveLamports: bigint;
}

/** Position lifecycle FSM (Section 7.3). */
export type PositionState =
  | 'PENDING_ENTRY'
  | 'OPEN'
  | 'EXITING'
  | 'CLOSED'
  | 'FAILED';

export type ExitTrigger =
  | 'TAKE_PROFIT_1'
  | 'TAKE_PROFIT_2'
  | 'TRAILING_STOP'
  | 'STOP_LOSS'
  | 'TIME_STOP'
  | 'EMERGENCY_EXIT'
  | 'KILL_SWITCH';

export interface Position {
  mint: Mint;
  state: PositionState;
  sizeSol: number;
  entryPrice?: number;
  openedAt?: number; // epoch ms
  closedAt?: number;
  exitTrigger?: ExitTrigger;
  pnlSol?: number;
  pnlPct?: number;
}

/** Reason an entry was blocked before capital was committed. */
export type VetoReason =
  | 'GUARDRAIL'
  | 'SLIPPAGE'
  | 'CIRCUIT_BREAKER'
  | 'STREAM_DOWN'
  | 'LOW_SCORE'
  | 'KILL_SWITCH';
