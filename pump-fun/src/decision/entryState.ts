import { parseFeaturesJson, type FeatureInput } from '../research/featureSpec.ts';

/**
 * Compact entry state for the decision model: the numbers code already
 * computed at the decision instant, nothing the model has to derive and
 * nothing from after the decision (timestamp discipline).
 *
 * Built from the SAME FeatureInput the learned filter uses — live from the
 * pipeline, replay from a candidates row via research/dataset.ts
 * candidateFeatureInput — so a replayed state is byte-identical to what live
 * would have sent. Dense and numeric (billed per input token; target < 400
 * tokens), 3 significant figures, no addresses.
 */
export const ENTRY_STATE_VERSION = 1;

export interface EntryStateInput extends FeatureInput {
  softScore?: number | null;
  /** Hard-check id -> status (pass | fail | unknown). */
  checks?: Record<string, string> | null;
}

export function buildEntryState(x: EntryStateInput): Record<string, unknown> {
  const p = parseFeaturesJson(x.featuresJson);
  const tx = p.earlyFlow?.tx;
  const cleanSells = (p.earlyFlow?.txFlowVersion ?? 1) >= 2;
  const m = p.manipulation;
  return prune({
    v: ENTRY_STATE_VERSION,
    venue: 'pumpswap_fresh_graduation',
    pool: {
      sol: sig(x.poolSolAtEntry),
      mcapSol: sig(x.mcapSolAtEntry),
      moveSinceDetectPct: sig(x.poolMovePct),
    },
    flow: {
      windowS: x.momentumWindowMs == null ? null : sig(x.momentumWindowMs / 1000),
      netInflowSol: sig(x.earlyFlowNetSol),
      inflowSolPerS: sig(x.earlyFlowRate),
      buys: tx?.buyCount ?? null,
      uniqueBuyers: tx?.uniqueBuyers ?? null,
      buySol: sig(tx?.buySol),
      // v1 sell stats counted the migration tx as an ~85 SOL sell.
      sells: cleanSells ? (tx?.sellCount ?? null) : null,
      sellSol: cleanSells ? sig(tx?.sellSol) : null,
      maxSellSol: cleanSells ? sig(tx?.maxSellSol) : null,
    },
    holders: {
      top10Share: sig(x.top10Share),
      maxShare: sig(x.maxHolderShare),
      creatorShare: sig(x.creatorShare),
      freshRatio: sig(m?.holderQuality?.freshRatio),
    },
    launch: {
      mintAgeS: x.mintAgeMs == null ? null : sig(x.mintAgeMs / 1000),
      timeToGraduateS: m?.timeToGraduateMs == null ? null : sig(m.timeToGraduateMs / 1000),
      creatorLaunches7d: m?.cluster?.launches7d ?? null,
      hubFunder: m?.cluster?.hubFunder ?? null,
      bundleSharePct: sig(m?.curve?.bundleSharePct),
      washRatio: sig(m?.curve?.washRatio),
      creationSlotBuyers: m?.curve?.creationSlotBuyers ?? null,
      sniperBuyShare: sig(m?.snipers?.sniperBuyShare),
      copycat: m?.copycat?.isCopycat ?? null,
    },
    safety: {
      rugcheck: sig(x.rugcheckScore),
      hasSocials: x.hasSocials ?? null,
      sellability: x.sellabilityStatus ?? null,
      softScore: sig(x.softScore),
      checks: x.checks ?? null,
    },
  });
}

/** 3 significant figures; null for missing / non-finite. */
function sig(v: number | null | undefined): number | null {
  if (v === null || v === undefined || !Number.isFinite(v)) return null;
  if (v === 0) return 0;
  return Number(v.toPrecision(3));
}

/** Drop nulls and empty objects — absent fields cost nothing and read as "unknown". */
function prune(o: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) {
    if (v === null || v === undefined) continue;
    if (typeof v === 'object' && !Array.isArray(v)) {
      const inner = prune(v as Record<string, unknown>);
      if (Object.keys(inner).length > 0) out[k] = inner;
      continue;
    }
    out[k] = v;
  }
  return out;
}
