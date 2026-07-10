/**
 * Pure analytics helpers + snapshot builders for operator monitoring / reports.
 * Kept free of HTTP so unit tests can exercise formulas without the dashboard server.
 */

import type { DB } from '../persistence/db.ts';
import { pruneLatencySamples } from '../persistence/db.ts';
import type { Config } from '../config/schema.ts';

export interface PerformanceStats {
  tradeCount: number;
  wins: number;
  losses: number;
  realizedPnlSol: number;
  feesSol: number;
  grossPnlSol: number;
  winRatePct: number;
  expectancySol: number;
  profitFactor: number;
  avgWinSol: number;
  avgLossSol: number;
  medianPnlSol: number;
  bestTradeSol: number;
  worstTradeSol: number;
  maxDrawdownSol: number;
  currentDrawdownSol: number;
}

export function computePerformanceStats(pnls: number[], fees: number[] = []): PerformanceStats {
  const tradeCount = pnls.length;
  if (tradeCount === 0) {
    return {
      tradeCount: 0,
      wins: 0,
      losses: 0,
      realizedPnlSol: 0,
      feesSol: sum(fees),
      grossPnlSol: 0,
      winRatePct: 0,
      expectancySol: 0,
      profitFactor: 0,
      avgWinSol: 0,
      avgLossSol: 0,
      medianPnlSol: 0,
      bestTradeSol: 0,
      worstTradeSol: 0,
      maxDrawdownSol: 0,
      currentDrawdownSol: 0,
    };
  }

  const winsArr = pnls.filter((p) => p > 0);
  const lossesArr = pnls.filter((p) => p <= 0);
  const wins = winsArr.length;
  const losses = lossesArr.length;
  const realizedPnlSol = sum(pnls);
  const grossWins = sum(winsArr);
  const grossLossAbs = Math.abs(sum(lossesArr));
  const profitFactor = grossLossAbs > 0 ? grossWins / grossLossAbs : grossWins > 0 ? Infinity : 0;
  const avgWinSol = wins > 0 ? grossWins / wins : 0;
  const avgLossSol = losses > 0 ? sum(lossesArr) / losses : 0;
  const winRate = wins / tradeCount;
  const lossRate = losses / tradeCount;
  const expectancySol = winRate * avgWinSol + lossRate * avgLossSol;
  const sorted = [...pnls].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  const medianPnlSol =
    sorted.length % 2 === 0
      ? ((sorted[mid - 1] ?? 0) + (sorted[mid] ?? 0)) / 2
      : (sorted[mid] ?? 0);

  const { maxDrawdownSol, currentDrawdownSol } = computeDrawdowns(pnls);
  const feesSol = sum(fees);
  const grossPnlSol = realizedPnlSol + feesSol;

  return {
    tradeCount,
    wins,
    losses,
    realizedPnlSol,
    feesSol,
    grossPnlSol,
    winRatePct: winRate * 100,
    expectancySol,
    profitFactor: Number.isFinite(profitFactor) ? profitFactor : 999,
    avgWinSol,
    avgLossSol,
    medianPnlSol,
    bestTradeSol: Math.max(...pnls),
    worstTradeSol: Math.min(...pnls),
    maxDrawdownSol,
    currentDrawdownSol,
  };
}

export function computeDrawdowns(pnlsInOrder: number[]): { maxDrawdownSol: number; currentDrawdownSol: number } {
  let equity = 0;
  let peak = 0;
  let maxDd = 0;
  for (const p of pnlsInOrder) {
    equity += p;
    if (equity > peak) peak = equity;
    const dd = peak - equity;
    if (dd > maxDd) maxDd = dd;
  }
  return { maxDrawdownSol: maxDd, currentDrawdownSol: Math.max(0, peak - equity) };
}

export function percentile(values: number[], p: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[idx] ?? 0;
}

export function latencyPercentiles(
  db: DB,
  kind: string,
  sinceModifier: string,
): { count: number; p50: number; p95: number; max: number } {
  const rows = db
    .prepare(
      `SELECT latency_ms AS ms FROM latency_samples
       WHERE kind = ?
         AND julianday(created_at) >= julianday('now', ?)
       ORDER BY created_at ASC`,
    )
    .all(kind, sinceModifier) as Array<{ ms: number }>;
  const values = rows.map((r) => r.ms);
  return {
    count: values.length,
    p50: percentile(values, 50),
    p95: percentile(values, 95),
    max: values.length ? Math.max(...values) : 0,
  };
}

/** Closed-position PnLs in chronological order for a SQL day-window modifier. */
export function loadClosedPnls(db: DB, sinceModifier?: string): { pnls: number[]; fees: number[] } {
  const sql = sinceModifier
    ? `SELECT COALESCE(net_pnl_sol, pnl_sol, 0) AS pnl, COALESCE(fees_sol, 0) AS fees
       FROM positions
       WHERE state = 'CLOSED'
         AND COALESCE(closed_at, created_at) IS NOT NULL
         AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)
       ORDER BY julianday(COALESCE(closed_at, created_at)) ASC, rowid ASC`
    : `SELECT COALESCE(net_pnl_sol, pnl_sol, 0) AS pnl, COALESCE(fees_sol, 0) AS fees
       FROM positions
       WHERE state = 'CLOSED'
       ORDER BY julianday(COALESCE(closed_at, created_at)) ASC, rowid ASC`;
  const rows = (sinceModifier
    ? db.prepare(sql).all(sinceModifier)
    : db.prepare(sql).all()) as Array<{ pnl: number; fees: number }>;
  return { pnls: rows.map((r) => r.pnl), fees: rows.map((r) => r.fees) };
}

export function rangeToModifier(range?: '24h' | '7d' | '30d' | 'all'): string | undefined {
  if (!range || range === 'all') return undefined;
  if (range === '24h') return '-1 day';
  if (range === '30d') return '-30 days';
  return '-7 days';
}

export function buildHourlySnapshot(db: DB, config: Config, periodStartIso: string): void {
  const endIso = new Date(Date.parse(periodStartIso) + 3_600_000).toISOString();
  const closed = db
    .prepare(
      `SELECT COALESCE(net_pnl_sol, pnl_sol, 0) AS pnl, COALESCE(fees_sol, 0) AS fees, exit_reason
       FROM positions
       WHERE state = 'CLOSED'
         AND closed_at IS NOT NULL
         AND closed_at >= ? AND closed_at < ?
       ORDER BY closed_at ASC, rowid ASC`,
    )
    .all(periodStartIso, endIso) as Array<{ pnl: number; fees: number; exit_reason: string | null }>;

  const pnls = closed.map((r) => r.pnl);
  const fees = closed.map((r) => r.fees);
  const stats = computePerformanceStats(pnls, fees);

  const flow = db
    .prepare(
      `SELECT
         (SELECT COUNT(*) FROM graduations WHERE created_at >= ? AND created_at < ?) AS graduations,
         (SELECT COUNT(*) FROM candidates WHERE verdict = 'accept' AND created_at >= ? AND created_at < ?) AS accepted,
         (SELECT COUNT(*) FROM candidates WHERE verdict = 'veto' AND created_at >= ? AND created_at < ?) AS vetoed,
         -- positions is append-only; dedupe by mint so a single position that
         -- transitions through several states is not counted multiple times.
         (SELECT COUNT(DISTINCT mint) FROM positions WHERE state IN ('OPEN','PENDING_ENTRY','EXITING','CLOSED')
            AND created_at >= ? AND created_at < ?) AS entered,
         (SELECT COUNT(DISTINCT mint) FROM positions WHERE state = 'FAILED' AND created_at >= ? AND created_at < ?) AS failed,
         (SELECT COUNT(DISTINCT mint) FROM positions WHERE state = 'CLOSED' AND exit_reason = 'EMERGENCY_EXIT'
            AND closed_at >= ? AND closed_at < ?) AS emergency_exits`,
    )
    .get(
      periodStartIso,
      endIso,
      periodStartIso,
      endIso,
      periodStartIso,
      endIso,
      periodStartIso,
      endIso,
      periodStartIso,
      endIso,
      periodStartIso,
      endIso,
    ) as {
    graduations: number;
    accepted: number;
    vetoed: number;
    entered: number;
    failed: number;
    emergency_exits: number;
  };

  const det = samplesInRange(db, 'detection', periodStartIso, endIso);
  const exitLat = samplesInRange(db, 'exit_confirm', periodStartIso, endIso);

  const exitBreakdown: Record<string, { count: number; pnlSol: number }> = {};
  for (const row of closed) {
    const key = row.exit_reason ?? 'UNKNOWN';
    const bucket = exitBreakdown[key] ?? { count: 0, pnlSol: 0 };
    bucket.count += 1;
    bucket.pnlSol += row.pnl;
    exitBreakdown[key] = bucket;
  }

  db.prepare(
    `INSERT INTO analytics_snapshots (
       period, period_start, mode, realized_pnl_sol, trade_count, wins, losses,
       expectancy_sol, profit_factor, max_drawdown_sol, graduations, accepted, vetoed,
       entered, failed, emergency_exits, detection_p50_ms, detection_p95_ms,
       exit_confirm_p50_ms, exit_confirm_p95_ms, fees_sol, payload_json
     ) VALUES (
       'hour', @periodStart, @mode, @realizedPnlSol, @tradeCount, @wins, @losses,
       @expectancySol, @profitFactor, @maxDrawdownSol, @graduations, @accepted, @vetoed,
       @entered, @failed, @emergencyExits, @detectionP50Ms, @detectionP95Ms,
       @exitConfirmP50Ms, @exitConfirmP95Ms, @feesSol, @payloadJson
     )
     ON CONFLICT(period, period_start, mode) DO UPDATE SET
       realized_pnl_sol = excluded.realized_pnl_sol,
       trade_count = excluded.trade_count,
       wins = excluded.wins,
       losses = excluded.losses,
       expectancy_sol = excluded.expectancy_sol,
       profit_factor = excluded.profit_factor,
       max_drawdown_sol = excluded.max_drawdown_sol,
       graduations = excluded.graduations,
       accepted = excluded.accepted,
       vetoed = excluded.vetoed,
       entered = excluded.entered,
       failed = excluded.failed,
       emergency_exits = excluded.emergency_exits,
       detection_p50_ms = excluded.detection_p50_ms,
       detection_p95_ms = excluded.detection_p95_ms,
       exit_confirm_p50_ms = excluded.exit_confirm_p50_ms,
       exit_confirm_p95_ms = excluded.exit_confirm_p95_ms,
       fees_sol = excluded.fees_sol,
       payload_json = excluded.payload_json`,
  ).run({
    periodStart: periodStartIso,
    mode: config.mode,
    realizedPnlSol: stats.realizedPnlSol,
    tradeCount: stats.tradeCount,
    wins: stats.wins,
    losses: stats.losses,
    expectancySol: stats.expectancySol,
    profitFactor: stats.profitFactor,
    maxDrawdownSol: stats.maxDrawdownSol,
    graduations: flow.graduations,
    accepted: flow.accepted,
    vetoed: flow.vetoed,
    entered: flow.entered,
    failed: flow.failed,
    emergencyExits: flow.emergency_exits,
    detectionP50Ms: percentile(det, 50),
    detectionP95Ms: percentile(det, 95),
    exitConfirmP50Ms: percentile(exitLat, 50),
    exitConfirmP95Ms: percentile(exitLat, 95),
    feesSol: stats.feesSol,
    payloadJson: JSON.stringify({ exitBreakdown }),
  });
}

/** Floor current UTC time to the previous complete hour and upsert snapshot. */
export function runAnalyticsMaintenance(
  db: DB,
  config: Config,
  retentionDays: number,
): { latencyPruned: number; snapshotPeriodStart: string } {
  const latencyPruned = pruneLatencySamples(db, retentionDays);
  const now = Date.now();
  const hourMs = 3_600_000;
  const prevHourStart = new Date(Math.floor(now / hourMs) * hourMs - hourMs).toISOString();
  buildHourlySnapshot(db, config, prevHourStart);
  return { latencyPruned, snapshotPeriodStart: prevHourStart };
}

function samplesInRange(db: DB, kind: string, fromIso: string, toIso: string): number[] {
  const rows = db
    .prepare(
      `SELECT latency_ms AS ms FROM latency_samples
       WHERE kind = ? AND created_at >= ? AND created_at < ?`,
    )
    .all(kind, fromIso, toIso) as Array<{ ms: number }>;
  return rows.map((r) => r.ms);
}

function sum(xs: number[]): number {
  return xs.reduce((a, b) => a + b, 0);
}
