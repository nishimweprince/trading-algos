import type { DB } from '../persistence/db.ts';
import type { Config } from '../config/schema.ts';
import type { OperatorEventLevel } from '../persistence/repositories.ts';
import type { RiskSnapshot } from '../risk/manager.ts';
import {
  computePerformanceStats,
  latencyPercentiles,
  loadClosedPnls,
  rangeToModifier,
  type PerformanceStats,
} from './analytics.ts';

type Row = Record<string, unknown>;

const ACTIVE_STATES = ['PENDING_ENTRY', 'OPEN', 'EXITING'];

export interface DashboardSummary {
  mode: Config['mode'];
  pnl: {
    realizedSol: number;
    realized24hSol: number;
    realized7dSol: number;
    winRatePct: number;
    closedCount: number;
    wins: number;
    losses: number;
    expectancySol: number;
    profitFactor: number;
    maxDrawdownSol: number;
    currentDrawdownSol: number;
    avgWinSol: number;
    avgLossSol: number;
    feesSol: number;
    unrealizedSol: number;
  };
  positions: {
    openCount: number;
    openExposureSol: number;
    maxConcurrent: number;
    pendingCount: number;
    exitingCount: number;
    failedCount: number;
  };
  flow: {
    graduations: number;
    accepted: number;
    vetoed: number;
    highVolatility: number;
  };
  latency: {
    detection: { count: number; p50: number; p95: number; max: number };
    exitConfirm: { count: number; p50: number; p95: number; max: number };
  };
  system: {
    latestBreaker: { type: string; detail: string | null; tripped: boolean; at: string } | null;
    latestEventAt: string | null;
    lastGraduationAt: string | null;
  };
}

export interface DashboardPosition {
  id: number;
  mint: string;
  state: string;
  entryTx: string | null;
  entryPrice: number | null;
  exitPrice: number | null;
  sizeSol: number;
  exitReason: string | null;
  exitTx: string | null;
  pnlSol: number | null;
  pnlPct: number | null;
  grossPnlSol: number | null;
  feesSol: number | null;
  netPnlSol: number | null;
  entrySoftScore: number | null;
  highVolatility: boolean | null;
  mfePct: number | null;
  maePct: number | null;
  holdMs: number | null;
  exitTriggerToConfirmMs: number | null;
  feedSource: string | null;
  venue: string | null;
  mode: string | null;
  relaxedRisk: boolean;
  relaxedReasons: string[];
  openedAt: string | null;
  closedAt: string | null;
  createdAt: string;
  unrealizedSol: number | null;
}

export interface PnlPoint {
  time: string;
  mint: string;
  pnlSol: number;
  cumulativePnlSol: number;
}

export interface DashboardCandidate {
  id: number;
  mint: string;
  verdict: string | null;
  softScore: number | null;
  vetoReasons: string[];
  highVolatility: boolean;
  relaxedRisk: boolean;
  relaxedReasons: string[];
  createdAt: string;
  hardChecks?: Array<{ id: string; label?: string; status: string; detail?: string; reason?: string }>;
}

export interface DashboardEvent {
  id: number;
  category: string;
  level: OperatorEventLevel;
  message: string;
  entityMint: string | null;
  payload: unknown;
  createdAt: string;
}

export interface ExitReasonRow {
  reason: string;
  count: number;
  pnlSol: number;
  winRatePct: number;
}

export interface FunnelStats {
  graduations: number;
  accepted: number;
  vetoed: number;
  entered: number;
  closed: number;
  failed: number;
  acceptRatePct: number;
  entryRatePct: number;
}

export interface BreakerRow {
  type: string;
  detail: string | null;
  tripped: boolean;
  at: string;
}

export function getDashboardSummary(db: DB, config: Config): DashboardSummary {
  const closed = one<{ total: number; count: number; wins: number; losses: number; fees: number }>(
    db,
    `SELECT
       COALESCE(SUM(COALESCE(net_pnl_sol, pnl_sol)), 0) AS total,
       COUNT(*) AS count,
       COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) > 0 THEN 1 ELSE 0 END), 0) AS wins,
       COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) <= 0 THEN 1 ELSE 0 END), 0) AS losses,
       COALESCE(SUM(COALESCE(fees_sol, 0)), 0) AS fees
     FROM positions
     WHERE state = 'CLOSED'`,
  );
  const recent = one<{ pnl24h: number; pnl7d: number }>(
    db,
    `SELECT
       COALESCE(SUM(CASE WHEN closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now', '-1 day') THEN COALESCE(net_pnl_sol, pnl_sol) ELSE 0 END), 0) AS pnl24h,
       COALESCE(SUM(CASE WHEN closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now', '-7 days') THEN COALESCE(net_pnl_sol, pnl_sol) ELSE 0 END), 0) AS pnl7d
     FROM positions
     WHERE state = 'CLOSED'`,
  );
  const active = one<{ count: number; exposure: number; pending: number; exiting: number }>(
    db,
    `WITH latest AS (${latestPositionsSql()})
     SELECT
       COUNT(*) AS count,
       COALESCE(SUM(size_sol), 0) AS exposure,
       COALESCE(SUM(CASE WHEN state = 'PENDING_ENTRY' THEN 1 ELSE 0 END), 0) AS pending,
       COALESCE(SUM(CASE WHEN state = 'EXITING' THEN 1 ELSE 0 END), 0) AS exiting
     FROM latest
     WHERE state IN (${ACTIVE_STATES.map((s) => `'${s}'`).join(',')})`,
  );
  const failed = one<{ n: number }>(
    db,
    `WITH latest AS (${latestPositionsSql()})
     SELECT COUNT(*) AS n FROM latest WHERE state = 'FAILED'`,
  );
  const flow = one<{ graduations: number; accepted: number; vetoed: number; highVol: number }>(
    db,
    `SELECT
       (SELECT COUNT(*) FROM graduations) AS graduations,
       (SELECT COUNT(*) FROM candidates WHERE verdict = 'accept') AS accepted,
       (SELECT COUNT(*) FROM candidates WHERE verdict = 'veto') AS vetoed,
       (SELECT COUNT(*) FROM candidates WHERE high_volatility = 1) AS highVol`,
  );
  const latestBreaker = db
    .prepare(
      `SELECT type, detail, tripped, at
       FROM breaker_events
       ORDER BY rowid DESC
       LIMIT 1`,
    )
    .get() as { type: string; detail: string | null; tripped: number; at: string } | undefined;
  const latestEvent = one<{ at: string | null }>(
    db,
    `SELECT MAX(created_at) AS at FROM operator_events`,
  );
  const lastGrad = one<{ at: string | null }>(
    db,
    `SELECT MAX(created_at) AS at FROM graduations`,
  );

  const { pnls, fees } = loadClosedPnls(db);
  const perf = computePerformanceStats(pnls, fees);
  const unrealizedSol = computeUnrealizedPnl(db);

  return {
    mode: config.mode,
    pnl: {
      realizedSol: closed.total,
      realized24hSol: recent.pnl24h,
      realized7dSol: recent.pnl7d,
      winRatePct: closed.count > 0 ? (closed.wins / closed.count) * 100 : 0,
      closedCount: closed.count,
      wins: closed.wins,
      losses: closed.losses,
      expectancySol: perf.expectancySol,
      profitFactor: perf.profitFactor,
      maxDrawdownSol: perf.maxDrawdownSol,
      currentDrawdownSol: perf.currentDrawdownSol,
      avgWinSol: perf.avgWinSol,
      avgLossSol: perf.avgLossSol,
      feesSol: closed.fees,
      unrealizedSol,
    },
    positions: {
      openCount: active.count,
      openExposureSol: active.exposure,
      maxConcurrent: config.risk.maxConcurrentPositions,
      pendingCount: active.pending,
      exitingCount: active.exiting,
      failedCount: failed.n,
    },
    flow: {
      graduations: flow.graduations,
      accepted: flow.accepted,
      vetoed: flow.vetoed,
      highVolatility: flow.highVol,
    },
    latency: {
      detection: latencyPercentiles(db, 'detection', '-1 day'),
      exitConfirm: latencyPercentiles(db, 'exit_confirm', '-1 day'),
    },
    system: {
      latestBreaker: latestBreaker
        ? {
            type: latestBreaker.type,
            detail: latestBreaker.detail,
            tripped: latestBreaker.tripped === 1,
            at: latestBreaker.at,
          }
        : null,
      latestEventAt: latestEvent.at,
      lastGraduationAt: lastGrad.at,
    },
  };
}

export function getPerformanceAnalytics(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): PerformanceStats & { exitReasons: ExitReasonRow[] } {
  const mod = rangeToModifier(opts.range);
  const { pnls, fees } = loadClosedPnls(db, mod);
  const stats = computePerformanceStats(pnls, fees);
  return { ...stats, exitReasons: listExitReasonBreakdown(db, mod) };
}

export function getFunnelAnalytics(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): FunnelStats {
  const mod = rangeToModifier(opts.range);
  const whereTime = mod ? `AND julianday(created_at) >= julianday('now', '${mod}')` : '';
  const whereClosed = mod
    ? `AND closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now', '${mod}')`
    : '';

  const row = one<{
    graduations: number;
    accepted: number;
    vetoed: number;
    entered: number;
    closed: number;
    failed: number;
  }>(
    db,
    `SELECT
       (SELECT COUNT(*) FROM graduations WHERE 1=1 ${whereTime}) AS graduations,
       (SELECT COUNT(*) FROM candidates WHERE verdict = 'accept' ${whereTime}) AS accepted,
       (SELECT COUNT(*) FROM candidates WHERE verdict = 'veto' ${whereTime}) AS vetoed,
       -- positions is append-only (one row per FSM transition), so COUNT(*) would
       -- over-count a mint once per state change. Dedupe by mint to count entities.
       (SELECT COUNT(DISTINCT mint) FROM positions WHERE state IN ('PENDING_ENTRY','OPEN','EXITING','CLOSED','FAILED') ${whereTime}) AS entered,
       (SELECT COUNT(DISTINCT mint) FROM positions WHERE state = 'CLOSED' ${whereClosed}) AS closed,
       (SELECT COUNT(DISTINCT mint) FROM positions WHERE state = 'FAILED' ${whereTime}) AS failed`,
  );

  const screened = row.accepted + row.vetoed;
  return {
    ...row,
    acceptRatePct: screened > 0 ? (row.accepted / screened) * 100 : 0,
    entryRatePct: row.accepted > 0 ? (row.entered / row.accepted) * 100 : 0,
  };
}

export function listExitReasonBreakdown(db: DB, sinceModifier?: string): ExitReasonRow[] {
  const sql = sinceModifier
    ? `SELECT COALESCE(exit_reason, 'UNKNOWN') AS reason,
              COUNT(*) AS count,
              COALESCE(SUM(COALESCE(net_pnl_sol, pnl_sol)), 0) AS pnlSol,
              COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) > 0 THEN 1 ELSE 0 END), 0) AS wins
       FROM positions
       WHERE state = 'CLOSED'
         AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)
       GROUP BY reason
       ORDER BY count DESC`
    : `SELECT COALESCE(exit_reason, 'UNKNOWN') AS reason,
              COUNT(*) AS count,
              COALESCE(SUM(COALESCE(net_pnl_sol, pnl_sol)), 0) AS pnlSol,
              COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) > 0 THEN 1 ELSE 0 END), 0) AS wins
       FROM positions
       WHERE state = 'CLOSED'
       GROUP BY reason
       ORDER BY count DESC`;
  const rows = (sinceModifier ? db.prepare(sql).all(sinceModifier) : db.prepare(sql).all()) as Array<{
    reason: string;
    count: number;
    pnlSol: number;
    wins: number;
  }>;
  return rows.map((r) => ({
    reason: r.reason,
    count: r.count,
    pnlSol: r.pnlSol,
    winRatePct: r.count > 0 ? (r.wins / r.count) * 100 : 0,
  }));
}

export function listBreakers(db: DB, opts: { limit?: number } = {}): BreakerRow[] {
  const limit = clampLimit(opts.limit, 40, 200);
  const rows = db
    .prepare(
      `SELECT type, detail, tripped, at FROM breaker_events ORDER BY rowid DESC LIMIT ?`,
    )
    .all(limit) as Array<{ type: string; detail: string | null; tripped: number; at: string }>;
  return rows.map((r) => ({
    type: r.type,
    detail: r.detail,
    tripped: r.tripped === 1,
    at: r.at,
  }));
}

export function listCheckFailRates(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): Array<{ checkId: string; pass: number; fail: number; unknown: number; failRatePct: number }> {
  const mod = rangeToModifier(opts.range);
  const sql = mod
    ? `SELECT check_id AS checkId,
              SUM(CASE WHEN status = 'pass' THEN 1 ELSE 0 END) AS pass,
              SUM(CASE WHEN status = 'fail' THEN 1 ELSE 0 END) AS fail,
              SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END) AS unknown
       FROM candidate_check_results
       WHERE julianday(created_at) >= julianday('now', ?)
       GROUP BY check_id
       ORDER BY fail DESC`
    : `SELECT check_id AS checkId,
              SUM(CASE WHEN status = 'pass' THEN 1 ELSE 0 END) AS pass,
              SUM(CASE WHEN status = 'fail' THEN 1 ELSE 0 END) AS fail,
              SUM(CASE WHEN status = 'unknown' THEN 1 ELSE 0 END) AS unknown
       FROM candidate_check_results
       GROUP BY check_id
       ORDER BY fail DESC`;
  const rows = (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as Array<{
    checkId: string;
    pass: number;
    fail: number;
    unknown: number;
  }>;
  return rows.map((r) => {
    const total = r.pass + r.fail + r.unknown;
    return {
      ...r,
      failRatePct: total > 0 ? (r.fail / total) * 100 : 0,
    };
  });
}

export type VetoReasonCategory = 'unknown' | 'hard_fail' | 'low_score' | 'none';

export interface VetoReasonRow {
  reason: string;
  category: VetoReasonCategory;
  count: number;
  pct: number;
}

export interface VetoReasonBreakdown {
  totalVetoed: number;
  /** By the single primary (first) veto reason per candidate — sums to 100%. */
  primary: VetoReasonRow[];
  /** Every reason exploded from veto_reasons — a multi-reason veto appears in
   *  more than one row, so these pct values can sum to more than 100%. */
  allReasons: VetoReasonRow[];
}

function classifyVetoReason(reason: string): VetoReasonCategory {
  if (reason.startsWith('UNKNOWN:')) return 'unknown';
  if (reason === 'LOW_SCORE') return 'low_score';
  if (reason === 'NONE' || reason === '') return 'none';
  return 'hard_fail';
}

/**
 * Aggregates why candidates were vetoed. `primary` groups by the first veto
 * reason (the single dominant cause); `allReasons` explodes the full
 * `veto_reasons` array so overlapping causes are visible. Splitting
 * `UNKNOWN:*` (enrichment reliability — recoverable) from hard `Hx` fails
 * (structural rejection) and `LOW_SCORE` is the point: it tells us whether the
 * funnel is narrow because of missing data or because of real risk signals.
 */
export function getVetoReasonBreakdown(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): VetoReasonBreakdown {
  const mod = rangeToModifier(opts.range);

  const totalStmt = db.prepare(
    `SELECT COUNT(*) AS n FROM candidates
     WHERE verdict = 'veto'${mod ? ` AND julianday(created_at) >= julianday('now', ?)` : ''}`,
  );
  const totalVetoed = ((mod ? totalStmt.get(mod) : totalStmt.get()) as { n: number }).n;

  const primaryStmt = db.prepare(
    `SELECT COALESCE(primary_veto_code, 'NONE') AS reason, COUNT(*) AS count
     FROM candidates
     WHERE verdict = 'veto'${mod ? ` AND julianday(created_at) >= julianday('now', ?)` : ''}
     GROUP BY reason
     ORDER BY count DESC`,
  );
  const allStmt = db.prepare(
    `SELECT je.value AS reason, COUNT(*) AS count
     FROM candidates c, json_each(c.veto_reasons) je
     WHERE c.verdict = 'veto'${mod ? ` AND julianday(c.created_at) >= julianday('now', ?)` : ''}
     GROUP BY je.value
     ORDER BY count DESC`,
  );

  const toRows = (rows: Array<{ reason: string; count: number }>): VetoReasonRow[] =>
    rows.map((r) => ({
      reason: r.reason,
      category: classifyVetoReason(r.reason),
      count: r.count,
      pct: totalVetoed > 0 ? (r.count / totalVetoed) * 100 : 0,
    }));

  const primaryRows = (mod ? primaryStmt.all(mod) : primaryStmt.all()) as Array<{
    reason: string;
    count: number;
  }>;
  const allRows = (mod ? allStmt.all(mod) : allStmt.all()) as Array<{
    reason: string;
    count: number;
  }>;

  return {
    totalVetoed,
    primary: toRows(primaryRows),
    allReasons: toRows(allRows),
  };
}

export interface ShadowVetoQualityRow {
  primaryVetoCode: string;
  trackedN: number;
  exitSimN: number;
  legacyPeakN: number;
  /** Backward-compatible alias for exitSimN. */
  n: number;
  avgPeakMfePct: number | null;
  avgMaxMaePct: number | null;
  hit25Pct: number | null;
  hit50Pct: number | null;
  /** Fee-adjusted dry-run net PnL averages (null when no exit metrics yet). */
  avgNetPnlSol: number | null;
  winRatePct: number | null;
  expectancySol: number | null;
  avgHoldMs: number | null;
}

/** Live accepted closed-trade stats vs counterfactual veto dry-run by reason. */
export interface VetoDryRunComparison {
  range: string;
  liveAccepted: {
    n: number;
    winRatePct: number;
    expectancySol: number;
    netPnlSol: number;
    avgMfePct: number | null;
    avgMaePct: number | null;
    avgHoldMs: number | null;
  };
  /** Dry-run outcomes for vetoed candidates, grouped by primary veto reason. */
  byVetoReason: VetoDryRunGroup[];
  /** Same completed outcomes expanded across every veto code. */
  byVetoCode: VetoDryRunGroup[];
  /** Exact veto-code combinations, preserving interactions. */
  byVetoCombination: VetoDryRunGroup[];
  trackedN: number;
  exitSimN: number;
  legacyPeakN: number;
  /** Backward-compatible alias for exitSimN. */
  vetoDryRunTotal: number;
}

export interface VetoDryRunGroup {
  primaryVetoCode: string;
  trackedN: number;
  exitSimN: number;
  legacyPeakN: number;
  n: number;
  winRatePct: number;
  expectancySol: number;
  netPnlSol: number;
  avgNetPnlSol: number;
  avgPeakMfePct: number | null;
  avgMaxMaePct: number | null;
  avgHoldMs: number | null;
  hit25Pct: number | null;
  hit50Pct: number | null;
}

export interface RelaxedRiskGroup {
  kind: 'clean' | 'relaxed';
  accepted: number;
  entered: number;
  closed: number;
  winRatePct: number;
  netPnlSol: number;
  emergencyExits: number;
  avgMfePct: number;
  avgMaePct: number;
}

export interface H4ReasonCount {
  reason: string;
  count: number;
}

export interface RelaxedRiskAnalytics {
  groups: RelaxedRiskGroup[];
  h4Reasons: H4ReasonCount[];
  rollback: {
    relaxedNetPnlSol: number;
    relaxedEmergencyExits: number;
    relaxedStopLosses: number;
    relaxedFailedEntries: number;
  };
}

export interface ShadowCoverage {
  eligible: number;
  started: number;
  skippedMissingPricing: number;
  droppedCapacity: number;
}

export function getShadowCoverage(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): ShadowCoverage {
  const mod = rangeToModifier(opts.range);
  const sql = `SELECT kind, COUNT(*) AS n FROM shadow_coverage_events
    ${mod ? `WHERE julianday(created_at) >= julianday('now', ?)` : ''}
    GROUP BY kind`;
  const rows = (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as Array<{ kind: string; n: number }>;
  const counts = new Map(rows.map((r) => [r.kind, r.n]));
  return {
    eligible: counts.get('eligible') ?? 0,
    started: counts.get('started') ?? 0,
    skippedMissingPricing: counts.get('skipped_missing_pricing') ?? 0,
    droppedCapacity: counts.get('dropped_capacity') ?? 0,
  };
}

/**
 * Veto quality from shadow (counterfactual) outcomes: for each primary veto
 * reason, how often did the token we rejected pump anyway, and what would the
 * exit-policy dry-run have realized (net PnL / win rate)? A high `hit50Pct` or
 * positive expectancy on a tunable threshold check (H5/H6/H7) or `LOW_SCORE` is
 * the evidence needed to justify loosening it. Rows with tiny `n` are not actionable.
 */
export function getShadowVetoQuality(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): { totalTracked: number; exitSimN: number; legacyPeakN: number; byReason: ShadowVetoQualityRow[] } {
  const mod = rangeToModifier(opts.range);
  const timeClause = mod ? `WHERE julianday(created_at) >= julianday('now', ?)` : '';

  const totalStmt = db.prepare(
    `SELECT COUNT(*) AS trackedN,
            COALESCE(SUM(CASE WHEN outcome_version = 'exit_fsm_v1' AND net_pnl_sol IS NOT NULL THEN 1 ELSE 0 END), 0) AS exitSimN,
            COALESCE(SUM(CASE WHEN outcome_version IS NULL THEN 1 ELSE 0 END), 0) AS legacyPeakN
     FROM shadow_outcomes ${timeClause}`,
  );
  const totals = (mod ? totalStmt.get(mod) : totalStmt.get()) as {
    trackedN: number; exitSimN: number; legacyPeakN: number;
  };

  const stmt = db.prepare(
    `SELECT COALESCE(primary_veto_code, 'NONE') AS primaryVetoCode,
            COUNT(*) AS trackedN,
            SUM(CASE WHEN outcome_version = 'exit_fsm_v1' AND net_pnl_sol IS NOT NULL THEN 1 ELSE 0 END) AS exitSimN,
            SUM(CASE WHEN outcome_version IS NULL THEN 1 ELSE 0 END) AS legacyPeakN,
            AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN peak_mfe_pct END) AS avgPeakMfePct,
            AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN max_mae_pct END) AS avgMaxMaePct,
            AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN hit_25 END) * 100 AS hit25Pct,
            AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN hit_50 END) * 100 AS hit50Pct,
            AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN net_pnl_sol END) AS avgNetPnlSol,
            AVG(CASE WHEN outcome_version = 'exit_fsm_v1' AND net_pnl_sol > 0 THEN 1.0
                     WHEN outcome_version = 'exit_fsm_v1' AND net_pnl_sol IS NOT NULL THEN 0.0 END) * 100 AS winRatePct,
            AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN net_pnl_sol END) AS expectancySol,
            AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN hold_ms END) AS avgHoldMs
     FROM shadow_outcomes
     ${timeClause}
     GROUP BY primaryVetoCode
     ORDER BY trackedN DESC`,
  );
  const rows = (mod ? stmt.all(mod) : stmt.all()) as Array<{
    primaryVetoCode: string;
    trackedN: number; exitSimN: number; legacyPeakN: number;
    avgPeakMfePct: number | null; avgMaxMaePct: number | null;
    hit25Pct: number | null; hit50Pct: number | null;
    avgNetPnlSol: number | null;
    winRatePct: number | null;
    expectancySol: number | null;
    avgHoldMs: number | null;
  }>;
  return {
    totalTracked: totals.trackedN,
    exitSimN: totals.exitSimN,
    legacyPeakN: totals.legacyPeakN,
    byReason: rows.map((r) => ({
      ...r,
      n: r.exitSimN,
      avgNetPnlSol: r.avgNetPnlSol ?? null,
      winRatePct: r.winRatePct ?? null,
      expectancySol: r.expectancySol ?? null,
      avgHoldMs: r.avgHoldMs ?? null,
    })),
  };
}

/**
 * Side-by-side comparison: live accepted closed performance vs veto dry-run
 * performance grouped by primary veto reason, for the same time range.
 * Live PnL is never mixed with counterfactual veto dry-run PnL (shadow_outcomes
 * is a separate table; this query never unions them into one PnL series).
 */
export function getVetoDryRunComparison(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all'; mode?: string | null } = {},
): VetoDryRunComparison {
  const range = opts.range ?? 'all';
  const mod = rangeToModifier(range);
  const mode = opts.mode ?? null;

  const liveFilters: string[] = [`state = 'CLOSED'`];
  const liveParams: string[] = [];
  if (mod) {
    liveFilters.push(`julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)`);
    liveParams.push(mod);
  }
  if (mode) {
    liveFilters.push(`mode = ?`);
    liveParams.push(mode);
  }
  const liveWhere = liveFilters.join(' AND ');
  const liveSql = `
    WITH latest AS (${latestPositionsSql()})
    SELECT COALESCE(net_pnl_sol, pnl_sol, 0) AS netPnl,
           mfe_pct AS mfePct,
           mae_pct AS maePct,
           hold_ms AS holdMs
    FROM latest
    WHERE ${liveWhere}
    ORDER BY julianday(COALESCE(closed_at, created_at)) ASC, id ASC`;
  const liveRows = (
    liveParams.length ? db.prepare(liveSql).all(...liveParams) : db.prepare(liveSql).all()
  ) as Array<{ netPnl: number; mfePct: number | null; maePct: number | null; holdMs: number | null }>;

  const livePnls = liveRows.map((r) => r.netPnl);
  const liveStats = computePerformanceStats(livePnls);
  const liveMfe = liveRows.map((r) => r.mfePct).filter((x): x is number => x != null);
  const liveMae = liveRows.map((r) => r.maePct).filter((x): x is number => x != null);
  const liveHold = liveRows.map((r) => r.holdMs).filter((x): x is number => x != null);

  const totalSql = `SELECT COUNT(*) AS trackedN,
      COALESCE(SUM(CASE WHEN outcome_version = 'exit_fsm_v1' AND net_pnl_sol IS NOT NULL THEN 1 ELSE 0 END), 0) AS exitSimN,
      COALESCE(SUM(CASE WHEN outcome_version IS NULL THEN 1 ELSE 0 END), 0) AS legacyPeakN
    FROM shadow_outcomes ${mod ? `WHERE julianday(created_at) >= julianday('now', ?)` : ''}`;
  const totals = (mod ? db.prepare(totalSql).get(mod) : db.prepare(totalSql).get()) as {
    trackedN: number; exitSimN: number; legacyPeakN: number;
  };

  type RawGroup = {
    primaryVetoCode: string;
    trackedN: number; exitSimN: number; legacyPeakN: number;
    winRatePct: number | null;
    avgNetPnlSol: number | null;
    netPnlSol: number;
    avgPeakMfePct: number | null;
    avgMaxMaePct: number | null;
    avgHoldMs: number | null;
    hit25Pct: number | null;
    hit50Pct: number | null;
  };
  const aggregateSelect = (label: string) => `SELECT ${label} AS primaryVetoCode,
      COUNT(*) AS trackedN,
      SUM(CASE WHEN outcome_version = 'exit_fsm_v1' AND net_pnl_sol IS NOT NULL THEN 1 ELSE 0 END) AS exitSimN,
      SUM(CASE WHEN outcome_version IS NULL THEN 1 ELSE 0 END) AS legacyPeakN,
      AVG(CASE WHEN outcome_version = 'exit_fsm_v1' AND net_pnl_sol > 0 THEN 1.0
               WHEN outcome_version = 'exit_fsm_v1' AND net_pnl_sol IS NOT NULL THEN 0.0 END) * 100 AS winRatePct,
      AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN net_pnl_sol END) AS avgNetPnlSol,
      SUM(CASE WHEN outcome_version = 'exit_fsm_v1' THEN COALESCE(net_pnl_sol, 0) ELSE 0 END) AS netPnlSol,
      AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN peak_mfe_pct END) AS avgPeakMfePct,
      AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN max_mae_pct END) AS avgMaxMaePct,
      AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN hold_ms END) AS avgHoldMs,
      AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN hit_25 END) * 100 AS hit25Pct,
      AVG(CASE WHEN outcome_version = 'exit_fsm_v1' THEN hit_50 END) * 100 AS hit50Pct`;
  const timeWhere = mod ? `WHERE julianday(s.created_at) >= julianday('now', ?)` : '';
  const run = (sql: string): RawGroup[] => (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as RawGroup[];
  const reasonRows = run(`${aggregateSelect("COALESCE(primary_veto_code, 'NONE')")}
    FROM shadow_outcomes s ${timeWhere} GROUP BY primaryVetoCode ORDER BY trackedN DESC`);
  const codeRows = run(`${aggregateSelect("COALESCE(je.value, 'NONE')")}
    FROM shadow_outcomes s
    LEFT JOIN json_each(COALESCE(s.veto_codes_json, json_array(s.primary_veto_code))) je
    ${timeWhere} GROUP BY primaryVetoCode ORDER BY trackedN DESC`);
  const combinationRows = run(`${aggregateSelect("COALESCE(veto_codes_json, json_array(primary_veto_code), '[]')")}
    FROM shadow_outcomes s ${timeWhere} GROUP BY primaryVetoCode ORDER BY trackedN DESC`);
  const mapGroups = (rows: RawGroup[]): VetoDryRunGroup[] => rows.map((r) => ({
    primaryVetoCode: r.primaryVetoCode,
    trackedN: r.trackedN,
    exitSimN: r.exitSimN,
    legacyPeakN: r.legacyPeakN,
    n: r.exitSimN,
    winRatePct: r.winRatePct ?? 0,
    expectancySol: r.avgNetPnlSol ?? 0,
    netPnlSol: r.netPnlSol,
    avgNetPnlSol: r.avgNetPnlSol ?? 0,
    avgPeakMfePct: r.avgPeakMfePct,
    avgMaxMaePct: r.avgMaxMaePct,
    avgHoldMs: r.avgHoldMs,
    hit25Pct: r.hit25Pct,
    hit50Pct: r.hit50Pct,
  }));

  return {
    range,
    liveAccepted: {
      n: liveStats.tradeCount,
      winRatePct: liveStats.winRatePct,
      expectancySol: liveStats.expectancySol,
      netPnlSol: liveStats.realizedPnlSol,
      avgMfePct: liveMfe.length ? liveMfe.reduce((a, b) => a + b, 0) / liveMfe.length : null,
      avgMaePct: liveMae.length ? liveMae.reduce((a, b) => a + b, 0) / liveMae.length : null,
      avgHoldMs: liveHold.length ? liveHold.reduce((a, b) => a + b, 0) / liveHold.length : null,
    },
    byVetoReason: mapGroups(reasonRows),
    byVetoCode: mapGroups(codeRows),
    byVetoCombination: mapGroups(combinationRows),
    trackedN: totals.trackedN,
    exitSimN: totals.exitSimN,
    legacyPeakN: totals.legacyPeakN,
    vetoDryRunTotal: totals.exitSimN,
  };
}

export function getRelaxedRiskAnalytics(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): RelaxedRiskAnalytics {
  const mod = rangeToModifier(opts.range);
  const candTime = mod ? `AND julianday(created_at) >= julianday('now', ?)` : '';
  const posTime = mod ? `AND julianday(created_at) >= julianday('now', ?)` : '';
  const closedTime = mod ? `AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)` : '';

  const acceptedRows = (
    mod
      ? db.prepare(
          `SELECT relaxed_risk AS relaxedRisk, COUNT(*) AS n
           FROM candidates
           WHERE verdict = 'accept' ${candTime}
           GROUP BY relaxed_risk`,
        ).all(mod)
      : db.prepare(
          `SELECT relaxed_risk AS relaxedRisk, COUNT(*) AS n
           FROM candidates
           WHERE verdict = 'accept'
           GROUP BY relaxed_risk`,
        ).all()
  ) as Array<{ relaxedRisk: number; n: number }>;

  const enteredRows = (
    mod
      ? db.prepare(
          `WITH latest AS (${latestPositionsSql()})
           SELECT relaxed_risk AS relaxedRisk, COUNT(*) AS n
           FROM latest
           WHERE state IN ('PENDING_ENTRY','OPEN','EXITING','CLOSED','FAILED') ${posTime}
           GROUP BY relaxed_risk`,
        ).all(mod)
      : db.prepare(
          `WITH latest AS (${latestPositionsSql()})
           SELECT relaxed_risk AS relaxedRisk, COUNT(*) AS n
           FROM latest
           WHERE state IN ('PENDING_ENTRY','OPEN','EXITING','CLOSED','FAILED')
           GROUP BY relaxed_risk`,
        ).all()
  ) as Array<{ relaxedRisk: number; n: number }>;

  const closedRows = (
    mod
      ? db.prepare(
          `SELECT relaxed_risk AS relaxedRisk,
                  COUNT(*) AS closed,
                  COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) > 0 THEN 1 ELSE 0 END), 0) AS wins,
                  COALESCE(SUM(COALESCE(net_pnl_sol, pnl_sol)), 0) AS netPnlSol,
                  COALESCE(SUM(CASE WHEN exit_reason = 'EMERGENCY_EXIT' THEN 1 ELSE 0 END), 0) AS emergencyExits,
                  COALESCE(AVG(mfe_pct), 0) AS avgMfePct,
                  COALESCE(AVG(mae_pct), 0) AS avgMaePct
           FROM positions
           WHERE state = 'CLOSED' ${closedTime}
           GROUP BY relaxed_risk`,
        ).all(mod)
      : db.prepare(
          `SELECT relaxed_risk AS relaxedRisk,
                  COUNT(*) AS closed,
                  COALESCE(SUM(CASE WHEN COALESCE(net_pnl_sol, pnl_sol) > 0 THEN 1 ELSE 0 END), 0) AS wins,
                  COALESCE(SUM(COALESCE(net_pnl_sol, pnl_sol)), 0) AS netPnlSol,
                  COALESCE(SUM(CASE WHEN exit_reason = 'EMERGENCY_EXIT' THEN 1 ELSE 0 END), 0) AS emergencyExits,
                  COALESCE(AVG(mfe_pct), 0) AS avgMfePct,
                  COALESCE(AVG(mae_pct), 0) AS avgMaePct
           FROM positions
           WHERE state = 'CLOSED'
           GROUP BY relaxed_risk`,
        ).all()
  ) as Array<{
    relaxedRisk: number;
    closed: number;
    wins: number;
    netPnlSol: number;
    emergencyExits: number;
    avgMfePct: number;
    avgMaePct: number;
  }>;

  const h4Rows = (
    mod
      ? db.prepare(
          `SELECT COALESCE(json_extract(je.value, '$.reason'), 'none') AS reason, COUNT(*) AS count
           FROM candidates c, json_each(c.hard_check_results) je
           WHERE json_extract(je.value, '$.id') = 'H4'
             AND julianday(c.created_at) >= julianday('now', ?)
           GROUP BY reason
           ORDER BY count DESC`,
        ).all(mod)
      : db.prepare(
          `SELECT COALESCE(json_extract(je.value, '$.reason'), 'none') AS reason, COUNT(*) AS count
           FROM candidates c, json_each(c.hard_check_results) je
           WHERE json_extract(je.value, '$.id') = 'H4'
           GROUP BY reason
           ORDER BY count DESC`,
        ).all()
  ) as unknown as H4ReasonCount[];

  const rollback = (
    mod
      ? db.prepare(
          `SELECT
             COALESCE(SUM(CASE WHEN state = 'CLOSED' THEN COALESCE(net_pnl_sol, pnl_sol) ELSE 0 END), 0) AS relaxedNetPnlSol,
             COALESCE(SUM(CASE WHEN state = 'CLOSED' AND exit_reason = 'EMERGENCY_EXIT' THEN 1 ELSE 0 END), 0) AS relaxedEmergencyExits,
             COALESCE(SUM(CASE WHEN state = 'CLOSED' AND exit_reason = 'STOP_LOSS' THEN 1 ELSE 0 END), 0) AS relaxedStopLosses,
             COALESCE(SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END), 0) AS relaxedFailedEntries
           FROM positions
           WHERE relaxed_risk = 1 ${posTime}`,
        ).get(mod)
      : db.prepare(
          `SELECT
             COALESCE(SUM(CASE WHEN state = 'CLOSED' THEN COALESCE(net_pnl_sol, pnl_sol) ELSE 0 END), 0) AS relaxedNetPnlSol,
             COALESCE(SUM(CASE WHEN state = 'CLOSED' AND exit_reason = 'EMERGENCY_EXIT' THEN 1 ELSE 0 END), 0) AS relaxedEmergencyExits,
             COALESCE(SUM(CASE WHEN state = 'CLOSED' AND exit_reason = 'STOP_LOSS' THEN 1 ELSE 0 END), 0) AS relaxedStopLosses,
             COALESCE(SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END), 0) AS relaxedFailedEntries
           FROM positions
           WHERE relaxed_risk = 1`,
        ).get()
  ) as RelaxedRiskAnalytics['rollback'];

  const accepted = byRelaxed(acceptedRows, 'n');
  const entered = byRelaxed(enteredRows, 'n');
  const closed = new Map(closedRows.map((r) => [Number(r.relaxedRisk) === 1 ? 'relaxed' : 'clean', r]));
  const groups: RelaxedRiskGroup[] = (['clean', 'relaxed'] as const).map((kind) => {
    const c = closed.get(kind);
    const closedCount = c?.closed ?? 0;
    return {
      kind,
      accepted: accepted.get(kind) ?? 0,
      entered: entered.get(kind) ?? 0,
      closed: closedCount,
      winRatePct: closedCount > 0 ? ((c?.wins ?? 0) / closedCount) * 100 : 0,
      netPnlSol: c?.netPnlSol ?? 0,
      emergencyExits: c?.emergencyExits ?? 0,
      avgMfePct: c?.avgMfePct ?? 0,
      avgMaePct: c?.avgMaePct ?? 0,
    };
  });

  return { groups, h4Reasons: h4Rows, rollback };
}

/** Mint-level veto dry-run outcomes for the operator table (shadow_outcomes only). */
export interface ShadowOutcomeRow {
  id: number;
  mint: string;
  verdict: string;
  primaryVetoCode: string | null;
  vetoCodes: string[];
  baselinePrice: number;
  peakPrice: number | null;
  troughPrice: number | null;
  peakMfePct: number | null;
  maxMaePct: number | null;
  hit25: boolean;
  hit50: boolean;
  netPnlSol: number | null;
  grossPnlSol: number | null;
  feesSol: number | null;
  pnlPct: number | null;
  exitReason: string | null;
  holdMs: number | null;
  sizeSol: number | null;
  samples: number;
  trackedMs: number | null;
  outcomeVersion: string | null;
  createdAt: string;
}

export function listShadowOutcomes(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all'; limit?: number } = {},
): ShadowOutcomeRow[] {
  const limit = clampLimit(opts.limit, 80, 500);
  const mod = rangeToModifier(opts.range);
  const sql = mod
    ? `SELECT rowid AS id, mint, verdict, primary_veto_code, veto_codes_json, baseline_price,
              peak_price, trough_price, peak_mfe_pct, max_mae_pct, hit_25, hit_50,
              net_pnl_sol, gross_pnl_sol, fees_sol, pnl_pct, exit_reason, hold_ms,
              size_sol, samples, tracked_ms, outcome_version, created_at
       FROM shadow_outcomes
       WHERE julianday(created_at) >= julianday('now', ?)
       ORDER BY julianday(created_at) DESC, rowid DESC
       LIMIT ?`
    : `SELECT rowid AS id, mint, verdict, primary_veto_code, veto_codes_json, baseline_price,
              peak_price, trough_price, peak_mfe_pct, max_mae_pct, hit_25, hit_50,
              net_pnl_sol, gross_pnl_sol, fees_sol, pnl_pct, exit_reason, hold_ms,
              size_sol, samples, tracked_ms, outcome_version, created_at
       FROM shadow_outcomes
       ORDER BY julianday(created_at) DESC, rowid DESC
       LIMIT ?`;
  const rows = (
    mod ? db.prepare(sql).all(mod, limit) : db.prepare(sql).all(limit)
  ) as Array<{
    id: number;
    mint: string;
    verdict: string;
    primary_veto_code: string | null;
    veto_codes_json: string | null;
    baseline_price: number;
    peak_price: number | null;
    trough_price: number | null;
    peak_mfe_pct: number | null;
    max_mae_pct: number | null;
    hit_25: number;
    hit_50: number;
    net_pnl_sol: number | null;
    gross_pnl_sol: number | null;
    fees_sol: number | null;
    pnl_pct: number | null;
    exit_reason: string | null;
    hold_ms: number | null;
    size_sol: number | null;
    samples: number;
    tracked_ms: number | null;
    outcome_version: string | null;
    created_at: string;
  }>;

  return rows.map((row) => ({
    id: row.id,
    mint: row.mint,
    verdict: row.verdict,
    primaryVetoCode: row.primary_veto_code,
    vetoCodes: parseJsonArray(row.veto_codes_json),
    baselinePrice: row.baseline_price,
    peakPrice: row.peak_price,
    troughPrice: row.trough_price,
    peakMfePct: row.peak_mfe_pct,
    maxMaePct: row.max_mae_pct,
    hit25: row.hit_25 === 1,
    hit50: row.hit_50 === 1,
    netPnlSol: row.net_pnl_sol,
    grossPnlSol: row.gross_pnl_sol,
    feesSol: row.fees_sol,
    pnlPct: row.pnl_pct,
    exitReason: row.exit_reason,
    holdMs: row.hold_ms,
    sizeSol: row.size_sol,
    samples: row.samples,
    trackedMs: row.tracked_ms,
    outcomeVersion: row.outcome_version,
    createdAt: row.created_at,
  }));
}

export function listPositions(
  db: DB,
  opts: { state?: 'open' | 'closed' | 'all'; limit?: number } = {},
): DashboardPosition[] {
  const state = opts.state ?? 'all';
  const limit = clampLimit(opts.limit, 50, 200);
  const activeList = ACTIVE_STATES.map((s) => `'${s}'`).join(',');
  let sql: string;

  if (state === 'closed') {
    sql = `SELECT rowid AS id, * FROM positions WHERE state = 'CLOSED' ORDER BY COALESCE(closed_at, created_at) DESC, rowid DESC LIMIT ?`;
  } else if (state === 'open') {
    sql = `WITH latest AS (${latestPositionsSql()}) SELECT * FROM latest WHERE state IN (${activeList}) ORDER BY created_at DESC, id DESC LIMIT ?`;
  } else {
    sql = `WITH latest AS (${latestPositionsSql()}) SELECT * FROM latest ORDER BY created_at DESC, id DESC LIMIT ?`;
  }

  const marks = latestMarks(db);
  return db.prepare(sql).all(limit).map((row) => mapPosition(row as Row, marks));
}

export function getPnlSeries(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' } = {},
): PnlPoint[] {
  const modifier = opts.range === '30d' ? '-30 days' : opts.range === '24h' ? '-1 day' : '-7 days';
  const rows = db
    .prepare(
      `SELECT mint, COALESCE(net_pnl_sol, pnl_sol) AS pnl_sol, COALESCE(closed_at, created_at) AS time
       FROM positions
       WHERE state = 'CLOSED'
         AND COALESCE(closed_at, created_at) IS NOT NULL
         AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)
       ORDER BY julianday(COALESCE(closed_at, created_at)) ASC, rowid ASC`,
    )
    .all(modifier) as Array<{ mint: string; pnl_sol: number | null; time: string }>;

  let cumulative = 0;
  return rows.map((row) => {
    const pnl = row.pnl_sol ?? 0;
    cumulative += pnl;
    return { time: row.time, mint: row.mint, pnlSol: pnl, cumulativePnlSol: cumulative };
  });
}

export function listCandidates(db: DB, opts: { limit?: number } = {}): DashboardCandidate[] {
  const limit = clampLimit(opts.limit, 40, 150);
  const rows = db
    .prepare(
      `SELECT rowid AS id, mint, verdict, soft_score, veto_reasons, high_volatility,
              relaxed_risk, relaxed_reasons_json, created_at, hard_check_results
       FROM candidates
       ORDER BY created_at DESC, rowid DESC
       LIMIT ?`,
    )
    .all(limit) as Array<{
      id: number;
      mint: string;
      verdict: string | null;
      soft_score: number | null;
      veto_reasons: string | null;
      high_volatility: number;
      relaxed_risk: number;
      relaxed_reasons_json: string | null;
      created_at: string;
      hard_check_results: string | null;
    }>;

  return rows.map((row) => {
    const hardChecks = parseHardChecks(row.hard_check_results);
    return {
      id: row.id,
      mint: row.mint,
      verdict: row.verdict,
      softScore: row.soft_score,
      vetoReasons: parseJsonArray(row.veto_reasons),
      highVolatility: row.high_volatility === 1,
      relaxedRisk: row.relaxed_risk === 1,
      relaxedReasons: parseJsonArray(row.relaxed_reasons_json),
      createdAt: row.created_at,
      ...(hardChecks ? { hardChecks } : {}),
    };
  });
}

export function listEvents(
  db: DB,
  opts: { limit?: number; level?: OperatorEventLevel; category?: string } = {},
): DashboardEvent[] {
  const limit = clampLimit(opts.limit, 80, 300);
  const clauses: string[] = [];
  const params: Array<string | number> = [];
  if (opts.level) {
    clauses.push('level = ?');
    params.push(opts.level);
  }
  if (opts.category) {
    clauses.push('category = ?');
    params.push(opts.category);
  }
  params.push(limit);
  const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
  const rows = db
    .prepare(
      `SELECT id, category, level, message, entity_mint, payload_json, created_at
       FROM operator_events
       ${where}
       ORDER BY created_at DESC, id DESC
       LIMIT ?`,
    )
    .all(...params) as Array<{
      id: number;
      category: string;
      level: OperatorEventLevel;
      message: string;
      entity_mint: string | null;
      payload_json: string | null;
      created_at: string;
    }>;
  return rows.map((row) => ({
    id: row.id,
    category: row.category,
    level: row.level,
    message: row.message,
    entityMint: row.entity_mint,
    payload: parseJson(row.payload_json),
    createdAt: row.created_at,
  }));
}

export function buildTradeBlotterCsv(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): string {
  const mod = rangeToModifier(opts.range);
  const sql = mod
    ? `SELECT mint, state, size_sol, entry_price, exit_price, exit_reason,
              COALESCE(gross_pnl_sol, pnl_sol) AS gross_pnl_sol, fees_sol,
              COALESCE(net_pnl_sol, pnl_sol) AS net_pnl_sol, pnl_pct,
              mfe_pct, mae_pct, hold_ms, exit_trigger_to_confirm_ms,
              entry_soft_score, high_volatility, feed_source, venue, mode,
              relaxed_risk, relaxed_reasons_json,
              opened_at, closed_at, entry_tx, exit_tx
       FROM positions
       WHERE state = 'CLOSED'
         AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)
       ORDER BY julianday(COALESCE(closed_at, created_at)) ASC, rowid ASC`
    : `SELECT mint, state, size_sol, entry_price, exit_price, exit_reason,
              COALESCE(gross_pnl_sol, pnl_sol) AS gross_pnl_sol, fees_sol,
              COALESCE(net_pnl_sol, pnl_sol) AS net_pnl_sol, pnl_pct,
              mfe_pct, mae_pct, hold_ms, exit_trigger_to_confirm_ms,
              entry_soft_score, high_volatility, feed_source, venue, mode,
              relaxed_risk, relaxed_reasons_json,
              opened_at, closed_at, entry_tx, exit_tx
       FROM positions
       WHERE state = 'CLOSED'
       ORDER BY julianday(COALESCE(closed_at, created_at)) ASC, rowid ASC`;
  const rows = (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as Array<Record<string, unknown>>;
  const headers = [
    'mint',
    'state',
    'size_sol',
    'entry_price',
    'exit_price',
    'exit_reason',
    'gross_pnl_sol',
    'fees_sol',
    'net_pnl_sol',
    'pnl_pct',
    'mfe_pct',
    'mae_pct',
    'hold_ms',
    'exit_trigger_to_confirm_ms',
    'entry_soft_score',
    'high_volatility',
    'feed_source',
    'venue',
    'mode',
    'relaxed_risk',
    'relaxed_reasons_json',
    'opened_at',
    'closed_at',
    'entry_tx',
    'exit_tx',
  ];
  const lines = [headers.join(',')];
  for (const row of rows) {
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

/**
 * Detail blotter of capital-free veto dry-runs (`shadow_outcomes` only).
 * Never includes live positions — counterfactual performance only.
 */
export function buildVetoDryRunCsv(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): string {
  const mod = rangeToModifier(opts.range);
  const sql = `WITH latest_candidates AS (
      SELECT c.* FROM candidates c
      WHERE c.rowid = (SELECT MAX(c2.rowid) FROM candidates c2 WHERE c2.mint = c.mint)
    )
    SELECT s.mint, s.verdict, s.primary_veto_code, s.veto_codes_json,
           COALESCE(s.outcome_version,
             CASE WHEN s.net_pnl_sol IS NULL THEN 'legacy_peak_only' ELSE 'exit_fsm_v1' END) AS outcome_version,
           CASE WHEN s.outcome_version = 'exit_fsm_v1' AND s.net_pnl_sol IS NOT NULL THEN 1 ELSE 0 END AS completed_exit_sim,
           s.baseline_price, s.peak_price, s.trough_price, s.peak_mfe_pct, s.max_mae_pct,
           s.hit_25, s.hit_50, s.net_pnl_sol, s.gross_pnl_sol, s.fees_sol, s.pnl_pct,
           s.exit_reason, s.hold_ms, s.size_sol, s.samples, s.tracked_ms,
           c.soft_score, c.top10_share, c.max_holder_share, c.creator_share,
           c.pool_sol_at_entry, c.buy_impact_pct, c.early_flow_net_sol, c.early_flow_rate,
           c.sellability_reason, c.sellability_tx_bytes, c.sellability_used_lookup_table,
           c.hard_check_results, s.created_at
    FROM shadow_outcomes s
    LEFT JOIN latest_candidates c ON c.mint = s.mint
    ${mod ? `WHERE julianday(s.created_at) >= julianday('now', ?)` : ''}
    ORDER BY julianday(s.created_at) ASC, s.rowid ASC`;
  const rows = (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as Array<Record<string, unknown>>;
  const headers = [
    'mint',
    'verdict',
    'primary_veto_code',
    'veto_codes_json',
    'outcome_version',
    'completed_exit_sim',
    'baseline_price',
    'peak_price',
    'trough_price',
    'peak_mfe_pct',
    'max_mae_pct',
    'hit_25',
    'hit_50',
    'net_pnl_sol',
    'gross_pnl_sol',
    'fees_sol',
    'pnl_pct',
    'exit_reason',
    'hold_ms',
    'size_sol',
    'samples',
    'tracked_ms',
    'soft_score',
    'top10_share',
    'max_holder_share',
    'creator_share',
    'pool_sol_at_entry',
    'buy_impact_pct',
    'early_flow_net_sol',
    'early_flow_rate',
    'sellability_reason',
    'sellability_tx_bytes',
    'sellability_used_lookup_table',
    'hard_check_results',
    'created_at',
  ];
  const lines = [headers.join(',')];
  for (const row of rows) {
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

/**
 * Operator snapshot CSV: live accepted headline + per-veto-reason dry-run rows.
 * Aggregation is shipped `getVetoDryRunComparison` (not reimplemented here).
 */
export function buildVetoDryRunSummaryCsv(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' | 'all'; mode?: string | null } = {},
): string {
  const range = opts.range ?? 'all';
  const cmp = getVetoDryRunComparison(db, {
    range,
    ...(opts.mode !== undefined ? { mode: opts.mode } : {}),
  });
  const lines: string[] = [];
  lines.push('# Veto dry-run performance snapshot (counterfactual; not live capital)');
  lines.push(`# range=${cmp.range}`);
  lines.push(
    `# live_accepted n=${cmp.liveAccepted.n} win_pct=${cmp.liveAccepted.winRatePct.toFixed(2)} ` +
      `expectancy_sol=${cmp.liveAccepted.expectancySol} net_pnl_sol=${cmp.liveAccepted.netPnlSol} ` +
      `avg_mfe_pct=${cmp.liveAccepted.avgMfePct ?? ''} avg_mae_pct=${cmp.liveAccepted.avgMaePct ?? ''} ` +
      `avg_hold_ms=${cmp.liveAccepted.avgHoldMs ?? ''}`,
  );
  lines.push(`# tracked_n=${cmp.trackedN} exit_sim_n=${cmp.exitSimN} legacy_peak_n=${cmp.legacyPeakN}`);
  const headers = [
    'cohort_type',
    'primary_veto_code',
    'tracked_n',
    'exit_sim_n',
    'legacy_peak_n',
    'n',
    'win_rate_pct',
    'expectancy_sol',
    'net_pnl_sol',
    'avg_net_pnl_sol',
    'avg_peak_mfe_pct',
    'avg_max_mae_pct',
    'hit_25_pct',
    'hit_50_pct',
    'avg_hold_ms',
  ];
  lines.push(headers.join(','));
  const cohorts: Array<{ type: string; rows: VetoDryRunGroup[] }> = [
    { type: 'primary', rows: cmp.byVetoReason },
    { type: 'all_code', rows: cmp.byVetoCode },
    { type: 'combination', rows: cmp.byVetoCombination },
  ];
  for (const cohort of cohorts) for (const r of cohort.rows) {
    const row: Record<string, unknown> = {
      cohort_type: cohort.type,
      primary_veto_code: r.primaryVetoCode,
      tracked_n: r.trackedN,
      exit_sim_n: r.exitSimN,
      legacy_peak_n: r.legacyPeakN,
      n: r.n,
      win_rate_pct: r.winRatePct,
      expectancy_sol: r.expectancySol,
      net_pnl_sol: r.netPnlSol,
      avg_net_pnl_sol: r.avgNetPnlSol,
      avg_peak_mfe_pct: r.avgPeakMfePct,
      avg_max_mae_pct: r.avgMaxMaePct,
      hit_25_pct: r.hit25Pct,
      hit_50_pct: r.hit50Pct,
      avg_hold_ms: r.avgHoldMs,
    };
    lines.push(headers.map((h) => csvEscape(row[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

export function buildOpsReport(
  db: DB,
  config: Config,
  risk: RiskSnapshot | null,
): Record<string, unknown> {
  const summary = getDashboardSummary(db, config);
  return {
    generatedAt: new Date().toISOString(),
    mode: config.mode,
    risk,
    summary,
    breakers: listBreakers(db, { limit: 20 }),
    funnel: getFunnelAnalytics(db, { range: '24h' }),
    relaxedRisk: getRelaxedRiskAnalytics(db, { range: '24h' }),
  };
}

export function buildSoakReport(
  db: DB,
  config: Config,
  opts: { range?: '24h' | '7d' | '30d' | 'all' } = {},
): Record<string, unknown> {
  const range = opts.range ?? '7d';
  const perf = getPerformanceAnalytics(db, { range });
  const funnel = getFunnelAnalytics(db, { range });
  return {
    generatedAt: new Date().toISOString(),
    mode: config.mode,
    range,
    feeAware: true,
    sampleSize: perf.tradeCount,
    performance: perf,
    funnel,
    checkFailRates: listCheckFailRates(db, { range }),
    relaxedRisk: getRelaxedRiskAnalytics(db, { range }),
    caveats: [
      'Expectancy and profit factor need adequate sample size (prefer 50+ closed trades).',
      'Paper/dry-run fees are estimated; live fees come from execution when recorded.',
      'Do not mix paper and live numbers without filtering by mode.',
    ],
  };
}

export function buildFunnelCsv(db: DB, opts: { range?: '24h' | '7d' | '30d' | 'all' } = {}): string {
  const rates = listCheckFailRates(db, opts);
  const headers = ['check_id', 'pass', 'fail', 'unknown', 'fail_rate_pct'];
  const lines = [headers.join(',')];
  for (const r of rates) {
    lines.push([r.checkId, r.pass, r.fail, r.unknown, r.failRatePct.toFixed(2)].join(','));
  }
  return lines.join('\n') + '\n';
}

function computeUnrealizedPnl(db: DB): number {
  const rows = db
    .prepare(
      `WITH latest AS (${latestPositionsSql()}),
            marks AS (
              SELECT mint, price
              FROM price_ticks
              WHERE rowid IN (SELECT MAX(rowid) FROM price_ticks GROUP BY mint)
            )
       SELECT l.size_sol AS sizeSol, l.entry_price AS entryPrice, m.price AS mark
       FROM latest l
       LEFT JOIN marks m ON m.mint = l.mint
       WHERE l.state IN ('OPEN', 'EXITING')`,
    )
    .all() as Array<{ sizeSol: number; entryPrice: number | null; mark: number | null }>;

  let total = 0;
  for (const row of rows) {
    if (row.entryPrice && row.entryPrice > 0 && row.mark && row.mark > 0) {
      total += row.sizeSol * (row.mark / row.entryPrice - 1);
    }
  }
  return total;
}

function latestMarks(db: DB): Map<string, number> {
  const rows = db
    .prepare(
      `SELECT mint, price FROM price_ticks
       WHERE rowid IN (SELECT MAX(rowid) FROM price_ticks GROUP BY mint)`,
    )
    .all() as Array<{ mint: string; price: number }>;
  return new Map(rows.map((r) => [r.mint, r.price]));
}

function latestPositionsSql(): string {
  return `SELECT rowid AS id, * FROM positions WHERE rowid IN (SELECT MAX(rowid) FROM positions GROUP BY mint)`;
}

function mapPosition(row: Row, marks: Map<string, number>): DashboardPosition {
  const mint = String(row['mint']);
  const state = String(row['state']);
  const entryPrice = nullableNumber(row['entry_price']);
  const sizeSol = Number(row['size_sol']);
  let unrealizedSol: number | null = null;
  if ((state === 'OPEN' || state === 'EXITING') && entryPrice && entryPrice > 0) {
    const mark = marks.get(mint);
    if (mark && mark > 0) unrealizedSol = sizeSol * (mark / entryPrice - 1);
  }
  const hv = row['high_volatility'];
  return {
    id: Number(row['id']),
    mint,
    state,
    entryTx: nullableString(row['entry_tx']),
    entryPrice,
    exitPrice: nullableNumber(row['exit_price']),
    sizeSol,
    exitReason: nullableString(row['exit_reason']),
    exitTx: nullableString(row['exit_tx']),
    pnlSol: nullableNumber(row['pnl_sol']),
    pnlPct: nullableNumber(row['pnl_pct']),
    grossPnlSol: nullableNumber(row['gross_pnl_sol']),
    feesSol: nullableNumber(row['fees_sol']),
    netPnlSol: nullableNumber(row['net_pnl_sol']),
    entrySoftScore: nullableNumber(row['entry_soft_score']),
    highVolatility: hv === null || hv === undefined ? null : Number(hv) === 1,
    mfePct: nullableNumber(row['mfe_pct']),
    maePct: nullableNumber(row['mae_pct']),
    holdMs: nullableNumber(row['hold_ms']),
    exitTriggerToConfirmMs: nullableNumber(row['exit_trigger_to_confirm_ms']),
    feedSource: nullableString(row['feed_source']),
    venue: nullableString(row['venue']),
    mode: nullableString(row['mode']),
    relaxedRisk: Number(row['relaxed_risk'] ?? 0) === 1,
    relaxedReasons: parseJsonArray(nullableString(row['relaxed_reasons_json'])),
    openedAt: nullableString(row['opened_at']),
    closedAt: nullableString(row['closed_at']),
    createdAt: String(row['created_at']),
    unrealizedSol,
  };
}

function clampLimit(value: number | undefined, fallback: number, max: number): number {
  if (!value || !Number.isFinite(value)) return fallback;
  return Math.max(1, Math.min(Math.trunc(value), max));
}

function one<T>(db: DB, sql: string): T {
  return db.prepare(sql).get() as T;
}

function byRelaxed<T extends Record<string, unknown>>(rows: T[], countKey: keyof T): Map<'clean' | 'relaxed', number> {
  return new Map(
    rows.map((r) => [
      Number(r['relaxedRisk']) === 1 ? 'relaxed' : 'clean',
      typeof r[countKey] === 'number' ? r[countKey] : Number(r[countKey] ?? 0),
    ]),
  );
}

function nullableString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === 'number' ? value : null;
}

function parseJson(text: string | null): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function parseJsonArray(text: string | null): string[] {
  const parsed = parseJson(text);
  return Array.isArray(parsed) ? parsed.map(String) : [];
}

function parseHardChecks(
  text: string | null,
): Array<{ id: string; label?: string; status: string; detail?: string; reason?: string }> | undefined {
  const parsed = parseJson(text);
  if (!Array.isArray(parsed)) return undefined;
  return parsed.map((item) => {
    const o = item as Record<string, unknown>;
    return {
      id: String(o.id ?? ''),
      ...(typeof o.label === 'string' ? { label: o.label } : {}),
      status: String(o.status ?? 'unknown'),
      ...(typeof o.detail === 'string' ? { detail: o.detail } : {}),
      ...(typeof o.reason === 'string' ? { reason: o.reason } : {}),
    };
  });
}

function csvEscape(value: unknown): string {
  if (value === null || value === undefined) return '';
  const s = String(value);
  if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}
