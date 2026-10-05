import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { getRacerSummary, listRacerMigrations } from '../src/dashboard/queries.ts';

describe('racer dashboard queries', () => {
  it('summarizes curves, slot distributions, competition, bundles and paper', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    repos.upsertRacerCurve({ mint: 'A', curve: 'cA', admitSlot: 100, admitCostSol: 4 });
    repos.upsertRacerCurve({ mint: 'A', curve: 'cA', fireSlot: 110, fireCostSol: 0.4, admitSlot: 999 });
    repos.upsertRacerCurve({ mint: 'A', curve: 'cA', buyLegMs: 80, buyLegOk: true, completeSlot: 120 });
    repos.upsertRacerCurve({ mint: 'A', curve: 'cA', outcome: 'migrated' });
    repos.upsertRacerCurve({ mint: 'B', curve: 'cB', admitSlot: 5, outcome: 'expired' });
    for (const [sig, ok, slot] of [['s1', false, 121], ['s2', true, 122], ['s3', false, 123]] as const) {
      repos.recordRacerMigrateTx({ signature: sig, mint: 'A', slot, ok, landed: ok, caller: ok ? 'W1' : 'L', curveBuyBefore: false, poolBuyAfter: ok });
    }
    repos.recordRacerMigration({
      mint: 'A', signature: 's2', migrateSlot: 122, caller: 'W1', tracked: true, admitSlot: 100, fireSlot: 110, completeSlot: 120,
      curveBuyBefore: false, poolBuyAfter: true, bundledTokens: '10', poolAddress: 'P', openPrice: 1e-7, reachPrice: 1.2e-7, reachQuoteSol: 90,
    });
    repos.recordRacerPaperTrack({
      mint: 'A', poolAddress: 'P', entrySlot: 122, sizeSol: 0.1, openPrice: 1e-7, openQuoteSol: 85, reachPrice: 1.2e-7, reachQuoteSol: 90,
      horizonsJson: JSON.stringify([{ sec: 30, price: 1.3e-7, quoteSol: 95, pnlOpenPct: 25, pnlReachPct: 5 }]),
    });

    const s = getRacerSummary(db);
    expect(s.curves).toMatchObject({ admitted: 2, fired: 1, migrated: 1, expired: 1, open: 0 });
    // First write wins: the later admitSlot 999 did not overwrite 100.
    expect(db.prepare(`SELECT admit_slot FROM racer_curves WHERE mint='A'`).get()).toEqual({ admit_slot: 100 });
    expect(s.slots.fireToComplete).toEqual({ n: 1, p50: 10, p90: 10 });
    expect(s.slots.completeToMigrate.p50).toBe(2);
    expect(s.competition).toMatchObject({ attempts: 3, failedAttempts: 2, failedPerMigration: 2, contestedShare: 1 });
    expect(s.bundles).toEqual({ curveBuyBeforeShare: 0, poolBuyAfterShare: 1 });
    expect(s.topCallers).toEqual([{ caller: 'W1', wins: 1, share: 1 }]);
    expect(s.buyLeg).toEqual({ okRate: 1, avgMs: 80 });
    expect(s.paper.horizons).toEqual([{ sec: 30, n: 1, medianOpenPct: 25, medianReachPct: 5, winRateOpen: 1, winRateReach: 1 }]);

    const rows = listRacerMigrations(db);
    expect(rows[0]).toMatchObject({ mint: 'A', fireToComplete: 10, completeToMigrate: 2, failedAttempts: 2, poolBuyAfter: true });
  });

  it('reports an empty racer cleanly', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const s = getRacerSummary(db);
    expect(s.migrations.total).toBe(0);
    expect(s.competition.failedPerMigration).toBeNull();
    expect(s.lastMigrateAt).toBeNull();
  });
});
