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
export interface ScoreComponents {
  baseline: number;
  authorities: number;
  cleanMint: number;
  socials: number;
  nameSymbol: number;
  rugcheck: number;
  momentum: number;
  tokenAge: number;
}

export interface SoftSignals {
  score: number;
  highVolatility: boolean;
  /** baseSize multiplier derived from score (Section 6.2). */
  sizeMultiplier: number;
  /** Transparent deltas for strategy-week retuning. */
  components: ScoreComponents;
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

/** Tunables for the token-age advisory penalty (from config.guardrails). */
export interface TokenAgeScoringOpts {
  /** Mint age (ms) at/under which there is no penalty. */
  freshMs: number;
  /** Mint age (ms) at/beyond which the max penalty applies (linear ramp from freshMs). */
  staleMs: number;
  /** Score points subtracted at/beyond staleMs. */
  maxPenalty: number;
}

export const DEFAULT_TOKEN_AGE_OPTS: TokenAgeScoringOpts = {
  freshMs: 60 * 60_000, // 1h
  staleMs: 24 * 60 * 60_000, // 24h
  maxPenalty: 20,
};

export function scoreCandidate(
  candidate: Candidate,
  momentum: MomentumScoringOpts = DEFAULT_MOMENTUM_OPTS,
  tokenAge: TokenAgeScoringOpts = DEFAULT_TOKEN_AGE_OPTS,
): SoftSignals {
  const e = candidate.enrichment;
  const components: ScoreComponents = {
    baseline: 40,
    authorities: 0,
    cleanMint: 0,
    socials: 0,
    nameSymbol: 0,
    rugcheck: 0,
    momentum: 0,
    tokenAge: 0,
  };
  let highVolatility = false;

  if (e.mintInfo) {
    if (e.mintInfo.mintAuthority === null && e.mintInfo.freezeAuthority === null) {
      components.authorities = 15;
    }
    // "Clean mint" bonus keys off *rug* extensions, not any extension — pump.fun
    // issues Token-2022 tokens with benign metadata extensions (validated live).
    if (!hasRugExtensions(e.mintInfo.extensions)) {
      components.cleanMint = 10;
    }
  }
  if (e.metadata) {
    if (e.metadata.hasSocials) components.socials = 10;
    if (e.metadata.name && e.metadata.symbol) components.nameSymbol = 5;
  }
  if (typeof e.rugcheckScore === 'number') {
    // Capped at 15 points of influence (Section 6.2).
    components.rugcheck = Math.round((clamp01(e.rugcheckScore / 100) - 0.5) * 30);
  }

  // Coin-age penalty: a real bonding curve graduates once, near its own
  // creation — a "graduation" for a mint created long ago is a red flag
  // (a stale/misattributed detection, not fresh momentum), so it's penalized
  // rather than hard-vetoed (the fetch is a third-party API, not on-chain —
  // Section 13). Ramps linearly from 0 at freshMs to -maxPenalty at staleMs.
  if (typeof e.tokenAgeMs === 'number' && tokenAge.staleMs > tokenAge.freshMs) {
    const frac = clamp01((e.tokenAgeMs - tokenAge.freshMs) / (tokenAge.staleMs - tokenAge.freshMs));
    components.tokenAge = frac > 0 ? -Math.round(frac * tokenAge.maxPenalty) : 0;
  }

  // Early-flow momentum: net SOL inflow over the first seconds post-graduation.
  // Scaled linearly to ±maxScoreBonus; a fast inflow rate flags high volatility,
  // which tightens the trailing stop downstream (exits/engine.ts).
  if (e.earlyFlow && momentum.strongInflowSol > 0) {
    const frac = clampSigned(e.earlyFlow.netInflowSol / momentum.strongInflowSol);
    components.momentum = Math.round(frac * momentum.maxScoreBonus);
    if (e.earlyFlow.inflowRateSolPerSec >= momentum.highVolInflowRateSolPerSec) {
      highVolatility = true;
    }
  }

  let score =
    components.baseline +
    components.authorities +
    components.cleanMint +
    components.socials +
    components.nameSymbol +
    components.rugcheck +
    components.momentum +
    components.tokenAge;
  score = Math.max(0, Math.min(100, score));
  return { score, highVolatility, sizeMultiplier: sizeMultiplierFor(score), components };
}

/**
 * Momentum-driven size factor (Round 3). Scales position size by early net SOL
 * inflow — the one feature that separates winners from craters. Maps inflow to
 * [floor, 1.0]: <=0 inflow → floor (min conviction), >= fullInflowSol → 1.0.
 * Applied as a MULTIPLIER on the score-based size, so it never gates an entry
 * (volume preserved) — it only shrinks low-conviction positions.
 */
export function momentumSizeFactor(
  netInflowSol: number,
  fullInflowSol: number,
  floor: number,
): number {
  if (fullInflowSol <= 0) return 1;
  const frac = clamp01(netInflowSol / fullInflowSol);
  return floor + frac * (1 - floor);
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
