/**
 * Racer economics (pure). Answers the spec's gate — "the watcher only fires
 * when the remaining buy costs less than the expected payout" — from verified
 * bonding-curve reserves. R=0 per the verification verdict, so the gate prices
 * in SOL against a configured ceiling; the dry-run measures timing, not profit.
 */
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import type { CurveReserves } from '../enrichment/curve.ts';
import { buyFillPrice, sellProceedsSol } from '../positions/ammImpact.ts';
import { feeBpsForMcap, mcapFromPrice } from '../positions/feeTiers.ts';

/**
 * SOL cost to buy ALL remaining real tokens off the curve (constant-product
 * drain on virtual reserves: k = vT·vS, cost = k/(vT − real) − vS), grossed
 * up by the curve's trade fee (`feeBps`, charged on top of the SOL in).
 * Null when the curve shape is unbuyable or unpriced. A complete/empty curve
 * costs 0 — nothing left to buy.
 */
export function completionCostSol(r: CurveReserves, feeBps = 0): number | null {
  if (r.complete || r.realTokenReserves <= 0n) return 0;
  if (r.virtualTokenReserves <= 0n || r.virtualSolReserves <= 0n) return null;
  if (r.virtualTokenReserves <= r.realTokenReserves) return null;
  const k = Number(r.virtualTokenReserves) * Number(r.virtualSolReserves);
  const remaining = Number(r.virtualTokenReserves - r.realTokenReserves);
  const costLamports = k / remaining - Number(r.virtualSolReserves);
  if (!Number.isFinite(costLamports) || costLamports < 0) return null;
  const fee = Math.min(Math.max(feeBps, 0), 9_999) / 10_000;
  return costLamports / (1 - fee) / LAMPORTS_PER_SOL;
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

export function evaluateFire(reserves: CurveReserves | null, maxCostSol: number, feeBps = 0): FireDecision {
  if (!reserves) return { fire: false, reason: 'no-curve', costSol: null };
  if (reserves.complete) return { fire: false, reason: 'complete', costSol: 0 };
  const costSol = completionCostSol(reserves, feeBps);
  if (costSol === null) return { fire: false, reason: 'unpriced', costSol: null };
  if (costSol > maxCostSol) return { fire: false, reason: 'too-expensive', costSol };
  return { fire: true, reason: 'fire', costSol };
}

/** A pool mark: mid price (SOL per whole token) and the effective quote depth (vault + virtual, SOL). */
export interface PoolMark {
  price: number;
  quoteSol: number;
}

/**
 * Paper round trip of `sizeSol` through a PumpSwap pool: buy at `entry`
 * (constant-product impact on the entry depth, tiered fee by mcap), hold,
 * sell at `exit` (impact on the exit depth, tiered fee). Percent of size;
 * null on unusable marks.
 */
export function paperRoundTripPct(entry: PoolMark, exit: PoolMark, sizeSol: number): number | null {
  if (!(entry.price > 0) || !(exit.price > 0) || !(sizeSol > 0)) return null;
  const buyFee = feeBpsForMcap(mcapFromPrice(entry.price)) / 10_000;
  const sellFee = feeBpsForMcap(mcapFromPrice(exit.price)) / 10_000;
  const netIn = sizeSol * (1 - buyFee);
  const fill = buyFillPrice(entry.price, netIn, entry.quoteSol);
  const tokens = netIn / fill;
  const proceeds = sellProceedsSol(tokens * exit.price, exit.quoteSol) * (1 - sellFee);
  const pct = (proceeds / sizeSol - 1) * 100;
  return Number.isFinite(pct) ? pct : null;
}
