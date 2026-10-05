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
  /**
   * SOL actually in the quote vault. A sell can never take out more than
   * this: once liquidity is pulled the virtual quote still props up the mid
   * price, but there is nothing to sell into. Absent = uncapped.
   */
  realQuoteSol?: number;
}

/**
 * Paper round trip of `sizeSol` through a PumpSwap pool: buy at `entry`
 * (constant-product impact on the entry depth, tiered fee by mcap), hold,
 * sell at `exit` (impact on the exit depth, tiered fee). Percent of size;
 * null on unusable marks.
 */
export function paperRoundTripPct(entry: PoolMark, exit: PoolMark, sizeSol: number): number | null {
  return roundTripParts(entry, exit, sizeSol)?.pct ?? null;
}

export interface RoundTripParts {
  pct: number;
  /** Volume-weighted buy fill vs the entry mid, % (fees excluded). */
  entryImpactPct: number;
  /** Sell proceeds vs the exit notional, % (fees excluded; pre liquidity-cap). */
  exitImpactPct: number;
}

/**
 * The round trip with its legs exposed, so the slippage gate (roundTripGate)
 * prices the identical trade the value leg scores. Same op order as the
 * historic computation — pct is bit-identical to the old inline math.
 */
export function roundTripParts(entry: PoolMark, exit: PoolMark, sizeSol: number): RoundTripParts | null {
  if (!(entry.price > 0) || !(exit.price > 0) || !(sizeSol > 0)) return null;
  const buyFee = feeBpsForMcap(mcapFromPrice(entry.price)) / 10_000;
  const sellFee = feeBpsForMcap(mcapFromPrice(exit.price)) / 10_000;
  const netIn = sizeSol * (1 - buyFee);
  const fill = buyFillPrice(entry.price, netIn, entry.quoteSol);
  const tokens = netIn / fill;
  const notional = tokens * exit.price;
  const gross = sellProceedsSol(notional, exit.quoteSol);
  let proceeds = gross * (1 - sellFee);
  if (exit.realQuoteSol !== undefined) proceeds = Math.min(proceeds, Math.max(0, exit.realQuoteSol));
  const pct = (proceeds / sizeSol - 1) * 100;
  if (!Number.isFinite(pct)) return null;
  return {
    pct,
    entryImpactPct: (fill / entry.price - 1) * 100,
    exitImpactPct: notional > 0 ? (1 - gross / notional) * 100 : 0,
  };
}

export type GateReason = 'entry-slippage' | 'exit-slippage';

/**
 * Would the live bot fill this simulated trade? The entry leg must fit the
 * loosest live buy bound and the exit leg the worst ordinary ladder tier —
 * both measured pre-fee, like the on-chain slippage check. Undefined caps
 * disable that side. Null parts (unusable marks) are unpriced, not gated.
 */
export function roundTripGate(
  parts: RoundTripParts | null,
  maxEntrySlippagePct?: number,
  maxExitSlippagePct?: number,
): GateReason | null {
  if (!parts) return null;
  if (maxEntrySlippagePct !== undefined && parts.entryImpactPct > maxEntrySlippagePct) return 'entry-slippage';
  if (maxExitSlippagePct !== undefined && parts.exitImpactPct > maxExitSlippagePct) return 'exit-slippage';
  return null;
}
