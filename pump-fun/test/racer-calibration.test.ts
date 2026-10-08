import { describe, expect, it } from 'vitest';
import {
  calibrateFill,
  calibrationAlerts,
  costDrift,
} from '../src/racer/calibration.ts';
import { openDb } from '../src/persistence/db.ts';
import { getRacerCalibration } from '../src/dashboard/queries.ts';

describe('calibrateFill', () => {
  it('gaps realized rates against predicted p per bucket', () => {
    const c = calibrateFill(
      [
        { offset: 1, contested: false, p: 0.9 },
        { offset: 1, contested: false, p: 0.7 },
        { offset: 2, contested: true, p: 0.5 },
      ],
      [
        { offset: 1, contested: false, ok: 1, n: 4 },
        { offset: 2, contested: true, ok: 3, n: 4 },
      ],
      3, 1,
    );
    expect(c.tracks).toBe(3);
    expect(c.buckets).toHaveLength(2);
    expect(c.buckets[0]).toMatchObject({ offset: 1, contested: false, predictedP: 0.8, observedRate: 0.25, gap: -0.55 });
    expect(c.buckets[1]).toMatchObject({ offset: 2, contested: true, predictedP: 0.5, observedRate: 0.75, gap: 0.25 });
    expect(c.meanAbsGap).toBeCloseTo(0.4, 12);
    expect(c.bias).toBeCloseTo(-0.15, 12);
  });

  it('gates small buckets and skips invalid predictions', () => {
    const c = calibrateFill(
      [{ offset: 1, contested: false, p: 0.9 }, { offset: 1, contested: false, p: 2 }],
      [{ offset: 1, contested: false, ok: 1, n: 1 }],
      1, 5,
    );
    expect(c.buckets[0]).toMatchObject({ predictedP: null, observedRate: null, gap: null, predictedN: 1, observedN: 1 });
    expect(c.meanAbsGap).toBeNull();
    expect(c.bias).toBeNull();
  });
});

describe('costDrift', () => {
  const H = 3_600_000;
  const now = Date.now();

  it('splits trailing vs prior windows and compares against config', () => {
    const d = costDrift(
      [
        { feeSol: 0.00001, tipSol: 0.0001, atMs: now - H },
        { feeSol: 0.00003, tipSol: 0.0003, atMs: now - 30 * H },
      ],
      now,
      { sellCostSol: 0.0005, liveCostSol: 0.001, windowHours: 24 },
    );
    expect(d.window).toMatchObject({ medianFeeSol: 0.00001, medianTipSol: 0.0001, n: 1 });
    expect(d.prior).toMatchObject({ medianFeeSol: 0.00003, medianTipSol: 0.0003, n: 1 });
    expect(d.feeDriftPct).toBeCloseTo(-200 / 3, 9);
    expect(d.measuredVsConfigPct).toBeCloseTo(-39, 9);
  });

  it('nulls empty windows and zero config', () => {
    const d = costDrift([], now, { sellCostSol: 0, liveCostSol: 0 });
    expect(d.window.medianFeeSol).toBeNull();
    expect(d.feeDriftPct).toBeNull();
    expect(d.measuredVsConfigPct).toBeNull();
  });
});

describe('calibrationAlerts', () => {
  it('fires past thresholds and stays quiet inside them', () => {
    const fill = calibrateFill([{ offset: 1, contested: false, p: 0.9 }], [{ offset: 1, contested: false, ok: 0, n: 4 }], 1, 1);
    const cost = costDrift([], Date.now(), { sellCostSol: 0, liveCostSol: 0 });
    const alerts = calibrationAlerts(fill, cost, {});
    expect(alerts.map((a) => a.code)).toEqual(['fill_gap']);
    expect(alerts[0]!.detail).toContain('offset 1');
    expect(calibrationAlerts(fill, cost, { fillGapAlert: 0.95 }).map((a) => a.code)).toEqual([]);
  });
});

describe('getRacerCalibration', () => {
  function seed(): ReturnType<typeof openDb> {
    const db = openDb({ path: ':memory:', memory: true });
    db.prepare(`INSERT INTO racer_migrations (mint, signature, migrate_slot, tracked, pool_address)
      VALUES ('A', 's1', 100, 1, 'P1'), ('B', 's2', 200, 1, 'P2')`).run();
    db.prepare(`INSERT INTO racer_migrate_txs (signature, mint, slot, ok, landed) VALUES ('txb', 'B', 199, 0, 0)`).run();
    const insT = db.prepare(`INSERT INTO racer_paper_tracks
      (mint, size_sol, reach_price, reach_quote_sol, horizons_json, entries_json, created_at, armed,
       entry_model_version, entry_quality, fill_p_json, entry_features_json,
       live_cost_sol, live_cost_source, live_cost_buy_fee_sol, live_cost_buy_tip_sol)
      VALUES (?, 0.1, 1, 10, '[]', '{}', ?, 1, 2, 'verified', ?, ?, 0.001, 'measured', ?, ?)`);
    insT.run('A', '2026-01-01 00:00:00',
      JSON.stringify({ slot1: { p: 0.9, costSol: 0.001 }, slot1mid: { p: 0.9, costSol: 0.001 } }),
      JSON.stringify({ knownFailedMigrateAttempts: 0 }), 0.00001, 0.0001);
    insT.run('B', '2026-01-01 00:00:00',
      JSON.stringify({ slot1: { p: 0.6, costSol: 0.001 } }),
      JSON.stringify({ knownFailedMigrateAttempts: 2 }), 0.00001, 0.0001);
    const insA = db.prepare(`INSERT INTO racer_pool_buy_attempts (signature, pool_address, slot, ok, created_at)
      VALUES (?, ?, ?, ?, ?)`);
    // Offset-1 outcomes for A inside the forward window: 2/4 land.
    insA.run('a1', 'P1', 101, 1, '2026-01-01 00:00:30');
    insA.run('a2', 'P1', 101, 1, '2026-01-01 00:01:00');
    insA.run('a3', 'P1', 101, 0, '2026-01-01 00:02:00');
    insA.run('a4', 'P1', 101, 0, '2026-01-01 00:03:00');
    // Outside the window (before entry / past 24h): excluded.
    insA.run('a5', 'P1', 101, 1, '2025-12-31 23:59:00');
    insA.run('a6', 'P1', 101, 1, '2026-01-02 00:00:01');
    // Contested offset-1 outcome for B: 1/1 lands.
    insA.run('b1', 'P2', 201, 1, '2026-01-01 00:00:30');
    return db;
  }

  it('replays forward attempts against stamped predictions', () => {
    const r = getRacerCalibration(seed(), { minN: 1 });
    expect(r.fill.tracks).toBe(2);
    const plain = r.fill.buckets.find((b) => b.offset === 1 && !b.contested)!;
    expect(plain).toMatchObject({ predictedP: 0.9, observedRate: 0.5, predictedN: 1, observedN: 4, gap: -0.4 });
    const contested = r.fill.buckets.find((b) => b.offset === 1 && b.contested)!;
    expect(contested).toMatchObject({ predictedP: 0.6, observedRate: 1, gap: 0.4 });
    expect(r.alerts.map((a) => a.code)).toContain('fill_gap');
    expect(r.landing).toEqual({ calibrated: false, reason: expect.stringContaining('no live sends') });
  });

  it('reads cost medians from measured tracks', () => {
    const db = seed();
    // Fresh timestamps so the samples land in the trailing window.
    db.prepare(`UPDATE racer_paper_tracks SET created_at = datetime('now')`).run();
    const r = getRacerCalibration(db, { minN: 1, sellCostSol: 0.0005, liveCostSol: 0.001 });
    expect(r.cost.window).toMatchObject({ medianFeeSol: 0.00001, medianTipSol: 0.0001, n: 2 });
    expect(r.cost.measuredVsConfigPct).toBeCloseTo(-39, 9);
  });
});
