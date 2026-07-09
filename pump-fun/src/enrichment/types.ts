import type { GraduationEvent } from '../core/types.ts';
import type { MintInfo } from './mint.ts';
import type { PoolInfo } from './pool.ts';
import type { EarlyFlow } from './momentum.ts';

/**
 * A single enriched holder derived from getTokenLargestAccounts +
 * getMultipleAccounts (to resolve the owning wallet behind each token account).
 */
export interface HolderInfo {
  /** Token-account address. */
  account: string;
  /** Owning wallet, when resolved. */
  owner?: string;
  amount: bigint;
  /** amount / supply, 0..1. */
  share: number;
}

export interface HolderSnapshot {
  supply: bigint;
  decimals: number;
  holders: HolderInfo[];
  /** Combined share of the top-10 holders (raw — pool NOT yet excluded). */
  top10Share: number;
  /** Largest single holder share (raw). */
  maxShare: number;
}

export interface TokenMetadata {
  name?: string;
  symbol?: string;
  hasSocials: boolean;
  links?: Record<string, string>;
}

/**
 * Everything gathered about a graduation before screening (Section 5). Any field
 * may be absent if its fetch missed the enrichment budget — the guardrail engine
 * applies the unknowns policy (Section 6.3) per field.
 */
export interface EnrichmentData {
  mintInfo?: MintInfo;
  pool?: PoolInfo;
  holders?: HolderSnapshot;
  metadata?: TokenMetadata;
  /** RugCheck aggregate (0..100), advisory only. Absent when unconfigured/down. */
  rugcheckScore?: number;
  /**
   * Early post-graduation flow (net SOL inflow over the first few seconds).
   * Advisory soft signal (Section 6.2). Absent when sampling is disabled or the
   * pool/vault could not be re-read within the window.
   */
  earlyFlow?: EarlyFlow;
  /** Early-flow window selected for this candidate, ms. */
  momentumWindowMs?: number;
  /**
   * H4 sellability probe result (atomic buy+sell simulation). Present only in
   * dry-run/live with a funded wallet; the checkSellability hard check reads it.
   */
  sellable?: { status: 'pass' | 'fail' | 'unknown'; detail: string };
  /** Fields whose fetch failed or timed out, by key. */
  unknowns: string[];
  /** Wall-clock spent enriching, ms. */
  elapsedMs: number;
}

export interface Candidate {
  graduation: GraduationEvent;
  enrichment: EnrichmentData;
}
