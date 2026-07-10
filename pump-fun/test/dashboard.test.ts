import { afterEach, describe, expect, it } from 'vitest';
import { ConfigError } from '../src/config/load.ts';
import { ConfigSchema, type Config } from '../src/config/schema.ts';
import { TypedBus } from '../src/core/bus.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { attachOperatorEventRecorder } from '../src/dashboard/events.ts';
import { computePerformanceStats } from '../src/dashboard/analytics.ts';
import {
  buildSoakReport,
  buildTradeBlotterCsv,
  buildVetoDryRunCsv,
  buildVetoDryRunSummaryCsv,
  getDashboardSummary,
  getFunnelAnalytics,
  getVetoReasonBreakdown,
  getRelaxedRiskAnalytics,
  getPerformanceAnalytics,
  listEvents,
  listPositions,
} from '../src/dashboard/queries.ts';
import { assertDashboardExposureSafe, createDashboardApp } from '../src/dashboard/server.ts';

const AUTH_USER = 'DASHBOARD_TEST_USER';
const AUTH_PASS = 'DASHBOARD_TEST_PASS';

afterEach(() => {
  delete process.env[AUTH_USER];
  delete process.env[AUTH_PASS];
});

describe('dashboard queries', () => {
  it('summarizes latest open positions and realized closed PnL', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const config = makeConfig();
    db.prepare(
      `INSERT INTO positions (mint, entry_price, size_sol, state, opened_at)
       VALUES ('mintA', 1, 0.25, 'OPEN', '2026-01-01T00:00:00.000Z')`,
    ).run();
    db.prepare(
      `INSERT INTO positions (mint, entry_price, size_sol, state, pnl_sol, pnl_pct, net_pnl_sol, fees_sol, opened_at, closed_at)
       VALUES ('mintA', 1, 0.25, 'CLOSED', 0.05, 20, 0.05, 0.01, '2026-01-01T00:00:00.000Z', datetime('now'))`,
    ).run();
    db.prepare(
      `INSERT INTO positions (mint, entry_price, size_sol, state, opened_at)
       VALUES ('mintB', 2, 0.3, 'OPEN', datetime('now'))`,
    ).run();
    db.prepare(`INSERT INTO graduations (mint, slot, detected_at_ns, feed_source) VALUES ('mintA', 1, '1', 'pumpportal')`).run();
    db.prepare(`INSERT INTO candidates (mint, verdict, soft_score, veto_reasons, high_volatility) VALUES ('mintA', 'accept', 78, '[]', 0)`).run();
    db.prepare(`INSERT INTO candidates (mint, verdict, soft_score, veto_reasons, high_volatility) VALUES ('mintC', 'veto', 20, '["H1"]', 1)`).run();
    db.prepare(`INSERT INTO latency_samples (kind, mint, latency_ms) VALUES ('detection', 'mintA', 40)`).run();
    db.prepare(`INSERT INTO latency_samples (kind, mint, latency_ms) VALUES ('exit_confirm', 'mintA', 120)`).run();

    const summary = getDashboardSummary(db, config);
    expect(summary.pnl.realizedSol).toBeCloseTo(0.05);
    expect(summary.pnl.feesSol).toBeCloseTo(0.01);
    expect(summary.positions.openCount).toBe(1);
    expect(summary.positions.openExposureSol).toBeCloseTo(0.3);
    expect(summary.flow).toMatchObject({ graduations: 1, accepted: 1, vetoed: 1, highVolatility: 1 });
    expect(summary.latency.detection.count).toBe(1);
    expect(summary.latency.exitConfirm.p95).toBe(120);

    expect(listPositions(db, { state: 'open' }).map((p) => p.mint)).toEqual(['mintB']);
    expect(listPositions(db, { state: 'closed' }).map((p) => p.mint)).toEqual(['mintA']);
    db.close();
  });

  it('computes expectancy, profit factor, and drawdown', () => {
    const stats = computePerformanceStats([0.1, -0.05, 0.2, -0.1], [0.01, 0.01, 0.01, 0.01]);
    expect(stats.tradeCount).toBe(4);
    expect(stats.wins).toBe(2);
    expect(stats.losses).toBe(2);
    expect(stats.realizedPnlSol).toBeCloseTo(0.15);
    expect(stats.expectancySol).toBeCloseTo(0.0375);
    expect(stats.profitFactor).toBeCloseTo(2);
    expect(stats.maxDrawdownSol).toBeGreaterThan(0);
  });

  it('builds trade blotter CSV and soak JSON', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const config = makeConfig();
    db.prepare(
      `INSERT INTO positions (mint, entry_price, size_sol, state, pnl_sol, net_pnl_sol, fees_sol, gross_pnl_sol, exit_reason, opened_at, closed_at)
       VALUES ('mintA', 1, 0.2, 'CLOSED', 0.04, 0.04, 0.01, 0.05, 'TAKE_PROFIT_1', datetime('now'), datetime('now'))`,
    ).run();
    db.prepare(
      `INSERT INTO positions (mint, entry_price, size_sol, state, pnl_sol, net_pnl_sol, fees_sol, exit_reason, opened_at, closed_at)
       VALUES ('mintB', 1, 0.2, 'CLOSED', -0.02, -0.02, 0.01, 'STOP_LOSS', datetime('now'), datetime('now'))`,
    ).run();

    const csv = buildTradeBlotterCsv(db, { range: 'all' });
    expect(csv.split('\n')[0]).toContain('mint');
    expect(csv).toContain('mintA');
    expect(csv).toContain('TAKE_PROFIT_1');

    const soak = buildSoakReport(db, config, { range: 'all' });
    expect(soak.feeAware).toBe(true);
    expect(soak.sampleSize).toBe(2);
    const perf = getPerformanceAnalytics(db, { range: 'all' });
    expect(perf.exitReasons.length).toBeGreaterThanOrEqual(2);
    expect(getFunnelAnalytics(db, { range: 'all' }).closed).toBe(2);
    db.close();
  });

  it('dedupes funnel counts by mint across append-only position rows', () => {
    const db = openDb({ path: ':memory:', memory: true });
    // One accepted candidate that produced one real position...
    db.prepare(`INSERT INTO candidates (mint, verdict, soft_score, veto_reasons) VALUES ('mintA', 'accept', 80, '[]')`).run();
    // ...but the append-only positions table has a row per FSM transition.
    for (const state of ['PENDING_ENTRY', 'OPEN', 'EXITING', 'CLOSED']) {
      db.prepare(
        `INSERT INTO positions (mint, entry_price, size_sol, state, opened_at, closed_at)
         VALUES ('mintA', 1, 0.03, ?, datetime('now'), ${state === 'CLOSED' ? "datetime('now')" : 'NULL'})`,
      ).run(state);
    }

    const funnel = getFunnelAnalytics(db, { range: 'all' });
    expect(funnel.accepted).toBe(1);
    expect(funnel.entered).toBe(1); // not 4 — deduped by mint
    expect(funnel.closed).toBe(1);
    expect(funnel.entryRatePct).toBeLessThanOrEqual(100);
    db.close();
  });

  it('breaks down veto reasons by primary cause and category', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const ins = (mint: string, verdict: string, reasons: string[]) =>
      db
        .prepare(
          `INSERT INTO candidates (mint, verdict, soft_score, veto_reasons, primary_veto_code)
           VALUES (?, ?, 30, ?, ?)`,
        )
        .run(mint, verdict, JSON.stringify(reasons), reasons[0] ?? null);
    ins('m1', 'veto', ['UNKNOWN:H4']);
    ins('m2', 'veto', ['UNKNOWN:H5', 'H7']); // multi-reason
    ins('m3', 'veto', ['LOW_SCORE']);
    ins('m4', 'veto', ['H7']);
    ins('m5', 'accept', []); // not counted

    const b = getVetoReasonBreakdown(db, { range: 'all' });
    expect(b.totalVetoed).toBe(4);

    const primary = Object.fromEntries(b.primary.map((r) => [r.reason, r]));
    expect(primary['UNKNOWN:H4']?.count).toBe(1);
    expect(primary['UNKNOWN:H4']?.category).toBe('unknown');
    expect(primary['LOW_SCORE']?.category).toBe('low_score');
    expect(primary['H7']?.category).toBe('hard_fail'); // m4's primary

    // Exploded: H7 appears in m2 (secondary) and m4 (primary) => 2.
    const all = Object.fromEntries(b.allReasons.map((r) => [r.reason, r.count]));
    expect(all['H7']).toBe(2);
    expect(all['UNKNOWN:H5']).toBe(1);
    db.close();
  });

  it('reports clean vs relaxed performance and H4 reason counts', () => {
    const db = openDb({ path: ':memory:', memory: true });
    db.prepare(
      `INSERT INTO candidates
         (mint, verdict, soft_score, veto_reasons, relaxed_risk, relaxed_reasons_json, hard_check_results)
       VALUES
         ('cleanA', 'accept', 80, '[]', 0, NULL, '[{"id":"H4","status":"pass"}]'),
         ('relaxedA', 'accept', 80, '[]', 1, '["relaxed_h7_pool_sol"]', '[{"id":"H4","status":"unknown","reason":"tx_too_large"}]')`,
    ).run();
    db.prepare(
      `INSERT INTO positions
         (mint, entry_price, size_sol, state, pnl_sol, net_pnl_sol, exit_reason, relaxed_risk, relaxed_reasons_json, mfe_pct, mae_pct, opened_at, closed_at)
       VALUES
         ('cleanA', 1, 0.03, 'CLOSED', 0.01, 0.01, 'TAKE_PROFIT_0', 0, NULL, 30, -5, datetime('now'), datetime('now')),
         ('relaxedA', 1, 0.02, 'CLOSED', -0.004, -0.004, 'STOP_LOSS', 1, '["relaxed_h7_pool_sol"]', 10, -20, datetime('now'), datetime('now')),
         ('relaxedB', 1, 0.02, 'FAILED', NULL, NULL, NULL, 1, '["relaxed_h5_top10"]', NULL, NULL, datetime('now'), NULL)`,
    ).run();

    const a = getRelaxedRiskAnalytics(db, { range: 'all' });
    expect(a.groups.find((g) => g.kind === 'clean')?.accepted).toBe(1);
    expect(a.groups.find((g) => g.kind === 'relaxed')?.entered).toBe(2);
    expect(a.groups.find((g) => g.kind === 'relaxed')?.netPnlSol).toBeCloseTo(-0.004);
    expect(a.rollback.relaxedStopLosses).toBe(1);
    expect(a.rollback.relaxedFailedEntries).toBe(1);
    expect(Object.fromEntries(a.h4Reasons.map((r) => [r.reason, r.count]))['tx_too_large']).toBe(1);
    db.close();
  });
});

describe('operator event recorder', () => {
  it('persists UI-friendly bus events and serializes bigint payloads', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const bus = new TypedBus();
    const detach = attachOperatorEventRecorder(bus, repos);

    bus.emit('graduation', {
      mint: 'mintA',
      venue: 'pumpswap',
      poolAddress: 'poolA',
      slot: 42,
      feedSource: 'pumpportal',
      receivedAtNs: 123n,
    });
    bus.emit('alert', { level: 'warn', message: 'watch this' });

    const events = listEvents(db, { limit: 10 });
    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({ category: 'notification', level: 'warn', message: 'watch this' });
    expect(events[0]?.payload).toEqual({ level: 'warn', message: 'watch this' });
    expect(events[1]?.payload).toMatchObject({ receivedAtNs: '123' });

    detach();
    db.close();
  });
});

describe('dashboard auth', () => {
  it('allows localhost dashboard requests without credentials', async () => {
    const db = openDb({ path: ':memory:', memory: true });
    const app = createDashboardApp({ config: makeConfig(), db });
    const res = await app.request('/api/health');
    expect(res.status).toBe(200);
    db.close();
  });

  it('serves analytics and report endpoints', async () => {
    const db = openDb({ path: ':memory:', memory: true });
    db.prepare(
      `INSERT INTO positions (mint, entry_price, size_sol, state, pnl_sol, net_pnl_sol, opened_at, closed_at)
       VALUES ('mintA', 1, 0.2, 'CLOSED', 0.03, 0.03, datetime('now'), datetime('now'))`,
    ).run();
    const app = createDashboardApp({
      config: makeConfig(),
      db,
      getRiskSnapshot: () => ({
        mode: 'paper',
        killed: false,
        streamDown: false,
        dayUtc: '2026-01-01',
        dailyRealizedPnlSol: 0.03,
        dailyLossLimitSol: 1,
        dailyLossUsedPct: 0,
        consecutiveLosses: 0,
        consecutiveLossHalt: 3,
        consecutiveHaltUntilMs: null,
        emergencies24h: 0,
        emergencyExitCount24hLimit: 5,
        walletBalanceSol: null,
        walletFloorSol: 0.1,
        tripped: [],
        canEnter: { ok: true },
      }),
    });

    expect((await app.request('/api/risk/status')).status).toBe(200);
    expect((await app.request('/api/analytics/performance?range=all')).status).toBe(200);
    expect((await app.request('/api/analytics/funnel')).status).toBe(200);
    expect((await app.request('/api/analytics/veto-reasons')).status).toBe(200);
    expect((await app.request('/api/analytics/shadow-veto-quality')).status).toBe(200);
    expect((await app.request('/api/analytics/veto-dry-run-comparison')).status).toBe(200);
    expect((await app.request('/api/analytics/relaxed-risk')).status).toBe(200);
    const csv = await app.request('/api/reports/trades.csv?range=all');
    expect(csv.status).toBe(200);
    expect(csv.headers.get('content-type')).toContain('text/csv');
    const vetoDetail = await app.request('/api/reports/veto-dry-run.csv?range=all');
    expect(vetoDetail.status).toBe(200);
    expect(vetoDetail.headers.get('content-type')).toContain('text/csv');
    const vetoSummary = await app.request('/api/reports/veto-dry-run-summary.csv?range=all');
    expect(vetoSummary.status).toBe(200);
    expect(vetoSummary.headers.get('content-type')).toContain('text/csv');
    const soak = await app.request('/api/reports/soak.json?range=all');
    expect(soak.status).toBe(200);
    const body = (await soak.json()) as { sampleSize: number };
    expect(body.sampleSize).toBe(1);
    db.close();
  });

  it('builds veto dry-run detail and summary CSV from shadow_outcomes', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    db.prepare(
      `INSERT INTO positions (mint, entry_price, size_sol, state, pnl_sol, net_pnl_sol, fees_sol,
        mode, opened_at, closed_at)
       VALUES ('liveX', 1, 0.03, 'CLOSED', 0.01, 0.01, 0.001, 'live', datetime('now'), datetime('now'))`,
    ).run();
    repos.recordShadowOutcome({
      mint: 'vetoH7',
      verdict: 'veto',
      primaryVetoCode: 'H7',
      baselinePrice: 1,
      peakPrice: 1.6,
      troughPrice: 0.9,
      peakMfePct: 60,
      maxMaePct: -10,
      hit25: true,
      hit50: true,
      samples: 5,
      trackedMs: 60_000,
      netPnlSol: 0.02,
      grossPnlSol: 0.025,
      feesSol: 0.005,
      exitReason: 'TAKE_PROFIT_1',
      holdMs: 50_000,
      sizeSol: 0.03,
    });

    const detail = buildVetoDryRunCsv(db, { range: 'all' });
    expect(detail).toContain('primary_veto_code');
    expect(detail).toContain('vetoH7');
    expect(detail).toContain('H7');
    expect(detail).not.toContain('liveX'); // live positions never in detail blotter

    const summary = buildVetoDryRunSummaryCsv(db, { range: 'all', mode: 'live' });
    expect(summary).toContain('live_accepted');
    expect(summary).toContain('primary_veto_code');
    expect(summary).toContain('H7');
    expect(summary).toMatch(/n=1/);
    db.close();
  });

  it('requires Basic Auth when credentials are configured', async () => {
    process.env[AUTH_USER] = 'operator';
    process.env[AUTH_PASS] = 'secret';
    const db = openDb({ path: ':memory:', memory: true });
    const app = createDashboardApp({ config: makeConfig(), db });

    expect((await app.request('/api/health')).status).toBe(401);
    const ok = await app.request('/api/health', {
      headers: { authorization: `Basic ${Buffer.from('operator:secret').toString('base64')}` },
    });
    expect(ok.status).toBe(200);
    db.close();
  });

  it('refuses non-localhost exposure without credentials', () => {
    expect(() => assertDashboardExposureSafe(makeConfig({ host: '0.0.0.0' }))).toThrow(ConfigError);
    process.env[AUTH_USER] = 'operator';
    process.env[AUTH_PASS] = 'secret';
    expect(() => assertDashboardExposureSafe(makeConfig({ host: '0.0.0.0' }))).not.toThrow();
  });
});

function makeConfig(dashboard: Partial<Config['dashboard']> = {}): Config {
  return ConfigSchema.parse({
    dashboard: {
      enabled: true,
      host: '127.0.0.1',
      port: 8787,
      usernameEnvVar: AUTH_USER,
      passwordEnvVar: AUTH_PASS,
      ...dashboard,
    },
  });
}
