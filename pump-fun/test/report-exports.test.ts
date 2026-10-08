import { describe, expect, it } from 'vitest';
import { ConfigSchema, type Config } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { createDashboardApp } from '../src/dashboard/server.ts';

/** Per-section export routes + the comprehensive full-bundle report. */
describe('per-section report exports', () => {
  it('serves every panel CSV/JSON and the full bundle', async () => {
    const db = openDb({ path: ':memory:', memory: true });
    seed(db);
    const app = createDashboardApp({ config: makeConfig(), db });

    const csvHeaders: Array<[string, string]> = [
      ['/api/reports/pnl.csv?range=7d&track=live', 'time,mint,pnl_sol,cumulative_pnl_sol'],
      ['/api/reports/positions.csv?state=all&track=live', 'id,mint,state'],
      ['/api/reports/candidates.csv', 'id,mint,verdict'],
      ['/api/reports/events.csv?limit=10', 'id,category,level'],
      ['/api/reports/events.csv?category=notification', 'id,category,level'],
      ['/api/reports/breakers.csv', 'type,detail,tripped,at'],
      ['/api/reports/veto-reasons.csv?range=all', 'scope,reason,category,count,pct'],
      ['/api/reports/edge.csv?track=live&window=100', 'section,cohort,n,net_sol,mean_pct,win_rate_pct,mean_prob'],
      ['/api/reports/relaxed-risk.csv?range=all', 'section,kind,accepted'],
      ['/api/reports/latency.csv?range=all', 'id,kind,mint,feed_source,latency_ms,created_at'],
      ['/api/reports/racer-migrations.csv', 'mint,signature,caller'],
      ['/api/reports/racer-paper.csv', 'mint,pool_address,size_sol'],
    ];
    for (const [path, headerPrefix] of csvHeaders) {
      const res = await app.request(path);
      expect(res.status, path).toBe(200);
      expect(res.headers.get('content-type'), path).toContain('text/csv');
      expect(res.headers.get('content-disposition'), path).toContain('attachment');
      const text = await res.text();
      const header = text.split('\n').find((l) => !l.startsWith('#'))!;
      expect(header.startsWith(headerPrefix), `${path}: ${header}`).toBe(true);
    }

    // Spot-check that exports carry panel data, not just headers.
    expect(await (await app.request('/api/reports/pnl.csv?range=30d&track=live')).text()).toContain('mintA');
    expect(await (await app.request('/api/reports/positions.csv?state=closed')).text()).toContain('mintA');
    expect(await (await app.request('/api/reports/candidates.csv')).text()).toContain('mintC');
    expect(await (await app.request('/api/reports/events.csv?category=notification')).text()).toContain('hello');
    expect(await (await app.request('/api/reports/breakers.csv')).text()).toContain('DAILY_LOSS');
    expect(await (await app.request('/api/reports/veto-reasons.csv?range=all')).text()).toContain('H1');
    expect(await (await app.request('/api/reports/latency.csv?kind=detection&range=all')).text()).toContain('detection');

    const racer = (await (await app.request('/api/reports/racer-summary.json')).json()) as Record<string, unknown>;
    expect(racer).toMatchObject({});

    const bundle = (await (
      await app.request('/api/reports/full-bundle.json?range=7d&track=live')
    ).json()) as Record<string, unknown>;
    for (const key of [
      'generatedAt', 'mode', 'range', 'track', 'caps', 'summary', 'performance', 'pnl',
      'positions', 'candidates', 'events', 'breakers', 'risk', 'walletReconciliation',
      'funnel', 'vetoBreakdown', 'checkFailRates', 'vetoDryRunComparison',
      'shadowOutcomes', 'relaxedRisk', 'edge', 'executionDrag', 'latency', 'racer',
    ]) {
      expect(bundle, `bundle.${key}`).toHaveProperty(key);
    }
    expect(bundle['range']).toBe('7d');
    expect(bundle['track']).toBe('live');
    expect((bundle['racer'] as Record<string, unknown>)).toHaveProperty('summary');
    expect((bundle['latency'] as Record<string, unknown>)).toHaveProperty('detection');
    db.close();
  });
});

function seed(db: ReturnType<typeof openDb>): void {
  db.prepare(
    `INSERT INTO positions (mint, entry_price, size_sol, state, pnl_sol, net_pnl_sol, opened_at, closed_at)
     VALUES ('mintA', 1, 0.25, 'CLOSED', 0.05, 0.05, datetime('now'), datetime('now'))`,
  ).run();
  db.prepare(
    `INSERT INTO candidates (mint, verdict, soft_score, veto_reasons, high_volatility)
     VALUES ('mintC', 'veto', 20, '["H1"]', 1)`,
  ).run();
  db.prepare(
    `INSERT INTO operator_events (category, level, message, entity_mint)
     VALUES ('notification', 'info', 'hello', NULL)`,
  ).run();
  db.prepare(
    `INSERT INTO breaker_events (type, detail, tripped, at)
     VALUES ('DAILY_LOSS', 'test', 1, datetime('now'))`,
  ).run();
  db.prepare(
    `INSERT INTO latency_samples (kind, mint, latency_ms) VALUES ('detection', 'mintA', 40)`,
  ).run();
}

function makeConfig(): Config {
  return ConfigSchema.parse({
    dashboard: {
      enabled: true,
      host: '127.0.0.1',
      port: 8787,
      usernameEnvVar: 'DASHBOARD_TEST_USER',
      passwordEnvVar: 'DASHBOARD_TEST_PASS',
    },
  });
}
