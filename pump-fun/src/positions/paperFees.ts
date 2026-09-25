import { LAMPORTS_PER_SOL } from '../core/constants.ts';

/**
 * Paper / shadow / twin fee drag (same formula the position manager uses for
 * non-live accounting). Priority per tx (entry + each exit fill), Jito tip per
 * tx when bundles are in use, and swap fee on both legs of the round-trip
 * notional.
 */
export function estimatePaperFees(
  sizeSol: number,
  exitFills: number,
  fees: { swapFeePct: number; estPriorityTipSolPerTx: number; jitoTipSolPerTx?: number },
): number {
  const txCount = 1 + Math.max(1, exitFills);
  const tip = (fees.estPriorityTipSolPerTx + (fees.jitoTipSolPerTx ?? 0)) * txCount;
  const swap = (fees.swapFeePct / 100) * sizeSol * 2; // entry + exit legs
  return tip + swap;
}

/** One swap leg for tiered paper fees: notional in SOL and its fee in bps. */
export interface FeeLeg {
  valueSol: number;
  feeBps: number;
}

/**
 * Tiered paper fee drag (work plan 2026-09-25 P1.1, F4). Same tx-cost terms
 * as estimatePaperFees, but each swap leg pays its own PumpSwap tier on its
 * own notional — the entry on the SOL spent, every exit on its proceeds —
 * instead of a flat % of the entry size on both legs.
 */
export function estimatePaperFeesTiered(args: {
  entry: FeeLeg;
  exits: readonly FeeLeg[];
  fees: { estPriorityTipSolPerTx: number; jitoTipSolPerTx?: number };
}): number {
  const txCount = 1 + Math.max(1, args.exits.length);
  const tip = (args.fees.estPriorityTipSolPerTx + (args.fees.jitoTipSolPerTx ?? 0)) * txCount;
  let swap = (args.entry.feeBps / 10_000) * args.entry.valueSol;
  for (const leg of args.exits) swap += (leg.feeBps / 10_000) * Math.max(0, leg.valueSol);
  return tip + swap;
}

/**
 * Constant-product price impact, in SOL, of buying `sizeSol` into a pool with
 * `quoteReserveLamports` of SOL. For x·y = k the tokens received are
 * X·s/(Y+s) against a mid of X·s/Y, so the value lost to impact is exactly
 * s · s/(Y+s). Zero when the reserve is unknown (0n), so injected test ticks
 * that carry no reserves are unaffected.
 */
export function buyImpactSol(sizeSol: number, quoteReserveLamports: bigint): number {
  if (!(sizeSol > 0) || quoteReserveLamports <= 0n) return 0;
  const y = Number(quoteReserveLamports) / LAMPORTS_PER_SOL;
  return sizeSol * (sizeSol / (y + sizeSol));
}

/**
 * Constant-product price impact, in SOL, of selling `tokensSold` whole tokens
 * into a pool holding `baseReserveWhole` whole tokens, where the fill is
 * worth `fillValueSol` at the tick mid. Selling dx yields Y·dx/(X+dx) against
 * a mid of Y·dx/X, so the impact fraction is dx/(X+dx). The swap fee is NOT
 * included — estimatePaperFees charges it.
 */
export function sellImpactSol(fillValueSol: number, tokensSold: number, baseReserveWhole: number): number {
  if (!(fillValueSol > 0) || !(tokensSold > 0) || !(baseReserveWhole > 0)) return 0;
  return fillValueSol * (tokensSold / (baseReserveWhole + tokensSold));
}

/** Whole-token pool reserve from raw units, for sellImpactSol. */
export function baseReserveWhole(baseReserve: bigint, baseDecimals: number): number {
  if (baseReserve <= 0n) return 0;
  return Number(baseReserve) / 10 ** baseDecimals;
}
