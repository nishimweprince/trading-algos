/**
 * Venue-aware pre-graduation screening (S2, no capital).
 *
 * Pure functions over bonding-curve state — no RPC, no bus, no risk. The
 * post-graduation H-checks assume a PumpSwap pool and an indexed mint, both
 * absent pre-graduation, so the pre-grad lane screens on curve-native terms:
 *
 * - CURVE_ACTIVE replaces H3 (LP burned is meaningless pre-grad): the curve
 *   account must decode, hold liquidity, use the standard supply, and not be
 *   flagged complete (migrated/drained).
 * - CURVE_FLOOR / CURVE_EARLY replace H7 (liquidity floor): minimum SOL
 *   collected + minimum completion. Completion doubles as the volume control
 *   — late-curve-only keeps flow enrichable at ~17 launches/min.
 * - TOP10 / CREATOR mirror H5/H6 caps but only when holder data exists;
 *   missing holder data is recorded in `unknowns`, never a veto (S1 index-lag
 *   data later decides whether unknowns should veto).
 * - UNINDEXED_MINT mirrors H11: unindexed mints veto unless explicitly
 *   allowed, in which case the accept is tagged relaxed (blind entry).
 *
 * The H4 dynamic probe (curve buy/sell simulation) ships with S3: it needs
 * the same pump.fun instruction builder as curve execution, so it lands with
 * sim-proven buys rather than as a standalone S2 artifact.
 */

export interface PregradScreenInput {
  mint: string;
  /** Curve decoded and holding liquidity (not brand-new, not drained). */
  priced: boolean;
  /** Curve complete flag (migrated). */
  complete: boolean;
  tokenTotalSupply: bigint | null;
  realTokenReserves: bigint | null;
  /** SOL collected on the curve (curveRealSol). Null when unread. */
  realSol: number | null;
  /** Holder concentration when the mint is indexed; null when unknown. */
  top10Share: number | null;
  creatorShare: number | null;
  /** Mint account + metadata + age all indexed. */
  mintIndexed: boolean;
}

export interface PregradScreenConfig {
  minCompletionPct: number;
  minRealSol: number;
  maxTop10Pct: number;
  maxCreatorPct: number;
  allowUnindexed: boolean;
}

export interface PregradVerdict {
  verdict: 'accept' | 'veto';
  reasons: string[];
  unknowns: string[];
  completionPct: number | null;
  /** Accepted without indexed mint data (blind entry — size/position caps apply in S3). */
  relaxed: boolean;
}

/**
 * Real tokens initially on the curve (base units), the observed creation
 * constant (fresh mainnet curve 2026-09-18: exactly this value). Completion
 * is the fraction sold off: fresh ≈ 0%, graduated (real = 0) = 100%.
 */
export const CURVE_INITIAL_REAL_TOKENS = 793_100_000_000_000n;
/** Standard pump.fun total supply (base units). Nonstandard supply vetoes. */
export const CURVE_STANDARD_SUPPLY = 1_000_000_000_000_000n;

export function curveCompletionPct(realTokenReserves: bigint | null): number | null {
  if (realTokenReserves === null || realTokenReserves < 0n) return null;
  const pct = (1 - Number(realTokenReserves) / Number(CURVE_INITIAL_REAL_TOKENS)) * 100;
  if (!Number.isFinite(pct)) return null;
  return Math.min(100, Math.max(0, pct));
}

export function screenPregrad(input: PregradScreenInput, cfg: PregradScreenConfig): PregradVerdict {
  const reasons: string[] = [];
  const unknowns: string[] = [];

  if (input.complete) reasons.push('CURVE_COMPLETE');
  if (!input.priced) reasons.push('CURVE_UNPRICED');
  if (input.tokenTotalSupply !== null && input.tokenTotalSupply !== CURVE_STANDARD_SUPPLY) {
    reasons.push('CURVE_SUPPLY');
  }

  const completion = curveCompletionPct(input.realTokenReserves);
  if (completion === null) {
    reasons.push('CURVE_COMPLETION_UNKNOWN');
  } else {
    if (completion < cfg.minCompletionPct) reasons.push('CURVE_EARLY');
    if (input.realSol === null) {
      unknowns.push('realSol');
    } else if (input.realSol < cfg.minRealSol) {
      reasons.push('CURVE_FLOOR');
    }
  }

  if (input.top10Share === null) {
    unknowns.push('top10');
  } else if (input.top10Share > cfg.maxTop10Pct) {
    reasons.push('TOP10');
  }
  if (input.creatorShare === null) {
    unknowns.push('creator');
  } else if (input.creatorShare > cfg.maxCreatorPct) {
    reasons.push('CREATOR');
  }

  let relaxed = false;
  if (!input.mintIndexed) {
    if (cfg.allowUnindexed) relaxed = true;
    else reasons.push('UNINDEXED_MINT');
  }

  return {
    verdict: reasons.length === 0 ? 'accept' : 'veto',
    reasons,
    unknowns,
    completionPct: completion,
    relaxed,
  };
}
