import type { GraduationEvent } from '../core/types.ts';
import type { MintInfo } from './mint.ts';
import type { PoolInfo } from './pool.ts';
import type { EarlyFlow } from './momentum.ts';
import type { ManipulationFeatures } from './features/types.ts';

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
  /**
   * Combined share of the top-10 holders (raw — pool vault NOT excluded). For
   * the H5 / persisted definition use `effectiveHolderShares` (holderShares.ts).
   */
  top10Share: number;
  /** Largest single holder share (raw — usually the pool base vault). */
  maxShare: number;
}

export interface TokenMetadata {
  name?: string;
  symbol?: string;
  /** DAS content.metadata.description, trimmed, <= 500 chars (metadata battery input). */
  description?: string;
  hasSocials: boolean;
  links?: Record<string, string>;
}

/**
 * Everything gathered about a graduation (Section 5). The fast path fills
 * `pool`, `mintInfo` and `creatorHolding`; the rest is background research
 * data that lands after the verdict and never gates it.
 */
export interface EnrichmentData {
  mintInfo?: MintInfo;
  pool?: PoolInfo;
  holders?: HolderSnapshot;
  metadata?: TokenMetadata;
  /**
   * Mint/freeze authorities indexed by DAS (from the same getAsset call as
   * metadata — no extra RPC). Backstop for H1/H2 when the direct mint-account
   * read fails. A field set to null means DAS explicitly reports no authority
   * (revoked); an absent field means DAS said nothing — still unknown.
   */
  dasAuthorities?: { mintAuthority?: string | null; freezeAuthority?: string | null };
  /**
   * Verified Metaplex creator addresses from DAS (same call, no extra RPC).
   * Identity backstop for creator checks (H6/H8) when the pool read fails.
   * Often empty for pump.fun mints — absent then.
   */
  dasCreators?: string[];
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
  /** Manipulation & population features (P3.3). */
  features?: ManipulationFeatures;
  /**
   * Creator's share of supply at graduation, from the fast read's creator ATA
   * (H6). Present only when the launch feed named the creator and it is the
   * pool's coin_creator.
   */
  creatorHolding?: { creator: string; share: number };
  /** Per-phase screening wall-clock (persisted as features_json.timings). */
  timings?: ScreenTimings;
  /** Fields whose fetch failed or timed out, by key. */
  unknowns: string[];
  /** Wall-clock spent enriching, ms. */
  elapsedMs: number;
}

/**
 * Where screening time went, ms. `fastReadMs` / `verdictMs` are the hot path;
 * everything else is the background research enrichment, which runs after
 * the verdict (and any buy) and overlaps itself.
 */
export interface ScreenTimings {
  /** The single batched account read (FastPoolReader). */
  fastReadMs?: number;
  /** Batched reads it took (retries while the RPC node lags the feed). */
  fastReadAttempts?: number;
  /** Where the pool reserves came from: the vaults, or the migrate tx. */
  reservesFrom?: 'rpc' | 'tx' | 'rpc_reread';
  /** Screen start → verdict emitted (and openPosition, when accepted). */
  verdictMs?: number;
  /** Background: Enricher.enrich(). */
  enrichMs?: number;
  /** Early-flow sample, measured from screen start (it starts at graduation). */
  momentumMs?: number;
  /** FeatureEngine.compute() as a whole. */
  featuresMs?: number;
  /** Curve signature paging inside the features phase. */
  curvePagingMs?: number;
  /** Creator funding-cluster lookup inside the features phase. */
  clusterMs?: number;
  /** Background: screen start → research data complete. */
  totalMs?: number;
  /** The pool's quote vault read under 1 SOL and was read again (PoolInfo.reread). */
  poolReread?: boolean;
}

export interface Candidate {
  graduation: GraduationEvent;
  enrichment: EnrichmentData;
}
