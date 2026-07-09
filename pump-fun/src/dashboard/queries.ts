import type { DB } from '../persistence/db.ts';
import type { Config } from '../config/schema.ts';
import type { OperatorEventLevel } from '../persistence/repositories.ts';

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
  };
  positions: {
    openCount: number;
    openExposureSol: number;
    maxConcurrent: number;
  };
  flow: {
    graduations: number;
    accepted: number;
    vetoed: number;
    highVolatility: number;
  };
  system: {
    latestBreaker: { type: string; detail: string | null; tripped: boolean; at: string } | null;
    latestEventAt: string | null;
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
  openedAt: string | null;
  closedAt: string | null;
  createdAt: string;
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

export function getDashboardSummary(db: DB, config: Config): DashboardSummary {
  const closed = one<{ total: number; count: number; wins: number; losses: number }>(
    db,
    `SELECT
       COALESCE(SUM(pnl_sol), 0) AS total,
       COUNT(*) AS count,
       COALESCE(SUM(CASE WHEN pnl_sol > 0 THEN 1 ELSE 0 END), 0) AS wins,
       COALESCE(SUM(CASE WHEN pnl_sol <= 0 THEN 1 ELSE 0 END), 0) AS losses
     FROM positions
     WHERE state = 'CLOSED'`,
  );
  const recent = one<{ pnl24h: number; pnl7d: number }>(
    db,
    `SELECT
       COALESCE(SUM(CASE WHEN closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now', '-1 day') THEN pnl_sol ELSE 0 END), 0) AS pnl24h,
       COALESCE(SUM(CASE WHEN closed_at IS NOT NULL AND julianday(closed_at) >= julianday('now', '-7 days') THEN pnl_sol ELSE 0 END), 0) AS pnl7d
     FROM positions
     WHERE state = 'CLOSED'`,
  );
  const active = one<{ count: number; exposure: number }>(
    db,
    `WITH latest AS (${latestPositionsSql()})
     SELECT COUNT(*) AS count, COALESCE(SUM(size_sol), 0) AS exposure
     FROM latest
     WHERE state IN (${ACTIVE_STATES.map((s) => `'${s}'`).join(',')})`,
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
    },
    positions: {
      openCount: active.count,
      openExposureSol: active.exposure,
      maxConcurrent: config.risk.maxConcurrentPositions,
    },
    flow: {
      graduations: flow.graduations,
      accepted: flow.accepted,
      vetoed: flow.vetoed,
      highVolatility: flow.highVol,
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
    },
  };
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

  return db.prepare(sql).all(limit).map(mapPosition);
}

export function getPnlSeries(
  db: DB,
  opts: { range?: '24h' | '7d' | '30d' } = {},
): PnlPoint[] {
  const modifier = opts.range === '30d' ? '-30 days' : opts.range === '24h' ? '-1 day' : '-7 days';
  const rows = db
    .prepare(
      `SELECT mint, pnl_sol, COALESCE(closed_at, created_at) AS time
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
      `SELECT rowid AS id, mint, verdict, soft_score, veto_reasons, high_volatility, created_at
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
    }>;

  return rows.map((row) => ({
    id: row.id,
    mint: row.mint,
    verdict: row.verdict,
    softScore: row.soft_score,
    vetoReasons: parseJsonArray(row.veto_reasons),
    highVolatility: row.high_volatility === 1,
    createdAt: row.created_at,
  }));
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

function latestPositionsSql(): string {
  return `SELECT rowid AS id, * FROM positions WHERE rowid IN (SELECT MAX(rowid) FROM positions GROUP BY mint)`;
}

function mapPosition(row: Row): DashboardPosition {
  return {
    id: Number(row['id']),
    mint: String(row['mint']),
    state: String(row['state']),
    entryTx: nullableString(row['entry_tx']),
    entryPrice: nullableNumber(row['entry_price']),
    exitPrice: nullableNumber(row['exit_price']),
    sizeSol: Number(row['size_sol']),
    exitReason: nullableString(row['exit_reason']),
    exitTx: nullableString(row['exit_tx']),
    pnlSol: nullableNumber(row['pnl_sol']),
    pnlPct: nullableNumber(row['pnl_pct']),
    openedAt: nullableString(row['opened_at']),
    closedAt: nullableString(row['closed_at']),
    createdAt: String(row['created_at']),
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
