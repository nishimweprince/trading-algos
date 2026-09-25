import type { DB } from '../persistence/db.ts';
import { toFeatureVector, type FeatureInput } from './featureSpec.ts';
import { tripleBarrier, type BarrierSpec, type CostSpec, type PathTick } from './labels.ts';
import type { Timed } from './validate.ts';

/**
 * Labelled dataset from recorded shadow / confirm-arm tick paths
 * (work plan 2026-09-25 P3.4). One sample per (mint, arm) path: features from
 * the latest candidates row for the mint (the same fields live inference
 * sees), label from the triple barrier on the path through the honest
 * simulator. Labels exist for EVERY tracked canonical graduation, not only
 * the ones the bot traded — that is what removes selection bias.
 */
export interface Sample extends Timed {
  mint: string;
  arm: string;
  x: number[];
  label: 0 | 1;
  netReturn: number;
  barrier: string;
}

interface CandRow {
  created_at: string;
  early_flow_net_sol: number | null;
  early_flow_rate: number | null;
  pool_sol_at_entry: number | null;
  top10_share: number | null;
  max_holder_share: number | null;
  creator_share: number | null;
  rugcheck_score: number | null;
  has_socials: number | null;
  mint_age_ms: number | null;
  mcap_sol_at_entry: number | null;
  pool_move_pct: number | null;
  sellability_status: string | null;
  momentum_window_ms: number | null;
  features_json: string | null;
  population_ok: number | null;
}

export function candidateFeatureInput(r: CandRow): FeatureInput {
  return {
    earlyFlowNetSol: r.early_flow_net_sol,
    earlyFlowRate: r.early_flow_rate,
    poolSolAtEntry: r.pool_sol_at_entry,
    top10Share: r.top10_share,
    maxHolderShare: r.max_holder_share,
    creatorShare: r.creator_share,
    rugcheckScore: r.rugcheck_score,
    hasSocials: r.has_socials === null ? null : r.has_socials === 1,
    mintAgeMs: r.mint_age_ms,
    mcapSolAtEntry: r.mcap_sol_at_entry,
    poolMovePct: r.pool_move_pct,
    sellabilityStatus: r.sellability_status,
    momentumWindowMs: r.momentum_window_ms,
    featuresJson: r.features_json,
  };
}

const parseTs = (raw: string) => Date.parse(raw.includes('T') ? raw : `${raw.replace(' ', 'T')}Z`);

export function buildDataset(
  db: DB,
  opts: { arms?: readonly string[]; barrier: BarrierSpec; cost: CostSpec; minTicks?: number; canonicalOnly?: boolean },
): Sample[] {
  const pairs = db
    .prepare(`SELECT mint, arm, COUNT(*) AS n FROM path_ticks GROUP BY mint, arm HAVING n >= ?`)
    .all(opts.minTicks ?? 5) as Array<{ mint: string; arm: string }>;
  const cand = db.prepare(
    `SELECT created_at, early_flow_net_sol, early_flow_rate, pool_sol_at_entry, top10_share, max_holder_share,
            creator_share, rugcheck_score, has_socials, mint_age_ms, mcap_sol_at_entry, pool_move_pct,
            sellability_status, momentum_window_ms, features_json, population_ok
     FROM candidates WHERE mint = ? ORDER BY rowid DESC LIMIT 1`,
  );
  const ticks = db.prepare(`SELECT t_ms AS tMs, price FROM path_ticks WHERE mint = ? AND arm = ? ORDER BY t_ms`);
  const out: Sample[] = [];
  for (const { mint, arm } of pairs) {
    if (opts.arms && !opts.arms.includes(arm)) continue;
    const row = cand.get(mint) as CandRow | undefined;
    if (!row) continue;
    if (opts.canonicalOnly && row.population_ok === 0) continue;
    const path = ticks.all(mint, arm) as unknown as PathTick[];
    const lab = tripleBarrier(path, opts.barrier, opts.cost);
    if (!lab) continue;
    const t = parseTs(row.created_at);
    if (!Number.isFinite(t)) continue;
    out.push({
      mint,
      arm,
      t,
      tEnd: t + Math.max(0, lab.holdMs),
      x: toFeatureVector(candidateFeatureInput(row)),
      label: lab.label,
      netReturn: lab.netReturn,
      barrier: lab.barrier,
    });
  }
  return out.sort((a, b) => a.t - b.t);
}
