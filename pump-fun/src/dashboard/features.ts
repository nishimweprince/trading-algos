/**
 * Extract model-ready denormalized features from enrichment + scoring.
 */
import type { EnrichmentData } from '../enrichment/types.ts';
import type { SoftSignals, ScoreComponents } from '../guardrails/scoring.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';

export interface StrategyFeatures {
  earlyFlowNetSol: number | null;
  earlyFlowRate: number | null;
  poolSolAtEntry: number | null;
  buyImpactPct: number | null;
  top10Share: number | null;
  maxHolderShare: number | null;
  creatorShare: number | null;
  rugcheckScore: number | null;
  hasSocials: boolean | null;
  scoreComponents: ScoreComponents | null;
  sizeMultiplier: number | null;
  unknowns: string[];
  enrichmentMs: number | null;
  momentumWindowMs: number | null;
  relaxedRisk: boolean | null;
  relaxedReasonsJson: string | null;
  sellabilityReason: string | null;
}

export function extractStrategyFeatures(
  enrichment: EnrichmentData | null | undefined,
  soft?: SoftSignals | null,
  buyImpactPct?: number | null,
): StrategyFeatures {
  const e = enrichment;
  const poolSol =
    e?.pool?.quoteReserveLamports !== undefined
      ? Number(e.pool.quoteReserveLamports) / LAMPORTS_PER_SOL
      : null;

  return {
    earlyFlowNetSol: e?.earlyFlow ? e.earlyFlow.netInflowSol : null,
    earlyFlowRate: e?.earlyFlow ? e.earlyFlow.inflowRateSolPerSec : null,
    poolSolAtEntry: poolSol,
    buyImpactPct: buyImpactPct ?? null,
    top10Share: e?.holders ? e.holders.top10Share : null,
    maxHolderShare: e?.holders ? e.holders.maxShare : null,
    creatorShare: null, // filled by pipeline when known from checks
    rugcheckScore: typeof e?.rugcheckScore === 'number' ? e.rugcheckScore : null,
    hasSocials: e?.metadata ? e.metadata.hasSocials : null,
    scoreComponents: soft?.components ?? null,
    sizeMultiplier: soft?.sizeMultiplier ?? null,
    unknowns: e?.unknowns ?? [],
    enrichmentMs: e?.elapsedMs ?? null,
    momentumWindowMs: e?.momentumWindowMs ?? null,
    relaxedRisk: null,
    relaxedReasonsJson: null,
    sellabilityReason: e?.sellable?.reason ?? null,
  };
}

export function featuresToDbFields(f: StrategyFeatures): Record<string, unknown> {
  return {
    earlyFlowNetSol: f.earlyFlowNetSol,
    earlyFlowRate: f.earlyFlowRate,
    poolSolAtEntry: f.poolSolAtEntry,
    buyImpactPct: f.buyImpactPct,
    top10Share: f.top10Share,
    maxHolderShare: f.maxHolderShare,
    creatorShare: f.creatorShare,
    rugcheckScore: f.rugcheckScore,
    hasSocials: f.hasSocials === null ? null : f.hasSocials ? 1 : 0,
    scoreComponentsJson: f.scoreComponents ? JSON.stringify(f.scoreComponents) : null,
    sizeMultiplier: f.sizeMultiplier,
    unknownsJson: f.unknowns.length ? JSON.stringify(f.unknowns) : null,
    enrichmentMs: f.enrichmentMs,
    momentumWindowMs: f.momentumWindowMs,
    relaxedRisk: f.relaxedRisk,
    relaxedReasonsJson: f.relaxedReasonsJson,
    sellabilityReason: f.sellabilityReason,
  };
}
