import type { DB } from '../persistence/db.ts';

/**
 * Rug forensics (LIVE_PILOT_PLAN §4 S3).
 *
 * Joins closed twin / live trades to the screening features recorded for the
 * same mint at accept time (`candidates`), splits them into rugs (net PnL at
 * or below `rugPnlPct`) and the rest, and tabulates each feature's
 * distribution in both cohorts. The point is to find the screening knob that
 * separates the 55 −100% trades in the dry run from the 1,783 that were not —
 * whatever separates them becomes a hard-cap tighten or a size-down, not a
 * guess.
 *
 * Pure reads; safe to run against a live DB.
 */

export type ForensicsTrack = 'dry' | 'live';

export interface FeatureStat {
  feature: string;
  rug: Quantiles;
  other: Quantiles;
  /** Share of rugs at/above the "other" cohort's p75 — a crude separability score. */
  rugShareAboveOtherP75: number | null;
}

export interface Quantiles {
  n: number;
  p25: number | null;
  p50: number | null;
  p75: number | null;
  mean: number | null;
}

export interface CreatorRepeat {
  creator: string;
  rugs: number;
  trades: number;
}

export interface RugForensicsReport {
  track: ForensicsTrack;
  range: string;
  rugPnlPct: number;
  trades: number;
  rugs: number;
  rugRatePct: number;
  rugNetSol: number;
  rugMedianHoldS: number | null;
  rugMedianMfePct: number | null;
  features: FeatureStat[];
  /** Creators that rugged more than once — H8 blacklist candidates. */
  repeatCreators: CreatorRepeat[];
  /** Mints of the rug cohort, for manual follow-up. */
  rugMints: string[];
}

const FEATURES = [
  'creator_share',
  'top10_share',
  'max_holder_share',
  'pool_sol_at_entry',
  'buy_impact_pct',
  'early_flow_net_sol',
  'early_flow_rate',
  'rugcheck_score',
  'soft_score',
  'size_multiplier',
  'enrichment_ms',
] as const;

interface JoinedRow {
  mint: string;
  net_pnl_sol: number | null;
  pnl_pct: number | null;
  hold_ms: number | null;
  mfe_pct: number | null;
  creator: string | null;
  [k: string]: unknown;
}

function rangeSql(range: string): string {
  const days = range === '24h' ? 1 : range === '7d' ? 7 : range === '30d' ? 30 : null;
  return days === null ? '' : ` AND julianday(t.closed_at) >= julianday('now', '-${days} days')`;
}

function quantiles(values: number[]): Quantiles {
  const v = values.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  if (v.length === 0) return { n: 0, p25: null, p50: null, p75: null, mean: null };
  const q = (p: number) => v[Math.min(v.length - 1, Math.floor(p * (v.length - 1)))]!;
  return {
    n: v.length,
    p25: q(0.25),
    p50: q(0.5),
    p75: q(0.75),
    mean: v.reduce((a, b) => a + b, 0) / v.length,
  };
}

export function buildRugForensics(
  db: DB,
  opts: { track?: ForensicsTrack; range?: string; rugPnlPct?: number } = {},
): RugForensicsReport {
  const track = opts.track ?? 'dry';
  const range = opts.range ?? '7d';
  const rugPnlPct = opts.rugPnlPct ?? -80;
  const table = track === 'dry' ? 'dry_run_positions' : 'positions';

  const featureCols = FEATURES.map((f) => `c.${f}`).join(', ');
  const rows = db
    .prepare(
      `SELECT t.mint, t.net_pnl_sol, t.pnl_pct, t.hold_ms, t.mfe_pct,
              json_extract(c.enrichment_json, '$.pool.coinCreator') AS creator,
              ${featureCols}
         FROM ${table} t
         LEFT JOIN candidates c
           ON c.rowid = (SELECT MAX(rowid) FROM candidates WHERE mint = t.mint)
        WHERE t.rowid IN (SELECT MAX(rowid) FROM ${table} GROUP BY mint)
          AND t.state = 'CLOSED'${rangeSql(range)}`,
    )
    .all() as unknown as JoinedRow[];

  const rugs = rows.filter((r) => (r.pnl_pct ?? 0) <= rugPnlPct);
  const others = rows.filter((r) => (r.pnl_pct ?? 0) > rugPnlPct);

  const features: FeatureStat[] = FEATURES.map((f) => {
    const pick = (rs: JoinedRow[]) => rs.map((r) => Number(r[f])).filter((x) => Number.isFinite(x));
    const rug = quantiles(pick(rugs));
    const other = quantiles(pick(others));
    const rugVals = pick(rugs);
    const rugShareAboveOtherP75 =
      other.p75 !== null && rugVals.length ? rugVals.filter((x) => x >= other.p75!).length / rugVals.length : null;
    return { feature: f, rug, other, rugShareAboveOtherP75 };
  });

  const byCreator = new Map<string, CreatorRepeat>();
  for (const r of rows) {
    if (!r.creator) continue;
    const entry = byCreator.get(r.creator) ?? { creator: r.creator, rugs: 0, trades: 0 };
    entry.trades++;
    if ((r.pnl_pct ?? 0) <= rugPnlPct) entry.rugs++;
    byCreator.set(r.creator, entry);
  }
  const repeatCreators = [...byCreator.values()].filter((c) => c.rugs > 1).sort((a, b) => b.rugs - a.rugs);

  return {
    track,
    range,
    rugPnlPct,
    trades: rows.length,
    rugs: rugs.length,
    rugRatePct: rows.length ? (rugs.length / rows.length) * 100 : 0,
    rugNetSol: rugs.reduce((a, r) => a + (r.net_pnl_sol ?? 0), 0),
    rugMedianHoldS: quantiles(rugs.map((r) => (r.hold_ms ?? 0) / 1000)).p50,
    rugMedianMfePct: quantiles(rugs.map((r) => r.mfe_pct ?? 0)).p50,
    features,
    repeatCreators,
    rugMints: rugs.map((r) => r.mint),
  };
}

export function renderRugForensicsMarkdown(r: RugForensicsReport): string {
  const fmt = (x: number | null, d = 2) => (x === null ? '—' : x.toFixed(d));
  const lines: string[] = [];
  lines.push(`# Rug forensics — ${r.track} track, range ${r.range}`);
  lines.push('');
  lines.push(
    `${r.trades} closed trades, **${r.rugs} rugs** (pnl ≤ ${r.rugPnlPct}%) = ${r.rugRatePct.toFixed(2)}% · ` +
      `rug net ${r.rugNetSol.toFixed(3)} SOL · rug median hold ${fmt(r.rugMedianHoldS, 0)} s · rug median MFE +${fmt(r.rugMedianMfePct, 1)}%`,
  );
  lines.push('');
  lines.push('| feature | rug p25 / p50 / p75 (n) | other p25 / p50 / p75 (n) | rugs ≥ other p75 |');
  lines.push('| --- | --- | --- | --- |');
  for (const f of r.features) {
    lines.push(
      `| ${f.feature} | ${fmt(f.rug.p25)} / ${fmt(f.rug.p50)} / ${fmt(f.rug.p75)} (${f.rug.n}) ` +
        `| ${fmt(f.other.p25)} / ${fmt(f.other.p50)} / ${fmt(f.other.p75)} (${f.other.n}) ` +
        `| ${f.rugShareAboveOtherP75 === null ? '—' : (f.rugShareAboveOtherP75 * 100).toFixed(0) + '%'} |`,
    );
  }
  lines.push('');
  lines.push('A feature is a screening lever when the rug column sits clearly above (or below) the other column — e.g. a rug p50 above the other p75. A feature with overlapping quartiles does not separate the cohorts and is not worth a cap.');
  lines.push('');
  if (r.repeatCreators.length) {
    lines.push('## Repeat-rug creators (blacklist candidates)');
    lines.push('');
    lines.push('| creator | rugs | trades |');
    lines.push('| --- | --- | --- |');
    for (const c of r.repeatCreators) lines.push(`| ${c.creator} | ${c.rugs} | ${c.trades} |`);
    lines.push('');
  }
  lines.push(`## Rug mints (${r.rugMints.length})`);
  lines.push('');
  for (const m of r.rugMints) lines.push(`- ${m}`);
  lines.push('');
  return lines.join('\n');
}
