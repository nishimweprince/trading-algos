/**
 * Veto-cohort evidence for relaxing thresholds.
 *
 * Reads `shadow_outcomes` joined to the latest `candidates` row per mint and
 * buckets rows the same way as the manual veto review: the primary H12
 * reason (or primary veto code), a pool-SOL band, and — for in-band rows —
 * the veto check in {H5, H13, RELAXED_DISABLED, H11}.
 *
 * A cohort is a relax candidate only when the bootstrap CI lower bound on
 * the mean pnl% is > 0 at n >= 30. This report never changes config itself;
 * it is the gate for any later threshold change.
 */
import { bootstrapCI, mean } from './stats.ts';

export type PoolBand = '<25' | '25-60' | '60-90' | '90-300' | '>300' | 'unknown';

/** Checks broken out separately for in-band rows. */
export const IN_BAND_CHECKS = ['H5', 'H13', 'RELAXED_DISABLED', 'H11'] as const;

export function poolBand(poolSol: number | null | undefined): PoolBand {
  if (poolSol === null || poolSol === undefined || !Number.isFinite(poolSol)) return 'unknown';
  if (poolSol < 25) return '<25';
  if (poolSol < 60) return '25-60';
  if (poolSol < 90) return '60-90';
  if (poolSol <= 300) return '90-300';
  return '>300';
}

export interface VetoRow {
  mint: string;
  primaryVetoCode: string | null;
  /** Full veto/red-flag code set from shadow_outcomes.veto_codes_json. */
  vetoCodes: string[];
  /** H12 sub-reason (mint_age_unknown, pool_sol_out_of_band, ...) or null. */
  h12Reason: string | null;
  poolSol: number | null;
  netPnlSol: number | null;
  pnlPct: number | null;
  exitReason: string | null;
}

/** Parse the H12 sub-reason out of a candidate's hard_check_results JSON. */
export function h12ReasonFromHardChecks(hardChecksJson: string | null | undefined): string | null {
  if (!hardChecksJson) return null;
  try {
    const checks = JSON.parse(hardChecksJson) as Array<{ id?: string; reason?: string | null }>;
    return checks.find((c) => c.id === 'H12')?.reason ?? null;
  } catch {
    return null;
  }
}

/** Cohort reason: the H12 sub-reason when H12 is the primary veto, else the primary code. */
export function cohortReason(row: VetoRow): string {
  if (row.primaryVetoCode === 'H12') return row.h12Reason ?? 'H12';
  return row.primaryVetoCode ?? 'UNKNOWN';
}

export interface CohortStat {
  key: string;
  n: number;
  netSol: number;
  meanPnlPct: number;
  medianPnlPct: number;
  winRatePct: number;
  worstNetSol: number;
  entryFailedSharePct: number;
  ciLo: number;
  ciHi: number;
  relaxCandidate: boolean;
}

function median(xs: number[]): number {
  if (xs.length === 0) return NaN;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m]! : (s[m - 1]! + s[m]!) / 2;
}

export function summarizeCohort(key: string, rows: VetoRow[], seed = 1): CohortStat {
  const nets = rows.map((r) => r.netPnlSol ?? 0);
  const pcts = rows.map((r) => r.pnlPct ?? 0);
  const ci = bootstrapCI(pcts, { seed });
  const n = rows.length;
  return {
    key,
    n,
    netSol: nets.reduce((a, b) => a + b, 0),
    meanPnlPct: n ? mean(pcts) : NaN,
    medianPnlPct: median(pcts),
    winRatePct: n ? (nets.filter((x) => x > 0).length / n) * 100 : NaN,
    worstNetSol: n ? Math.min(...nets) : NaN,
    entryFailedSharePct: n ? (rows.filter((r) => r.exitReason === 'ENTRY_FAILED').length / n) * 100 : NaN,
    ciLo: ci.lo,
    ciHi: ci.hi,
    relaxCandidate: n >= 30 && ci.lo > 0,
  };
}

export interface CohortReport {
  outcomeVersion: string;
  range: string;
  rows: number;
  cohorts: CohortStat[];
  inBandByCheck: CohortStat[];
}

/** Bucket rows into (reason × band) cohorts plus in-band per-check cohorts. */
export function buildReport(rows: VetoRow[], opts: { outcomeVersion: string; range: string; seed?: number }): CohortReport {
  const seed = opts.seed ?? 1;
  const byKey = new Map<string, VetoRow[]>();
  for (const r of rows) {
    const key = `${cohortReason(r)} | ${poolBand(r.poolSol)}`;
    byKey.set(key, [...(byKey.get(key) ?? []), r]);
  }
  const byCheck = new Map<string, VetoRow[]>();
  for (const r of rows) {
    const band = poolBand(r.poolSol);
    if (band !== '25-60' && band !== '60-90' && band !== '90-300') continue;
    for (const check of IN_BAND_CHECKS) {
      if (!r.vetoCodes.includes(check) && r.primaryVetoCode !== check) continue;
      const key = `${check} | ${band}`;
      byCheck.set(key, [...(byCheck.get(key) ?? []), r]);
    }
  }
  const sort = (a: CohortStat, b: CohortStat) => b.n - a.n || a.key.localeCompare(b.key);
  return {
    outcomeVersion: opts.outcomeVersion,
    range: opts.range,
    rows: rows.length,
    cohorts: [...byKey.entries()].map(([k, v]) => summarizeCohort(k, v, seed)).sort(sort),
    inBandByCheck: [...byCheck.entries()].map(([k, v]) => summarizeCohort(k, v, seed)).sort(sort),
  };
}

const fmt = (x: number, d = 2) => (Number.isFinite(x) ? x.toFixed(d) : '—');

export function renderMarkdown(r: CohortReport): string {
  const head = '| Cohort | n | net SOL | mean pnl% | median pnl% | win% | worst | ENTRY_FAILED% | mean CI lo–hi | relax? |';
  const sep = '|---|---|---|---|---|---|---|---|---|---|';
  const row = (c: CohortStat) =>
    `| ${c.key} | ${c.n} | ${fmt(c.netSol, 4)} | ${fmt(c.meanPnlPct)} | ${fmt(c.medianPnlPct)} | ${fmt(c.winRatePct, 1)} | ${fmt(c.worstNetSol, 4)} | ${fmt(c.entryFailedSharePct, 1)} | ${fmt(c.ciLo)}–${fmt(c.ciHi)} | ${c.relaxCandidate ? '**candidate**' : ''} |`;
  const flagged = [...r.cohorts, ...r.inBandByCheck].filter((c) => c.relaxCandidate);
  return [
    `# Veto cohorts — ${r.range} (${r.outcomeVersion})`,
    '',
    `${r.rows} veto outcomes. A cohort is a relax candidate only when the bootstrap CI lower bound on mean pnl% > 0 at n ≥ 30.`,
    '',
    flagged.length ? `Relax candidates: ${flagged.map((c) => c.key).join(', ')}` : 'Relax candidates: none.',
    '',
    '## Cohorts (reason × pool band)',
    '',
    head,
    sep,
    ...r.cohorts.map(row),
    '',
    '## In-band vetoes by check',
    '',
    head,
    sep,
    ...(r.inBandByCheck.length ? r.inBandByCheck.map(row) : ['(no in-band vetoes by H5/H13/RELAXED_DISABLED/H11)']),
    '',
  ].join('\n');
}
