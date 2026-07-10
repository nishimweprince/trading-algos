import { timingSafeEqual } from 'node:crypto';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { serve } from '@hono/node-server';
import { serveStatic } from '@hono/node-server/serve-static';
import { Hono } from 'hono';
import type { Context, Next } from 'hono';
import type { Config } from '../config/schema.ts';
import { ConfigError, readSecret } from '../config/load.ts';
import { logger } from '../core/logger.ts';
import type { TypedBus } from '../core/bus.ts';
import type { DB } from '../persistence/db.ts';
import type { Repositories } from '../persistence/repositories.ts';
import { attachOperatorEventRecorder } from './events.ts';
import type { RiskManager, RiskSnapshot } from '../risk/manager.ts';
import {
  buildFunnelCsv,
  buildOpsReport,
  buildSoakReport,
  buildTradeBlotterCsv,
  buildVetoDryRunCsv,
  buildVetoDryRunSummaryCsv,
  getDashboardSummary,
  getFunnelAnalytics,
  getVetoReasonBreakdown,
  getShadowVetoQuality,
  getVetoDryRunComparison,
  getRelaxedRiskAnalytics,
  getPerformanceAnalytics,
  getPnlSeries,
  listBreakers,
  listCandidates,
  listEvents,
  listPositions,
  listShadowOutcomes,
  type DashboardEvent,
} from './queries.ts';
import { latencyPercentiles, rangeToModifier } from './analytics.ts';
import {
  buildStrategyWeekReport,
  listStrategyTrades,
  renderStrategyWeekMarkdown,
  strategyFillsCsv,
  strategyTradesCsv,
} from './strategyReport.ts';

export interface DashboardRuntime {
  stop(): Promise<void>;
}

export interface DashboardAppDeps {
  config: Config;
  db: DB;
  hub?: DashboardEventHub;
  /** Optional live risk snapshot provider (null when standalone). */
  getRiskSnapshot?: () => RiskSnapshot | null;
}

export class DashboardEventHub {
  private readonly clients = new Set<(event: DashboardEvent) => void>();

  subscribe(send: (event: DashboardEvent) => void): () => void {
    this.clients.add(send);
    return () => this.clients.delete(send);
  }

  publish(event: DashboardEvent): void {
    for (const send of this.clients) send(event);
  }
}

export function createDashboardApp(deps: DashboardAppDeps): Hono {
  const app = new Hono();
  const auth = buildAuthMiddleware(deps.config);
  const hub = deps.hub ?? new DashboardEventHub();

  app.use('*', auth);

  app.get('/api/health', (c) => c.json({ ok: true, mode: deps.config.mode, time: new Date().toISOString() }));
  app.get('/api/dashboard/summary', (c) => c.json(getDashboardSummary(deps.db, deps.config)));
  app.get('/api/positions', (c) => {
    const state = parseState(c.req.query('state'));
    const limit = parseLimit(c.req.query('limit'));
    const opts: { state?: 'open' | 'closed' | 'all'; limit?: number } = {};
    if (state) opts.state = state;
    if (limit !== undefined) opts.limit = limit;
    return c.json(listPositions(deps.db, opts));
  });
  app.get('/api/shadow-outcomes', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    const limit = parseLimit(c.req.query('limit'));
    return c.json(
      listShadowOutcomes(deps.db, {
        ...(range ? { range } : {}),
        ...(limit !== undefined ? { limit } : {}),
      }),
    );
  });
  app.get('/api/pnl', (c) => {
    const range = parseRange(c.req.query('range'));
    const opts: { range?: '24h' | '7d' | '30d' } = {};
    if (range) opts.range = range;
    return c.json(getPnlSeries(deps.db, opts));
  });
  app.get('/api/candidates', (c) => {
    const limit = parseLimit(c.req.query('limit'));
    const opts: { limit?: number } = {};
    if (limit !== undefined) opts.limit = limit;
    return c.json(listCandidates(deps.db, opts));
  });
  app.get('/api/events', (c) => {
    const limit = parseLimit(c.req.query('limit'));
    const level = parseLevel(c.req.query('level'));
    const category = c.req.query('category');
    const opts: { limit?: number; level?: 'info' | 'warn' | 'error'; category?: string } = {};
    if (limit !== undefined) opts.limit = limit;
    if (level) opts.level = level;
    if (category) opts.category = category;
    return c.json(listEvents(deps.db, opts));
  });
  app.get('/api/risk/status', (c) => {
    const snap = deps.getRiskSnapshot?.() ?? null;
    return c.json(snap ?? { available: false, mode: deps.config.mode });
  });
  app.get('/api/breakers', (c) => {
    const limit = parseLimit(c.req.query('limit'));
    return c.json(listBreakers(deps.db, limit !== undefined ? { limit } : {}));
  });
  app.get('/api/latency', (c) => {
    const kind = c.req.query('kind') ?? 'detection';
    const range = parseAnalyticsRange(c.req.query('range')) ?? '24h';
    const mod = rangeToModifier(range) ?? '-1 day';
    return c.json({ kind, range, ...latencyPercentiles(deps.db, kind, mod) });
  });
  app.get('/api/analytics/ops', (c) => {
    const summary = getDashboardSummary(deps.db, deps.config);
    return c.json({
      generatedAt: new Date().toISOString(),
      mode: deps.config.mode,
      risk: deps.getRiskSnapshot?.() ?? null,
      summary,
      breakers: listBreakers(deps.db, { limit: 10 }),
    });
  });
  app.get('/api/analytics/performance', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    return c.json(getPerformanceAnalytics(deps.db, range ? { range } : {}));
  });
  app.get('/api/analytics/funnel', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    return c.json(getFunnelAnalytics(deps.db, range ? { range } : {}));
  });
  app.get('/api/analytics/veto-reasons', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    return c.json(getVetoReasonBreakdown(deps.db, range ? { range } : {}));
  });
  app.get('/api/analytics/shadow-veto-quality', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    return c.json(getShadowVetoQuality(deps.db, range ? { range } : {}));
  });
  app.get('/api/analytics/veto-dry-run-comparison', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    const mode = c.req.query('mode') ?? undefined;
    return c.json(
      getVetoDryRunComparison(deps.db, {
        ...(range ? { range } : {}),
        ...(mode ? { mode } : {}),
      }),
    );
  });
  app.get('/api/analytics/relaxed-risk', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    return c.json(getRelaxedRiskAnalytics(deps.db, range ? { range } : {}));
  });
  app.get('/api/reports/trades.csv', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    const csv = buildTradeBlotterCsv(deps.db, range ? { range } : {});
    return new Response(csv, {
      headers: {
        'content-type': 'text/csv; charset=utf-8',
        'content-disposition': `attachment; filename="trades-${range ?? 'all'}.csv"`,
      },
    });
  });
  app.get('/api/reports/ops.json', (c) =>
    c.json(buildOpsReport(deps.db, deps.config, deps.getRiskSnapshot?.() ?? null)),
  );
  app.get('/api/reports/soak.json', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    return c.json(buildSoakReport(deps.db, deps.config, range ? { range } : { range: '7d' }));
  });
  app.get('/api/reports/funnel.csv', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    const csv = buildFunnelCsv(deps.db, range ? { range } : {});
    return new Response(csv, {
      headers: {
        'content-type': 'text/csv; charset=utf-8',
        'content-disposition': `attachment; filename="funnel-checks-${range ?? 'all'}.csv"`,
      },
    });
  });
  app.get('/api/reports/veto-dry-run.csv', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    const csv = buildVetoDryRunCsv(deps.db, range ? { range } : {});
    return new Response(csv, {
      headers: {
        'content-type': 'text/csv; charset=utf-8',
        'content-disposition': `attachment; filename="veto-dry-run-${range ?? 'all'}.csv"`,
      },
    });
  });
  app.get('/api/reports/veto-dry-run-summary.csv', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    const mode = c.req.query('mode') ?? undefined;
    const csv = buildVetoDryRunSummaryCsv(deps.db, {
      ...(range ? { range } : {}),
      ...(mode ? { mode } : {}),
    });
    return new Response(csv, {
      headers: {
        'content-type': 'text/csv; charset=utf-8',
        'content-disposition': `attachment; filename="veto-dry-run-summary-${range ?? 'all'}.csv"`,
      },
    });
  });
  app.get('/api/reports/strategy-week.json', (c) => {
    const range = parseAnalyticsRange(c.req.query('range')) ?? '7d';
    const allowMixed = c.req.query('allowMixed') === '1';
    return c.json(
      buildStrategyWeekReport(deps.db, deps.config, {
        range,
        mode: allowMixed ? null : deps.config.mode,
        allowMixedModes: allowMixed,
      }),
    );
  });
  app.get('/api/reports/strategy-week.md', (c) => {
    const range = parseAnalyticsRange(c.req.query('range')) ?? '7d';
    const allowMixed = c.req.query('allowMixed') === '1';
    const report = buildStrategyWeekReport(deps.db, deps.config, {
      range,
      mode: allowMixed ? null : deps.config.mode,
      allowMixedModes: allowMixed,
    });
    return new Response(renderStrategyWeekMarkdown(report), {
      headers: {
        'content-type': 'text/markdown; charset=utf-8',
        'content-disposition': `attachment; filename="strategy-week-${range}.md"`,
      },
    });
  });
  app.get('/api/reports/strategy-trades.csv', (c) => {
    const range = parseAnalyticsRange(c.req.query('range')) ?? '7d';
    const allowMixed = c.req.query('allowMixed') === '1';
    const trades = listStrategyTrades(deps.db, range, allowMixed ? null : deps.config.mode);
    return new Response(strategyTradesCsv(trades), {
      headers: {
        'content-type': 'text/csv; charset=utf-8',
        'content-disposition': `attachment; filename="strategy-trades-${range}.csv"`,
      },
    });
  });
  app.get('/api/reports/strategy-fills.csv', (c) => {
    const range = parseAnalyticsRange(c.req.query('range')) ?? '7d';
    return new Response(strategyFillsCsv(deps.db, range), {
      headers: {
        'content-type': 'text/csv; charset=utf-8',
        'content-disposition': `attachment; filename="strategy-fills-${range}.csv"`,
      },
    });
  });
  app.get('/api/config-public', (c) =>
    c.json({
      mode: deps.config.mode,
      dashboard: {
        host: deps.config.dashboard.host,
        port: deps.config.dashboard.port,
        authRequired: hasCredentials(deps.config),
      },
      entry: {
        baseSizeSol: deps.config.entry.baseSizeSol,
        maxSizeSol: deps.config.entry.maxSizeSol,
        minEntryScore: deps.config.entry.minEntryScore,
      },
      risk: {
        maxConcurrentPositions: deps.config.risk.maxConcurrentPositions,
        dailyLossLimitSol: deps.config.risk.dailyLossLimitSol,
        consecutiveLossHalt: deps.config.risk.consecutiveLossHalt,
        emergencyExitCount24h: deps.config.risk.emergencyExitCount24h,
      },
    }),
  );
  app.get('/api/stream', (c) => streamEvents(c, hub));

  const dist = resolve('web/dashboard/dist');
  if (existsSync(dist)) {
    app.use('/assets/*', serveStatic({ root: dist }));
    app.get('/favicon.svg', serveStatic({ path: resolve(dist, 'favicon.svg') }));
    app.get('*', serveStatic({ path: resolve(dist, 'index.html') }));
  } else {
    app.get('/', (c) => c.text('Dashboard assets are not built. Run npm run dashboard:build.', 503));
  }

  return app;
}

export function startDashboardServer(deps: {
  config: Config;
  db: DB;
  bus: TypedBus;
  repos: Repositories;
  risk?: RiskManager;
}): DashboardRuntime | null {
  if (!deps.config.dashboard.enabled) return null;

  assertDashboardExposureSafe(deps.config);
  const hub = new DashboardEventHub();
  const unsubscribe = attachOperatorEventRecorder(deps.bus, deps.repos, hub);
  const app = createDashboardApp({
    config: deps.config,
    db: deps.db,
    hub,
    getRiskSnapshot: deps.risk ? () => deps.risk!.getSnapshot() : () => null,
  });
  const log = logger.child({ mod: 'dashboard' });
  const server = serve({
    fetch: app.fetch,
    hostname: deps.config.dashboard.host,
    port: deps.config.dashboard.port,
  });
  log.info('dashboard listening', {
    host: deps.config.dashboard.host,
    port: deps.config.dashboard.port,
    auth: hasCredentials(deps.config),
  });

  return {
    stop: () =>
      new Promise<void>((resolveStop) => {
        unsubscribe();
        server.close(() => resolveStop());
      }),
  };
}

function streamEvents(c: Context, hub: DashboardEventHub): Response {
  const encoder = new TextEncoder();
  let unsubscribe: (() => void) | null = null;
  let heartbeat: NodeJS.Timeout | null = null;

  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(`event: ready\ndata: ${JSON.stringify({ ok: true })}\n\n`));
      unsubscribe = hub.subscribe((event) => {
        try {
          controller.enqueue(encoder.encode(toSse('operator_event', event)));
        } catch {
          unsubscribe?.();
        }
      });
      heartbeat = setInterval(() => {
        try {
          controller.enqueue(encoder.encode(`: heartbeat ${Date.now()}\n\n`));
        } catch {
          unsubscribe?.();
        }
      }, 15_000);
    },
    cancel() {
      unsubscribe?.();
      if (heartbeat) clearInterval(heartbeat);
    },
  });

  return new Response(body, {
    headers: {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    },
  });
}

function toSse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function buildAuthMiddleware(config: Config): (c: Context, next: Next) => Promise<Response | void> {
  const username = readSecret(config.dashboard.usernameEnvVar);
  const password = readSecret(config.dashboard.passwordEnvVar);
  const eitherSet = Boolean(username || password);
  if (eitherSet && (!username || !password)) {
    throw new ConfigError('dashboard auth requires both username and password env vars when either is set');
  }
  if (!username || !password) return async (_c, next) => next();

  return async (c, next) => {
    const header = c.req.header('authorization');
    if (!header?.startsWith('Basic ')) return unauthorized(c);
    const decoded = decodeBasicAuth(header.slice('Basic '.length));
    if (!decoded || !constantEqual(decoded.username, username) || !constantEqual(decoded.password, password)) {
      return unauthorized(c);
    }
    await next();
  };
}

function unauthorized(c: Context): Response {
  return c.text('Unauthorized', 401, { 'www-authenticate': 'Basic realm="pump-fun dashboard"' });
}

function decodeBasicAuth(encoded: string): { username: string; password: string } | null {
  try {
    const raw = Buffer.from(encoded, 'base64').toString('utf8');
    const idx = raw.indexOf(':');
    if (idx < 0) return null;
    return { username: raw.slice(0, idx), password: raw.slice(idx + 1) };
  } catch {
    return null;
  }
}

function constantEqual(a: string, b: string): boolean {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}

export function assertDashboardExposureSafe(config: Config): void {
  if (isLocalHost(config.dashboard.host)) return;
  if (!hasCredentials(config)) {
    throw new ConfigError('dashboard must use Basic Auth before binding to a non-localhost host');
  }
}

function hasCredentials(config: Config): boolean {
  return Boolean(readSecret(config.dashboard.usernameEnvVar) && readSecret(config.dashboard.passwordEnvVar));
}

function isLocalHost(host: string): boolean {
  return host === '127.0.0.1' || host === 'localhost' || host === '::1';
}

function parseLimit(value: string | undefined): number | undefined {
  if (!value) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function parseState(value: string | undefined): 'open' | 'closed' | 'all' | undefined {
  return value === 'open' || value === 'closed' || value === 'all' ? value : undefined;
}

function parseRange(value: string | undefined): '24h' | '7d' | '30d' | undefined {
  return value === '24h' || value === '7d' || value === '30d' ? value : undefined;
}

function parseAnalyticsRange(value: string | undefined): '24h' | '7d' | '30d' | 'all' | undefined {
  return value === '24h' || value === '7d' || value === '30d' || value === 'all' ? value : undefined;
}

function parseLevel(value: string | undefined): 'info' | 'warn' | 'error' | undefined {
  return value === 'info' || value === 'warn' || value === 'error' ? value : undefined;
}
