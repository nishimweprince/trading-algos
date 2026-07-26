/**
 * Shared domain types. Kept dependency-free so every module can import them
 * without pulling in Solana SDKs. Richer types (transactions, keypairs) live
 * in the modules that own them.
 */

export type Mint = string; // base58 mint address
export type Address = string; // base58 pubkey

/** Which feed surfaced a graduation first. */
export type FeedSource = 'grpc' | 'helius-ws' | 'pumpportal';

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
  reason?: string;
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
  /** True when the accept depends on widened, experimental guardrail thresholds. */
  relaxedRisk?: boolean;
  /** Machine-readable flags for the widened thresholds used by this candidate. */
  relaxedReasons?: string[];
  /** Soft-score component deltas for strategy-week retuning (optional for older rows). */
  scoreComponents?: {
    baseline: number;
    authorities: number;
    cleanMint: number;
    socials: number;
    nameSymbol: number;
    rugcheck: number;
    momentum: number;
  };
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
  /** Dev wallet (pool coin_creator) — monitored for dev-dump while open. */
  creator?: Address;
  /** Base mint's token program (for ATA derivation). */
  baseIsToken2022?: boolean;
}

/** Position lifecycle FSM (Section 7.3). */
export type PositionState =
  | 'PENDING_ENTRY'
  | 'OPEN'
  | 'EXITING'
  | 'CLOSED'
  | 'FAILED';

export type ExitTrigger =
  | 'TAKE_PROFIT_0'
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

/**
 * Machine-readable discriminator for a post-accept entry block. `reason` alone
 * cannot distinguish "max concurrent positions" from a risk breaker — both are
 * CIRCUIT_BREAKER and differ only in free-text detail. The dry-run twin needs
 * the distinction to attribute opportunity cost, so it is typed here rather
 * than parsed out of prose.
 */
export type EntryVetoCode =
  | 'MAX_CONCURRENT'
  | 'MAX_RELAXED'
  | 'RISK_BREAKER'
  | 'KILL_SWITCH';

/**
 * What the LIVE leg did with a candidate whose dry-run twin was opened. Drives
 * the Δ cohorts: `entered` → full live-vs-dry comparison; everything else →
 * the twin's net PnL is measured opportunity cost.
 *
 * `unknown` is the default and is not hypothetical — `openLive` returns
 * silently with no bus event when the estimated entry price is invalid and on
 * the dedupe branch of `canStartEntry`. A growing `unknown` count is an
 * instrumentation gap and must be surfaced separately, never folded into
 * "blocked".
 */
export type LiveStatus =
  | 'entered'
  | 'pending_entry' // provisional; upgrades to entered / entry_failed
  | 'blocked_concurrent'
  | 'blocked_relaxed_cap'
  | 'blocked_breaker'
  | 'blocked_kill_switch'
  | 'entry_failed'
  | 'no_executor'
  | 'unknown';
