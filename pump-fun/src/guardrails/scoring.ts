import type { Candidate } from '../enrichment/types.ts';
import { hasRugExtensions } from '../enrichment/mint.ts';

/**
 * Soft-signal scoring (Section 6.2). Advisory only — it gates position size and
 * the minimum-entry threshold but NEVER overrides a hard-fail. Range 0..100.
 *
 * Structural signals (authorities, clean mint, socials, RugCheck) come from
 * enrichment. Early post-graduation flow (net SOL inflow, exposed as
 * enrichment.earlyFlow) adds the strategy's key missing signal: is anyone
 * actually buying? Higher early net inflow → higher score; a fast inflow rate
 * additionally flags high volatility. Kept deliberately transparent for tuning.
 */
export interface SoftSignals {
  score: number;
  highVolatility: boolean;
  /** baseSize multiplier derived from score (Section 6.2). */
  sizeMultiplier: number;
}

/** Tunables for the early-flow momentum signal (from config.guardrails). */
export interface MomentumScoringOpts {
  /** Net SOL inflow at/above which the full bonus is awarded (symmetric penalty). */
  strongInflowSol: number;
  /** Max points the momentum signal may add (or subtract). */
  maxScoreBonus: number;
  /** Inflow rate (SOL/sec) at/above which highVolatility trips (tighter trail). */
  highVolInflowRateSolPerSec: number;
}

export const DEFAULT_MOMENTUM_OPTS: MomentumScoringOpts = {
  strongInflowSol: 10,
  maxScoreBonus: 15,
  highVolInflowRateSolPerSec: 2,
};

export function scoreCandidate(
  candidate: Candidate,
  momentum: MomentumScoringOpts = DEFAULT_MOMENTUM_OPTS,
): SoftSignals {
  const e = candidate.enrichment;
  let score = 40; // neutral baseline
  let highVolatility = false;

  if (e.mintInfo) {
    if (e.mintInfo.mintAuthority === null && e.mintInfo.freezeAuthority === null) score += 15;
    // "Clean mint" bonus keys off *rug* extensions, not any extension — pump.fun
    // issues Token-2022 tokens with benign metadata extensions (validated live).
    if (!hasRugExtensions(e.mintInfo.extensions)) score += 10;
  }
  if (e.metadata) {
    if (e.metadata.hasSocials) score += 10;
    if (e.metadata.name && e.metadata.symbol) score += 5;
  }
  if (typeof e.rugcheckScore === 'number') {
    // Capped at 15 points of influence (Section 6.2).
    score += Math.round((clamp01(e.rugcheckScore / 100) - 0.5) * 30);
  }

  // Early-flow momentum: net SOL inflow over the first seconds post-graduation.
  // Scaled linearly to ±maxScoreBonus; a fast inflow rate flags high volatility,
  // which tightens the trailing stop downstream (exits/engine.ts).
  if (e.earlyFlow && momentum.strongInflowSol > 0) {
    const frac = clampSigned(e.earlyFlow.netInflowSol / momentum.strongInflowSol);
    score += Math.round(frac * momentum.maxScoreBonus);
    if (e.earlyFlow.inflowRateSolPerSec >= momentum.highVolInflowRateSolPerSec) {
      highVolatility = true;
    }
  }

  score = Math.max(0, Math.min(100, score));
  return { score, highVolatility, sizeMultiplier: sizeMultiplierFor(score) };
}

/** 60 -> 0.5x, 80 -> 1.0x, 95+ -> 1.25x, below 60 -> 0 (won't enter). */
export function sizeMultiplierFor(score: number): number {
  if (score < 60) return 0;
  if (score < 80) return lerp(score, 60, 80, 0.5, 1.0);
  if (score < 95) return lerp(score, 80, 95, 1.0, 1.25);
  return 1.25;
}

function lerp(x: number, x0: number, x1: number, y0: number, y1: number): number {
  return y0 + ((x - x0) / (x1 - x0)) * (y1 - y0);
}

function clamp01(x: number): number {
  return Math.max(0, Math.min(1, x));
}

function clampSigned(x: number): number {
  return Math.max(-1, Math.min(1, x));
}
