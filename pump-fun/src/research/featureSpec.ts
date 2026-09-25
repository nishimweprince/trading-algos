/**
 * Feature vector for the learned filter (work plan 2026-09-25 P3.4).
 *
 * ONE definition used by both offline training (from candidates rows) and
 * live inference (from the same fields the pipeline persists), so a model can
 * never be trained on one encoding and served another. Every feature is
 * numeric; a missing value encodes as 0 plus an `is_missing` indicator so the
 * model learns what absence means (an unindexed mint is itself a signal).
 */
export interface FeatureInput {
  earlyFlowNetSol?: number | null;
  earlyFlowRate?: number | null;
  poolSolAtEntry?: number | null;
  top10Share?: number | null;
  maxHolderShare?: number | null;
  creatorShare?: number | null;
  rugcheckScore?: number | null;
  hasSocials?: boolean | null;
  mintAgeMs?: number | null;
  mcapSolAtEntry?: number | null;
  poolMovePct?: number | null;
  sellabilityStatus?: string | null;
  momentumWindowMs?: number | null;
  /** candidates.features_json (early-flow tx stats + manipulation features). */
  featuresJson?: string | null;
}

interface Parsed {
  earlyFlow?: { tx?: { buyCount?: number; sellCount?: number; uniqueBuyers?: number; maxSellSol?: number; buySol?: number; sellSol?: number } };
  manipulation?: {
    cluster?: { launches7d?: number; wallets?: number; hubFunder?: boolean };
    curve?: { bundleSharePct?: number | null; washRatio?: number | null; creationSlotBuyers?: number | null };
    timeToGraduateMs?: number | null;
    copycat?: { isCopycat?: boolean };
    snipers?: { sniperBuyShare?: number; knownSnipers?: number };
    holderQuality?: { freshRatio?: number };
  };
  confirm?: { netInflowSol?: number; priceUpPct?: number; maxSingleDropPct?: number };
}

type Extract = (x: FeatureInput, p: Parsed) => number | null | undefined;

const log1p = (v: number | null | undefined) => (v === null || v === undefined ? null : Math.log1p(Math.max(0, v)));
const b = (v: boolean | null | undefined) => (v === null || v === undefined ? null : v ? 1 : 0);

/** Name -> extractor. Order is the model's column order; append only. */
const SPEC: ReadonlyArray<[string, Extract]> = [
  ['early_flow_net_sol', (x) => x.earlyFlowNetSol],
  ['early_flow_rate', (x) => x.earlyFlowRate],
  ['pool_sol', (x) => x.poolSolAtEntry],
  ['top10_share', (x) => x.top10Share],
  ['max_holder_share', (x) => x.maxHolderShare],
  ['creator_share', (x) => x.creatorShare],
  ['rugcheck', (x) => x.rugcheckScore],
  ['has_socials', (x) => b(x.hasSocials)],
  ['log_mint_age_s', (x) => log1p(x.mintAgeMs == null ? null : x.mintAgeMs / 1000)],
  ['log_mcap_sol', (x) => log1p(x.mcapSolAtEntry)],
  ['pool_move_pct', (x) => x.poolMovePct],
  ['h4_pass', (x) => (x.sellabilityStatus == null ? null : x.sellabilityStatus === 'pass' ? 1 : 0)],
  ['momentum_window_s', (x) => (x.momentumWindowMs == null ? null : x.momentumWindowMs / 1000)],
  ['tx_buys', (_x, p) => p.earlyFlow?.tx?.buyCount],
  ['tx_sells', (_x, p) => p.earlyFlow?.tx?.sellCount],
  ['tx_unique_buyers', (_x, p) => p.earlyFlow?.tx?.uniqueBuyers],
  ['tx_max_sell_sol', (_x, p) => p.earlyFlow?.tx?.maxSellSol],
  ['cluster_launches_7d', (_x, p) => p.manipulation?.cluster?.launches7d],
  ['cluster_hub_funder', (_x, p) => b(p.manipulation?.cluster?.hubFunder)],
  ['bundle_share_pct', (_x, p) => p.manipulation?.curve?.bundleSharePct],
  ['wash_ratio', (_x, p) => p.manipulation?.curve?.washRatio],
  ['creation_slot_buyers', (_x, p) => p.manipulation?.curve?.creationSlotBuyers],
  ['log_time_to_grad_s', (_x, p) => log1p(p.manipulation?.timeToGraduateMs == null ? null : p.manipulation.timeToGraduateMs / 1000)],
  ['copycat', (_x, p) => b(p.manipulation?.copycat?.isCopycat)],
  ['sniper_buy_share', (_x, p) => p.manipulation?.snipers?.sniperBuyShare],
  ['fresh_holder_ratio', (_x, p) => p.manipulation?.holderQuality?.freshRatio],
];

/** Columns: every value feature followed by its missing indicator. */
export const FEATURE_NAMES: readonly string[] = SPEC.flatMap(([n]) => [n, `${n}__missing`]);

export function parseFeaturesJson(json: string | null | undefined): Parsed {
  if (!json) return {};
  try {
    const v = JSON.parse(json) as unknown;
    return v && typeof v === 'object' ? (v as Parsed) : {};
  } catch {
    return {};
  }
}

export function toFeatureVector(x: FeatureInput): number[] {
  const p = parseFeaturesJson(x.featuresJson);
  const out: number[] = [];
  for (const [, f] of SPEC) {
    const v = f(x, p);
    const ok = typeof v === 'number' && Number.isFinite(v);
    out.push(ok ? v : 0, ok ? 0 : 1);
  }
  return out;
}
