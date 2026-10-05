import { timingSafeEqual } from 'node:crypto';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { serve } from '@hono/node-server';
import { serveStatic } from '@hono/node-server/serve-static';
import { Hono } from 'hono';
import type { Context, Next } from 'hono';
import type { Config } from '../config/schema.ts';
import { Repositories } from '../persistence/repositories.ts';
import { computeWalletReconciliation } from '../persistence/walletReconciliation.ts';
import { ConfigError, readSecret } from '../config/load.ts';
import { logger } from '../core/logger.ts';
import { routeTipLamports } from '../racer/costs.ts';
import type { TypedBus } from '../core/bus.ts';
import type { DB } from '../persistence/db.ts';
import { attachOperatorEventRecorder } from './events.ts';
import { getActiveRunSession } from '../core/session.ts';
import type { RiskManager, RiskSnapshot } from '../risk/manager.ts';
import { parseHeliusWebhook, type WebhookPriceIngest } from '../positions/webhookPricing.ts';
import {
  buildBreakersCsv,
  buildCandidatesCsv,
  buildEventsCsv,
  buildExecutionDragCsv,
  buildFunnelCsv,
  buildLatencyCsv,
  buildOpsReport,
  buildPnlCsv,
  buildPositionsCsv,
  buildRacerMigrationsCsv,
  buildRacerPaperCsv,
  buildRelaxedRiskCsv,
  buildSoakReport,
  buildTradeBlotterCsv,
  buildVetoDryRunCsv,
  buildVetoDryRunSummaryCsv,
  buildVetoReasonsCsv,
  getDashboardSummary,
  getExecutionDragComparison,
  getFunnelAnalytics,
  getVetoReasonBreakdown,
  getShadowVetoQuality,
  getShadowCoverage,
  getVetoDryRunComparison,
  getRelaxedRiskAnalytics,
  getPerformanceAnalytics,
  getPnlSeries,
  listBreakers,
  listCandidates,
  listEvents,
  listPositions,
  getRacerSummary,
  listCheckFailRates,
  listRacerMigrations,
  listRacerPaper,
  listShadowOutcomes,
  type DashboardEvent,
} from './queries.ts';
import { latencyPercentiles, rangeToModifier } from './analytics.ts';
import { buildEdgeCsv, getEdgeAnalytics } from './edge.ts';
import { buildRugForensics, renderRugForensicsMarkdown } from './rugForensics.ts';
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
  /**
   * Optional Helius webhook price ingest. When present (and webhooks are
   * enabled with a secret), POST /api/webhooks/helius accepts payloads.
   */
  priceIngest?: WebhookPriceIngest;
}

/** Webhook ingest route — exempt from dashboard basic auth (own secret). */
export const WEBHOOK_ROUTE = '/api/webhooks/helius';

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

  // Basic Auth gates the API only. The static app shell (index.html, assets)
  // stays public so a cold visit can load the login popup, which then sends
  // the Basic header on every API call itself.
  app.use('/api/*', auth);

  app.get('/api/health', (c) => c.json({ ok: true, mode: deps.config.mode, time: new Date().toISOString() }));
  app.post(WEBHOOK_ROUTE, async (c) => handleWebhookIngest(deps, c));
  // `track` selects the live wallet or the dry-run twin. Deliberately NOT
  // 'delta' here: these return wallet-shaped types, and a drag number rendered
  // in a wallet shape invites being read as a balance. Delta has its own route.
  app.get('/api/dashboard/summary', (c) => {
    const track = parseDataTrack(c.req.query('track'));
    return c.json(getDashboardSummary(deps.db, deps.config, track ? { track } : {}));
  });
  app.get('/api/positions', (c) => {
    const state = parseState(c.req.query('state'));
    const limit = parseLimit(c.req.query('limit'));
    const track = parseDataTrack(c.req.query('track'));
    const opts: { state?: 'open' | 'closed' | 'all'; limit?: number; track?: 'live' | 'dry' } = {};
    if (state) opts.state = state;
    if (limit !== undefined) opts.limit = limit;
    if (track) opts.track = track;
    return c.json(listPositions(deps.db, { ...opts, mode: deps.config.mode }));
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
  // Safe to accept 'delta' here: every track returns the same PnlPoint[] shape,
  // so a delta series cannot be mistaken for something else.
  app.get('/api/pnl', (c) => {
    const range = parseRange(c.req.query('range'));
    const track = parseTrack(c.req.query('track'));
    const opts: { range?: '24h' | '7d' | '30d'; track?: 'live' | 'dry' | 'delta'; mode: Config['mode'] } = { mode: deps.config.mode };
    if (range) opts.range = range;
    if (track) opts.track = track;
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
  // Wallet vs ledger: does booked PnL explain what the wallet actually did?
  app.get('/api/wallet/reconciliation', (c) => {
    const since = c.req.query('since');
    const sinceIso = since && !Number.isNaN(Date.parse(since)) ? new Date(since).toISOString() : null;
    return c.json(computeWalletReconciliation(new Repositories(deps.db), sinceIso));
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
  const racerSummary = () =>
    getRacerSummary(deps.db, {
      proposedSizeSol: deps.config.racer.paperSizeSol,
      ataRentSol: deps.config.racer.ataRentSol,
      exitLandMs: deps.config.racer.exitLandMs,
      modeledEntrySlot: deps.config.racer.realisticSlots[0] ?? 1,
      routeBuyTipSol: routeTipLamports(deps.config.heliusSender, 'buy') / 1e9,
      routeSellTipSol: routeTipLamports(deps.config.heliusSender, 'sell') / 1e9,
      sweepDelayMs: deps.config.wallet.sweepEmptyAtasMinutes * 60_000,
    });
  app.get('/api/racer/summary', (c) => c.json(racerSummary()));
  app.get('/api/racer/migrations', (c) => {
    const limit = parseLimit(c.req.query('limit'));
    return c.json(listRacerMigrations(deps.db, limit !== undefined ? { limit } : {}));
  });
  app.get('/api/racer/paper', (c) => {
    const limit = parseLimit(c.req.query('limit'));
    const cohort = c.req.query('cohort') === 'verified' ? 'verified' : undefined;
    return c.json(listRacerPaper(deps.db, { ...(limit !== undefined ? { limit } : {}), ...(cohort ? { cohort } : {}) }));
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
    const track = parseDataTrack(c.req.query('track'));
    return c.json(getPerformanceAnalytics(deps.db, { ...(range ? { range } : {}), ...(track ? { track } : {}), mode: deps.config.mode }));
  });
  // Live vs dry-run twin: total execution drag, plus the opportunity cost of
  // accepts the live leg never traded. Session-scoped by default so trades
  // predating the twin don't sit permanently in the live_only cohort.
  app.get('/api/analytics/execution-drag', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    const limit = parseLimit(c.req.query('limit'));
    const allSessions = c.req.query('allSessions') === '1';
    return c.json(
      getExecutionDragComparison(deps.db, {
        ...(range ? { range } : {}),
        ...(limit !== undefined ? { limit } : {}),
        allSessions,
        sessionId: getActiveRunSession()?.id ?? null,
      }),
    );
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
  app.get('/api/analytics/shadow-coverage', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    return c.json(getShadowCoverage(deps.db, range ? { range } : {}));
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
  // Rolling edge (work plan 2026-09-25 P4.3): ?track=live|dry&window=100
  app.get('/api/analytics/edge', (c) => {
    const track = parseDataTrack(c.req.query('track'));
    const rawWindow = parseLimit(c.req.query('window'));
    const window = rawWindow === undefined ? undefined : Math.min(1_000, Math.max(2, Math.floor(rawWindow)));
    return c.json({
      ...getEdgeAnalytics(deps.db, { ...(track ? { track } : {}), ...(window !== undefined ? { window } : {}), mode: deps.config.mode }),
      monitor: deps.getRiskSnapshot?.()?.edge ?? null,
    });
  });
  app.get('/api/analytics/relaxed-risk', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    return c.json(getRelaxedRiskAnalytics(deps.db, range ? { range } : {}));
  });
  app.get('/api/reports/trades.csv', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    const track = parseDataTrack(c.req.query('track'));
    const csv = buildTradeBlotterCsv(deps.db, { ...(range ? { range } : {}), ...(track ? { track } : {}), mode: deps.config.mode });
    return new Response(csv, {
      headers: {
        'content-type': 'text/csv; charset=utf-8',
        'content-disposition': `attachment; filename="trades-${track ?? 'live'}-${range ?? 'all'}.csv"`,
      },
    });
  });
  // Rug forensics (LIVE_PILOT_PLAN §4 S3): rug-vs-other feature quartiles.
  // ?track=dry|live&range=7d&rugPct=-80&format=md
  app.get('/api/reports/rug-forensics', (c) => {
    const range = parseAnalyticsRange(c.req.query('range')) ?? 'all';
    const track = c.req.query('track') === 'live' ? 'live' : 'dry';
    const rugRaw = Number(c.req.query('rugPct'));
    const report = buildRugForensics(deps.db, {
      track,
      range,
      ...(Number.isFinite(rugRaw) ? { rugPnlPct: rugRaw } : {}),
    });
    if (c.req.query('format') === 'md') {
      return new Response(renderRugForensicsMarkdown(report), {
        headers: { 'content-type': 'text/markdown; charset=utf-8' },
      });
    }
    return c.json(report);
  });
  app.get('/api/reports/execution-drag.csv', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    const allSessions = c.req.query('allSessions') === '1';
    const csv = buildExecutionDragCsv(deps.db, {
      ...(range ? { range } : {}),
      allSessions,
      sessionId: getActiveRunSession()?.id ?? null,
    });
    return new Response(csv, {
      headers: {
        'content-type': 'text/csv; charset=utf-8',
        'content-disposition': `attachment; filename="execution-drag-${range ?? '7d'}.csv"`,
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
  // Per-section exports: one download per dashboard panel, mirroring exactly
  // what the panel renders (same query, same filters).
  app.get('/api/reports/pnl.csv', (c) => {
    const range = parseRange(c.req.query('range')) ?? '7d';
    const track = parseTrack(c.req.query('track')) ?? 'live';
    const csv = buildPnlCsv(deps.db, { range, track, mode: deps.config.mode });
    return csvDownload(c, csv, `pnl-${track}-${range}.csv`);
  });
  app.get('/api/reports/positions.csv', (c) => {
    const state = parseState(c.req.query('state'));
    const track = parseDataTrack(c.req.query('track'));
    const limit = parseLimit(c.req.query('limit'));
    const csv = buildPositionsCsv(deps.db, {
      ...(state ? { state } : { state: 'all' as const }),
      ...(limit !== undefined ? { limit } : {}),
      ...(track ? { track } : {}),
      mode: deps.config.mode,
    });
    return csvDownload(c, csv, `positions-${track ?? 'live'}-${state ?? 'all'}.csv`);
  });
  app.get('/api/reports/candidates.csv', (c) => {
    const limit = parseLimit(c.req.query('limit'));
    return csvDownload(
      c,
      buildCandidatesCsv(deps.db, limit !== undefined ? { limit } : {}),
      'candidates.csv',
    );
  });
  app.get('/api/reports/events.csv', (c) => {
    const limit = parseLimit(c.req.query('limit'));
    const level = parseLevel(c.req.query('level'));
    const category = c.req.query('category');
    return csvDownload(
      c,
      buildEventsCsv(deps.db, {
        ...(limit !== undefined ? { limit } : {}),
        ...(level ? { level } : {}),
        ...(category ? { category } : {}),
      }),
      `events${category ? `-${category}` : ''}${level ? `-${level}` : ''}.csv`,
    );
  });
  app.get('/api/reports/breakers.csv', (c) => {
    const limit = parseLimit(c.req.query('limit'));
    return csvDownload(
      c,
      buildBreakersCsv(deps.db, limit !== undefined ? { limit } : {}),
      'breakers.csv',
    );
  });
  app.get('/api/reports/veto-reasons.csv', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    return csvDownload(
      c,
      buildVetoReasonsCsv(deps.db, range ? { range } : {}),
      `veto-reasons-${range ?? 'all'}.csv`,
    );
  });
  app.get('/api/reports/edge.csv', (c) => {
    const track = parseDataTrack(c.req.query('track'));
    const rawWindow = parseLimit(c.req.query('window'));
    const window = rawWindow === undefined ? undefined : Math.min(1_000, Math.max(2, Math.floor(rawWindow)));
    return csvDownload(
      c,
      buildEdgeCsv(deps.db, {
        ...(track ? { track } : {}),
        ...(window !== undefined ? { window } : {}),
        mode: deps.config.mode,
      }),
      `edge-${track ?? 'live'}-${window ?? 100}.csv`,
    );
  });
  app.get('/api/reports/relaxed-risk.csv', (c) => {
    const range = parseAnalyticsRange(c.req.query('range'));
    return csvDownload(
      c,
      buildRelaxedRiskCsv(deps.db, range ? { range } : {}),
      `relaxed-risk-${range ?? 'all'}.csv`,
    );
  });
  app.get('/api/reports/latency.csv', (c) => {
    const kind = c.req.query('kind');
    const range = parseAnalyticsRange(c.req.query('range'));
    const limit = parseLimit(c.req.query('limit'));
    return csvDownload(
      c,
      buildLatencyCsv(deps.db, {
        ...(kind ? { kind } : {}),
        ...(range ? { range } : {}),
        ...(limit !== undefined ? { limit } : {}),
      }),
      `latency${kind ? `-${kind}` : ''}-${range ?? 'all'}.csv`,
    );
  });
  app.get('/api/reports/racer-migrations.csv', (c) => {
    const limit = parseLimit(c.req.query('limit'));
    return csvDownload(
      c,
      buildRacerMigrationsCsv(deps.db, limit !== undefined ? { limit } : {}),
      'racer-migrations.csv',
    );
  });
  app.get('/api/reports/racer-paper.csv', (c) => {
    const limit = parseLimit(c.req.query('limit'));
    const cohort = c.req.query('cohort') === 'verified' ? 'verified' : undefined;
    return csvDownload(
      c,
      buildRacerPaperCsv(deps.db, { ...(limit !== undefined ? { limit } : {}), ...(cohort ? { cohort } : {}) }),
      'racer-paper.csv',
    );
  });
  app.get('/api/reports/racer-summary.json', (c) => c.json(racerSummary()));
  // Comprehensive bundle: every metric the dashboard renders, in one JSON
  // document for offline analysis (profitability, latency, detection, funnel,
  // edge, drag, racer). List endpoints carry row caps, noted in `caps`.
  app.get('/api/reports/full-bundle.json', (c) => {
    const range = parseAnalyticsRange(c.req.query('range')) ?? '7d';
    const track = parseDataTrack(c.req.query('track')) ?? 'live';
    const pnlRange = range === 'all' ? '30d' : range;
    const mod = rangeToModifier(range) ?? '-7 days';
    const latencyKinds = ['detection', 'exit_confirm', 'detection_slots', 'entry_land_slots', 'exit_land_slots'];
    const latency: Record<string, unknown> = {};
    for (const kind of latencyKinds) latency[kind] = latencyPercentiles(deps.db, kind, mod);
    return c.json({
      generatedAt: new Date().toISOString(),
      mode: deps.config.mode,
      range,
      track,
      caps: {
        positions: 200, candidates: 150, events: 300, breakers: 200,
        racerMigrations: 500, racerPaper: 500,
        note: 'Capped lists mirror the dashboard tables; use the per-section CSVs or trades blotter for wider pulls.',
      },
      summary: getDashboardSummary(deps.db, deps.config, { track }),
      performance: getPerformanceAnalytics(deps.db, { range, track, mode: deps.config.mode }),
      pnl: getPnlSeries(deps.db, { range: pnlRange, track, mode: deps.config.mode }),
      positions: listPositions(deps.db, { state: 'all', limit: 200, track, mode: deps.config.mode }),
      candidates: listCandidates(deps.db, { limit: 150 }),
      events: listEvents(deps.db, { limit: 300 }),
      breakers: listBreakers(deps.db, { limit: 200 }),
      risk: deps.getRiskSnapshot?.() ?? null,
      walletReconciliation: computeWalletReconciliation(new Repositories(deps.db), null),
      funnel: getFunnelAnalytics(deps.db, { range }),
      vetoBreakdown: getVetoReasonBreakdown(deps.db, { range }),
      checkFailRates: listCheckFailRates(deps.db, { range }),
      vetoDryRunComparison: getVetoDryRunComparison(deps.db, { range }),
      shadowOutcomes: listShadowOutcomes(deps.db, { range, limit: 500 }),
      relaxedRisk: getRelaxedRiskAnalytics(deps.db, { range }),
      edge: getEdgeAnalytics(deps.db, { track, window: 100, mode: deps.config.mode }),
      executionDrag: getExecutionDragComparison(deps.db, {
        range,
        sessionId: getActiveRunSession()?.id ?? null,
      }),
      latency,
      racer: {
        summary: racerSummary(),
        migrations: listRacerMigrations(deps.db, { limit: 500 }),
        paper: listRacerPaper(deps.db, { limit: 500 }),
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
        minSizeWalletPct: deps.config.entry.minSizeWalletPct,
        baseSizeWalletPct: deps.config.entry.baseSizeWalletPct,
        maxSizeWalletPct: deps.config.entry.maxSizeWalletPct,
        minAbsoluteSol: deps.config.entry.minAbsoluteSol,
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
  priceIngest?: WebhookPriceIngest;
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
    ...(deps.priceIngest ? { priceIngest: deps.priceIngest } : {}),
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
    // The Helius webhook route carries its own shared-secret auth (Helius
    // cannot do dashboard basic auth).
    if (c.req.path === WEBHOOK_ROUTE) return next();
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
  // Deliberately no `WWW-Authenticate: Basic` header: browsers answer it with
  // their native login dialog. The dashboard ships its own login popup and
  // sends the Basic header itself, so a 401 is JSON like every other API
  // error (`curl -u` keeps working unchanged).
  return c.json({ ok: false, error: 'unauthorized' }, 401);
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

/**
 * Helius webhook ingest for pool-reserve updates. Disabled (404) unless
 * webhooks are enabled AND a secret is configured AND an ingest is attached.
 * Auth is a shared secret via `x-webhook-secret` or Bearer token — never the
 * dashboard credentials. Malformed payloads are 400s, never crashes.
 */
async function handleWebhookIngest(deps: DashboardAppDeps, c: Context): Promise<Response> {
  const secret = readSecret(deps.config.webhooks.secretEnvVar);
  if (!deps.config.webhooks.enabled || !secret || !deps.priceIngest) {
    return c.json({ ok: false, error: 'webhook ingest disabled' }, 404);
  }
  const header = c.req.header('x-webhook-secret') ?? bearerToken(c.req.header('authorization'));
  if (!header || !constantEqual(header, secret)) {
    return c.json({ ok: false, error: 'unauthorized' }, 401);
  }
  let body: unknown;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ ok: false, error: 'invalid JSON' }, 400);
  }
  const ticks = deps.priceIngest.ingest(parseHeliusWebhook(body));
  return c.json({ ok: true, pools: ticks.length, tracked: deps.priceIngest.size });
}

function bearerToken(header: string | undefined): string | undefined {
  return header?.startsWith('Bearer ') ? header.slice('Bearer '.length) : undefined;
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

function csvDownload(c: Context, csv: string, filename: string): Response {
  return new Response(csv, {
    headers: {
      'content-type': 'text/csv; charset=utf-8',
      'content-disposition': `attachment; filename="${filename}"`,
    },
  });
}

function parseLimit(value: string | undefined): number | undefined {
  if (!value) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function parseState(value: string | undefined): 'open' | 'closed' | 'all' | undefined {
  return value === 'open' || value === 'closed' || value === 'all' ? value : undefined;
}

/** Full track, including 'delta'. Only routes returning PnlPoint[] accept it. */
function parseTrack(value: string | undefined): 'live' | 'dry' | 'delta' | undefined {
  return value === 'live' || value === 'dry' || value === 'delta' ? value : undefined;
}

/**
 * Track for routes returning wallet-shaped types. 'delta' is rejected (falls
 * back to live) so a drag number can never surface in a balance-shaped payload.
 */
function parseDataTrack(value: string | undefined): 'live' | 'dry' | undefined {
  return value === 'live' || value === 'dry' ? value : undefined;
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
