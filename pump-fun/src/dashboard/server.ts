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
import {
  getDashboardSummary,
  getPnlSeries,
  listCandidates,
  listEvents,
  listPositions,
  type DashboardEvent,
} from './queries.ts';

export interface DashboardRuntime {
  stop(): Promise<void>;
}

export interface DashboardAppDeps {
  config: Config;
  db: DB;
  hub?: DashboardEventHub;
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
}): DashboardRuntime | null {
  if (!deps.config.dashboard.enabled) return null;

  assertDashboardExposureSafe(deps.config);
  const hub = new DashboardEventHub();
  const unsubscribe = attachOperatorEventRecorder(deps.bus, deps.repos, hub);
  const app = createDashboardApp({ config: deps.config, db: deps.db, hub });
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

function parseLevel(value: string | undefined): 'info' | 'warn' | 'error' | undefined {
  return value === 'info' || value === 'warn' || value === 'error' ? value : undefined;
}
