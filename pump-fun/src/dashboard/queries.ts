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
  createdAt: string;
  hardChecks?: Array<{ id: string; label?: string; status: string; detail?: string }>;
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
       (SELECT COUNT(*) FROM positions WHERE state IN ('PENDING_ENTRY','OPEN','EXITING','CLOSED','FAILED') ${whereTime}) AS entered,
       (SELECT COUNT(*) FROM positions WHERE state = 'CLOSED' ${whereClosed}) AS closed,
       (SELECT COUNT(*) FROM positions WHERE state = 'FAILED' ${whereTime}) AS failed`,
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
      `SELECT rowid AS id, mint, verdict, soft_score, veto_reasons, high_volatility, created_at, hard_check_results
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
): Array<{ id: string; label?: string; status: string; detail?: string }> | undefined {
  const parsed = parseJson(text);
  if (!Array.isArray(parsed)) return undefined;
  return parsed.map((item) => {
    const o = item as Record<string, unknown>;
    return {
      id: String(o.id ?? ''),
      ...(typeof o.label === 'string' ? { label: o.label } : {}),
      status: String(o.status ?? 'unknown'),
      ...(typeof o.detail === 'string' ? { detail: o.detail } : {}),
    };
  });
}

function csvEscape(value: unknown): string {
  if (value === null || value === undefined) return '';
  const s = String(value);
  if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}
