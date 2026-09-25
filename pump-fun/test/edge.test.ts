import { describe, expect, it } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { getEdgeAnalytics } from '../src/dashboard/edge.ts';
import { createDashboardApp } from '../src/dashboard/server.ts';

function seed(db: ReturnType<typeof openDb>, table: 'positions' | 'dry_run_positions') {
  const extraCol = table === 'dry_run_positions' ? ', live_status' : '';
  const extraVal = table === 'dry_run_positions' ? ", 'opened'" : '';
  const ins = db.prepare(
    `INSERT INTO ${table} (mint, size_sol, state, exit_reason, pnl_sol, net_pnl_sol, fees_sol, relaxed_risk, closed_at${extraCol})
     VALUES (?, 0.04, 'CLOSED', ?, ?, ?, 0.001, ?, datetime('now', ?)${extraVal})`,
  );
  // 4 TP winners (+0.008), 4 stop losers (-0.006), 2 emergency losers (-0.02)
  let i = 0;
  const add = (reason: string, pnl: number, relaxed: number, mint: string) => {
    ins.run(mint, reason, pnl, pnl, relaxed, `-${100 - i} minutes`);
    i++;
  };
  for (let k = 0; k < 4; k++) add('TAKE_PROFIT_1', 0.008, 0, `w${k}pump`);
  for (let k = 0; k < 4; k++) add('STOP_LOSS', -0.006, 0, `l${k}pump`);
  for (let k = 0; k < 2; k++) add('EMERGENCY_EXIT', -0.02, 1, `e${k}xyz`);
}

describe('edge analytics (P4.3)', () => {
  it('computes rolling expectancy, cohorts, fee share and emergency share', () => {
    const db = openDb({ path: ':memory:', memory: true });
    seed(db, 'positions');
    db.prepare(`UPDATE positions SET model_prob = 0.75 WHERE exit_reason = 'TAKE_PROFIT_1'`).run();
    db.prepare(`INSERT INTO latency_samples (kind, latency_ms) VALUES ('detect_to_send', 500), ('detect_to_send', 700)`).run();
    const e = getEdgeAnalytics(db, { window: 100, iterations: 500 });
    expect(e.n).toBe(10);
    expect(e.netSol).toBeCloseTo(4 * 0.008 - 4 * 0.006 - 2 * 0.02, 9);
    // mean %: (4*20 - 4*15 - 2*50)/10 = -8
    expect(e.expectancyPct!.point).toBeCloseTo(-8, 9);
    expect(e.expectancyPct!.lo).toBeLessThanOrEqual(-8);
    expect(e.winRatePct).toBe(40);
    expect(e.emergencyShareOfLossesPct).toBeCloseTo((0.04 / 0.064) * 100, 6);
    expect(e.feePctOfNotional).toBeGreaterThan(0);
    expect(e.cohorts.byRisk.map((c) => c.cohort).sort()).toEqual(['relaxed', 'strict']);
    expect(e.cohorts.bySuffix.find((c) => c.cohort === 'other')?.n).toBe(2);
    expect(e.cohorts.byExit.find((c) => c.cohort === 'EMERGENCY_EXIT')?.netSol).toBeCloseTo(-0.04, 9);
    expect(e.latency.detect_to_send!.count).toBe(2);
    expect(e.calibration).toEqual([{ bin: '0.7-0.8', n: 4, meanProb: 0.75, winRatePct: 100 }]);
    // deterministic for a seed
    expect(getEdgeAnalytics(db, { iterations: 500 }).expectancyPct).toEqual(e.expectancyPct);
    db.close();
  });

  it('respects the window and works on the dry track (no model columns)', () => {
    const db = openDb({ path: ':memory:', memory: true });
    seed(db, 'dry_run_positions');
    const e = getEdgeAnalytics(db, { track: 'dry', window: 4, iterations: 200 });
    expect(e.n).toBe(4);
    // newest 4 = 2 stops + 2 emergencies
    expect(e.winRatePct).toBe(0);
    expect(e.calibration).toEqual([]);
    expect(getEdgeAnalytics(db, { track: 'live' }).n).toBe(0);
    expect(getEdgeAnalytics(db, { track: 'live' }).expectancyPct).toBeNull();
    db.close();
  });

  it('serves /api/analytics/edge', async () => {
    const db = openDb({ path: ':memory:', memory: true });
    seed(db, 'positions');
    const app = createDashboardApp({
      config: ConfigSchema.parse({ dashboard: { enabled: true, host: '127.0.0.1', port: 8787 } }),
      db,
    });
    const res = await app.request('/api/analytics/edge?window=5');
    expect(res.status).toBe(200);
    const body = (await res.json()) as { n: number; window: number; monitor: unknown };
    expect(body).toMatchObject({ n: 5, window: 5, monitor: null });
    db.close();
  });
});
