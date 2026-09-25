import type { DB } from '../persistence/db.ts';
import { bootstrapCI, mean, type Interval } from '../research/stats.ts';
import { latencyPercentiles } from './analytics.ts';

/**
 * Rolling edge analytics (work plan 2026-09-25 P4.3) behind
 * `/api/analytics/edge`: is the recent strategy still positive after costs,
 * and where is it leaking? Everything is over the last `window` closed trades
 * of one track, with the same deterministic bootstrap the research CLIs and
 * the NEGATIVE_EDGE breaker use.
 */

export interface EdgeCohort {
  cohort: string;
  n: number;
  netSol: number;
  meanPct: number;
  winRatePct: number;
}

export interface EdgeAnalytics {
  generatedAt: string;
  track: 'live' | 'dry';
  window: number;
  n: number;
  /** Mean net return, % of size per trade, with its bootstrap CI (null below 2 trades). */
  expectancyPct: Interval | null;
  netSol: number;
  winRatePct: number | null;
  /** All-in fees (swap + tx) as % of round-trip notional (entry size + exit proceeds). */
  feePctOfNotional: number | null;
  /** Share of gross losses (SOL) that came from EMERGENCY_EXIT closes. */
  emergencyShareOfLossesPct: number | null;
  cohorts: { byRisk: EdgeCohort[]; bySuffix: EdgeCohort[]; byExit: EdgeCohort[] };
  latency: Record<string, { count: number; p50: number; p95: number }>;
  /** Model calibration: predicted vs realized win rate per probability decile (live only). */
  calibration: Array<{ bin: string; n: number; meanProb: number; winRatePct: number }>;
}

interface Row {
  mint: string;
  pnl: number;
  size: number;
  fees: number | null;
  exitReason: string | null;
  relaxed: number | null;
  modelProb: number | null;
}

const LATENCY_KINDS = ['detect_to_send', 'entry_confirm', 'exit_confirm'] as const;

function hasColumn(db: DB, table: string, column: string): boolean {
  return (db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>).some((c) => c.name === column);
}

export function getEdgeAnalytics(
  db: DB,
  opts: { track?: 'live' | 'dry'; window?: number; seed?: number; iterations?: number } = {},
): EdgeAnalytics {
  const track = opts.track ?? 'live';
  const window = opts.window ?? 100;
  const table = track === 'dry' ? 'dry_run_positions' : 'positions';
  const col = (name: string, fallback = 'NULL') => (hasColumn(db, table, name) ? name : fallback);
  const rows = (
    db
      .prepare(
        `SELECT mint, COALESCE(${col('net_pnl_sol')}, pnl_sol) AS pnl, size_sol AS size, ${col('fees_sol')} AS fees,
                exit_reason AS exitReason, ${col('relaxed_risk')} AS relaxed, ${col('model_prob')} AS modelProb
           FROM ${table}
          WHERE state = 'CLOSED' AND pnl_sol IS NOT NULL AND size_sol > 0
          ORDER BY julianday(COALESCE(closed_at, created_at)) DESC, rowid DESC
          LIMIT ?`,
      )
      .all(window) as unknown as Row[]
  ).reverse();

  const pct = (r: Row) => (r.pnl / r.size) * 100;
  const returns = rows.map(pct);
  const n = rows.length;
  const netSol = rows.reduce((s, r) => s + r.pnl, 0);

  const withFees = rows.filter((r) => r.fees !== null);
  // Round-trip notional ~ entry size + exit proceeds (size + gross pnl ~ size + pnl + fees).
  const notional = withFees.reduce((s, r) => s + r.size + Math.max(0, r.size + r.pnl + (r.fees ?? 0)), 0);
  const feeSum = withFees.reduce((s, r) => s + (r.fees ?? 0), 0);

  const losses = rows.filter((r) => r.pnl < 0);
  const lossSol = losses.reduce((s, r) => s - r.pnl, 0);
  const emergencyLossSol = losses.filter((r) => r.exitReason === 'EMERGENCY_EXIT').reduce((s, r) => s - r.pnl, 0);

  const cohort = (key: (r: Row) => string): EdgeCohort[] => {
    const groups = new Map<string, Row[]>();
    for (const r of rows) {
      const k = key(r);
      groups.set(k, [...(groups.get(k) ?? []), r]);
    }
    return [...groups.entries()]
      .map(([c, rs]) => ({
        cohort: c,
        n: rs.length,
        netSol: rs.reduce((s, r) => s + r.pnl, 0),
        meanPct: mean(rs.map(pct)),
        winRatePct: (rs.filter((r) => r.pnl > 0).length / rs.length) * 100,
      }))
      .sort((a, b) => b.n - a.n);
  };

  const latency: EdgeAnalytics['latency'] = {};
  for (const kind of LATENCY_KINDS) {
    const l = latencyPercentiles(db, kind, '-7 days');
    latency[kind] = { count: l.count, p50: l.p50, p95: l.p95 };
  }

  const calibration: EdgeAnalytics['calibration'] = [];
  const scored = rows.filter((r) => r.modelProb !== null && Number.isFinite(r.modelProb));
  for (let b = 0; b < 10; b++) {
    const lo = b / 10;
    const inBin = scored.filter((r) => r.modelProb! >= lo && (b === 9 ? r.modelProb! <= 1 : r.modelProb! < lo + 0.1));
    if (inBin.length === 0) continue;
    calibration.push({
      bin: `${lo.toFixed(1)}-${(lo + 0.1).toFixed(1)}`,
      n: inBin.length,
      meanProb: mean(inBin.map((r) => r.modelProb!)),
      winRatePct: (inBin.filter((r) => r.pnl > 0).length / inBin.length) * 100,
    });
  }

  return {
    generatedAt: new Date().toISOString(),
    track,
    window,
    n,
    expectancyPct: n >= 2 ? bootstrapCI(returns, { seed: opts.seed ?? 1, iterations: opts.iterations ?? 2_000 }) : null,
    netSol,
    winRatePct: n > 0 ? (rows.filter((r) => r.pnl > 0).length / n) * 100 : null,
    feePctOfNotional: notional > 0 ? (feeSum / notional) * 100 : null,
    emergencyShareOfLossesPct: lossSol > 0 ? (emergencyLossSol / lossSol) * 100 : null,
    cohorts: {
      byRisk: cohort((r) => (r.relaxed ? 'relaxed' : 'strict')),
      bySuffix: cohort((r) => (r.mint.endsWith('pump') ? 'pump' : 'other')),
      byExit: cohort((r) => r.exitReason ?? 'unknown'),
    },
    latency,
    calibration,
  };
}
