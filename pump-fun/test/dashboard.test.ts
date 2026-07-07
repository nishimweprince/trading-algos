import { afterEach, describe, expect, it } from 'vitest';
import { ConfigError } from '../src/config/load.ts';
import { ConfigSchema, type Config } from '../src/config/schema.ts';
import { TypedBus } from '../src/core/bus.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { attachOperatorEventRecorder } from '../src/dashboard/events.ts';
import {
  getDashboardSummary,
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
      `INSERT INTO positions (mint, entry_price, size_sol, state, pnl_sol, pnl_pct, opened_at, closed_at)
       VALUES ('mintA', 1, 0.25, 'CLOSED', 0.05, 20, '2026-01-01T00:00:00.000Z', datetime('now'))`,
    ).run();
    db.prepare(
      `INSERT INTO positions (mint, entry_price, size_sol, state, opened_at)
       VALUES ('mintB', 2, 0.3, 'OPEN', datetime('now'))`,
    ).run();
    db.prepare(`INSERT INTO graduations (mint, slot, detected_at_ns, feed_source) VALUES ('mintA', 1, '1', 'pumpportal')`).run();
    db.prepare(`INSERT INTO candidates (mint, verdict, soft_score, veto_reasons, high_volatility) VALUES ('mintA', 'accept', 78, '[]', 0)`).run();
    db.prepare(`INSERT INTO candidates (mint, verdict, soft_score, veto_reasons, high_volatility) VALUES ('mintC', 'veto', 20, '["H1"]', 1)`).run();

    const summary = getDashboardSummary(db, config);
    expect(summary.pnl.realizedSol).toBeCloseTo(0.05);
    expect(summary.positions.openCount).toBe(1);
    expect(summary.positions.openExposureSol).toBeCloseTo(0.3);
    expect(summary.flow).toMatchObject({ graduations: 1, accepted: 1, vetoed: 1, highVolatility: 1 });

    expect(listPositions(db, { state: 'open' }).map((p) => p.mint)).toEqual(['mintB']);
    expect(listPositions(db, { state: 'closed' }).map((p) => p.mint)).toEqual(['mintA']);
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
