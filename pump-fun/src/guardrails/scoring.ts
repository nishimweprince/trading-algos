import type { Candidate } from '../enrichment/types.ts';

/**
 * Soft-signal scoring (Section 6.2). Advisory only — it gates position size and
 * the minimum-entry threshold but NEVER overrides a hard-fail. Range 0..100.
 *
 * v1 uses the signals available from current enrichment; bundler share, organic
 * buy/sell ratio, holder count, and bonding-curve fill speed arrive with richer
 * enrichment and will slot in here. Kept deliberately transparent for tuning.
 */
export interface SoftSignals {
  score: number;
  highVolatility: boolean;
  /** baseSize multiplier derived from score (Section 6.2). */
  sizeMultiplier: number;
}

export function scoreCandidate(candidate: Candidate): SoftSignals {
  const e = candidate.enrichment;
  let score = 40; // neutral baseline

  if (e.mintInfo) {
    if (e.mintInfo.mintAuthority === null && e.mintInfo.freezeAuthority === null) score += 15;
    if (!e.mintInfo.isToken2022 || e.mintInfo.extensions.length === 0) score += 10;
  }
  if (e.metadata) {
    if (e.metadata.hasSocials) score += 10;
    if (e.metadata.name && e.metadata.symbol) score += 5;
  }
  if (typeof e.rugcheckScore === 'number') {
    // Capped at 15 points of influence (Section 6.2).
    score += Math.round((clamp01(e.rugcheckScore / 100) - 0.5) * 30);
  }

  score = Math.max(0, Math.min(100, score));
  return { score, highVolatility: false, sizeMultiplier: sizeMultiplierFor(score) };
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
