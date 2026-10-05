/**
 * Racer economics (pure). Answers the spec's gate — "the watcher only fires
 * when the remaining buy costs less than the expected payout" — from verified
 * bonding-curve reserves. R=0 per the verification verdict, so the gate prices
 * in SOL against a configured ceiling; the dry-run measures timing, not profit.
 */
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import type { CurveReserves } from '../enrichment/curve.ts';

/**
 * SOL cost to buy ALL remaining real tokens off the curve (constant-product
 * drain on virtual reserves: k = vT·vS, cost = k/(vT − real) − vS).
 * Null when the curve shape is unbuyable or unpriced. A complete/empty curve
 * costs 0 — nothing left to buy.
 */
export function completionCostSol(r: CurveReserves): number | null {
  if (r.complete || r.realTokenReserves <= 0n) return 0;
  if (r.virtualTokenReserves <= 0n || r.virtualSolReserves <= 0n) return null;
  if (r.virtualTokenReserves <= r.realTokenReserves) return null;
  const k = Number(r.virtualTokenReserves) * Number(r.virtualSolReserves);
  const remaining = Number(r.virtualTokenReserves - r.realTokenReserves);
  const costLamports = k / remaining - Number(r.virtualSolReserves);
  if (!Number.isFinite(costLamports) || costLamports < 0) return null;
  return costLamports / LAMPORTS_PER_SOL;
}

/** Curve progress toward graduation as a fraction of real SOL collected (pump.fun graduates at ~85 SOL). */
export function curveProgress(r: CurveReserves, graduationSol = 85): number {
  if (r.complete) return 1;
  return Math.min(1, Number(r.realSolReserves) / LAMPORTS_PER_SOL / graduationSol);
}

export type FireReason = 'no-curve' | 'complete' | 'unpriced' | 'too-expensive' | 'fire';

export interface FireDecision {
  fire: boolean;
  reason: FireReason;
  costSol: number | null;
}

export function evaluateFire(reserves: CurveReserves | null, maxCostSol: number): FireDecision {
  if (!reserves) return { fire: false, reason: 'no-curve', costSol: null };
  if (reserves.complete) return { fire: false, reason: 'complete', costSol: 0 };
  const costSol = completionCostSol(reserves);
  if (costSol === null) return { fire: false, reason: 'unpriced', costSol: null };
  if (costSol > maxCostSol) return { fire: false, reason: 'too-expensive', costSol };
  return { fire: true, reason: 'fire', costSol };
}

/** Paper PnL in percent for a first-position entry held to a horizon price, net of a fee fraction. */
export function paperPnlPct(entryPrice: number, horizonPrice: number, feeFraction: number): number | null {
  if (!(entryPrice > 0) || !(horizonPrice > 0)) return null;
  return ((horizonPrice / entryPrice) * (1 - feeFraction) - 1) * 100;
}
