import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { getRacerSummary, listRacerMigrations, listRacerPaper } from '../src/dashboard/queries.ts';

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
    repos.recordRacerMigration({
      mint: 'Q', signature: 'q1', migrateSlot: 200, caller: 'W1', tracked: false, admitSlot: null, fireSlot: null, completeSlot: null,
      curveBuyBefore: false, poolBuyAfter: false, bundledTokens: null, poolAddress: 'PX', openPrice: null, reachPrice: null, reachQuoteSol: null,
      poolOwner: 'PX', quoteMint: 'SPCX', unpricedReason: 'non-wsol-quote',
    });
    repos.recordRacerPaperTrack({
      mint: 'A', poolAddress: 'P', entrySlot: 122, sizeSol: 0.1, openPrice: 1e-7, openQuoteSol: 85, reachPrice: 1.2e-7, reachQuoteSol: 90,
      horizonsJson: JSON.stringify([{ sec: 30, price: 1.3e-7, quoteSol: 95, pnlOpenPct: 25, pnlReachPct: 5 }]),
    });
    // A realistic-entry track: reach looks great, +1 slot (net of live cost) does not.
    repos.recordRacerPaperTrack({
      mint: 'R', poolAddress: 'PR', entrySlot: 300, sizeSol: 0.1, openPrice: 1e-7, openQuoteSol: 85, reachPrice: 1e-7, reachQuoteSol: 85,
      horizonsJson: JSON.stringify([
        { sec: 30, price: 2e-7, quoteSol: 120, pnlOpenPct: 15, pnlReachPct: 15, pnl: { open: 15, reach: 15, slot1: -30, slot2: -40 } },
      ]),
      entriesJson: '{}', armed: true, liveCostSol: 0.002,
    });
    // Measured cost arrives later: the upsert updates horizons and cost in place.
    repos.recordRacerPaperTrack({
      mint: 'R', poolAddress: 'PR', entrySlot: 300, sizeSol: 0.1, openPrice: 1e-7, openQuoteSol: 85, reachPrice: 1e-7, reachQuoteSol: 85,
      horizonsJson: JSON.stringify([
        { sec: 30, price: 2e-7, quoteSol: 120, pnlOpenPct: 15, pnlReachPct: 15, pnl: { open: 15, reach: 15, slot1: -30, slot2: -40 } },
      ]),
      entriesJson: '{}', armed: true, liveCostSol: 0.004, liveCostSource: 'measured',
      entryModelVersion: 2, entryQuality: 'verified', sameSlotDepletionPct: 95,
    });
    // A rugged pool: scored, not dropped.
    repos.recordRacerPaperTrack({
      mint: 'G', poolAddress: 'PG', entrySlot: 400, sizeSol: 0.1, openPrice: null, openQuoteSol: null, reachPrice: 1e-7, reachQuoteSol: 85,
      horizonsJson: JSON.stringify([{ sec: 30, price: 0, quoteSol: null, pnlOpenPct: null, pnlReachPct: -100, rugged: true, pnl: { reach: -100, slot1: -102 } }]),
      entriesJson: '{}', armed: false, liveCostSol: 0.002, liveCostSource: 'config',
      entryModelVersion: 2, entryQuality: 'late-armed', sameSlotDepletionPct: null,
    });

    const s = getRacerSummary(db);
    expect(s.curves).toMatchObject({ admitted: 2, fired: 1, migrated: 1, expired: 1, open: 0 });
    // First write wins: the later admitSlot 999 did not overwrite 100.
    expect(db.prepare(`SELECT admit_slot FROM racer_curves WHERE mint='A'`).get()).toEqual({ admit_slot: 100 });
    expect(s.slots.fireToComplete).toEqual({ n: 1, p50: 10, p90: 10 });
    expect(s.slots.completeToMigrate.p50).toBe(2);
    expect(s.competition).toMatchObject({ attempts: 3, failedAttempts: 2, failedPerMigration: 1, contestedShare: 0.5 });
    expect(s.migrations.unpriced).toEqual({ 'non-wsol-quote': 1 });
    expect(s.bundles).toEqual({ curveBuyBeforeShare: 0, poolBuyAfterShare: 0.5 });
    expect(s.topCallers).toEqual([{ caller: 'W1', wins: 2, share: 1 }]);
    expect(s.buyLeg).toEqual({ okRate: 1, avgMs: 80 });
    expect(s.paper.horizons).toMatchObject([{ sec: 30, n: 1, medianOpenPct: 15, medianReachPct: 15 }]);
    expect(s.paper.horizons[0]!.entries.slot1).toEqual({ n: 1, median: -30, winRate: 0 });
    expect(s.paper).toMatchObject({ tracks: 3, realisticTracks: 2, verifiedTracks: 1, excludedReasons: { 'legacy-unverified': 1, 'late-armed': 1 }, armedShare: 0.5, ruggedTracks: 1, measuredCostShare: 0.5, medianLiveCostSol: 0.004 });
    expect(listRacerPaper(db)).toHaveLength(3);

    const rows = listRacerMigrations(db);
    expect(rows[1]).toMatchObject({ mint: 'A', fireToComplete: 10, completeToMigrate: 2, failedAttempts: 2, poolBuyAfter: true });
  });

  it('reports an empty racer cleanly', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const s = getRacerSummary(db);
    expect(s.migrations.total).toBe(0);
    expect(s.competition.failedPerMigration).toBeNull();
    expect(s.lastMigrateAt).toBeNull();
  });

  it('separates same-slot buying and callers from one verified set with stable statistics', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    for (const [mint, caller, pct, depletion] of [
      ['A', 'W1', -10, 95], ['B', 'W1', 20, 10], ['C', 'W2', 30, 5],
    ] as const) {
      repos.recordRacerMigration({ mint, signature: `sig-${mint}`, migrateSlot: 1, caller, tracked: true,
        admitSlot: 1, fireSlot: 1, completeSlot: 1, curveBuyBefore: false, poolBuyAfter: false,
        bundledTokens: null, poolAddress: `pool-${mint}`, openPrice: 1, reachPrice: 1, reachQuoteSol: 80 });
      repos.recordRacerPaperTrack({ mint, poolAddress: `pool-${mint}`, entrySlot: 1, sizeSol: 0.1,
        openPrice: 1, openQuoteSol: 80, reachPrice: 1, reachQuoteSol: 80,
        horizonsJson: JSON.stringify([{ sec: 30, price: 1, quoteSol: 80, pnlOpenPct: pct, pnlReachPct: pct, pnl: { slot1mid: pct } }]),
        armed: true, entryModelVersion: 2, entryQuality: 'verified', sameSlotDepletionPct: depletion });
    }
    const first = getRacerSummary(db).paper.horizons[0]!;
    const second = getRacerSummary(db).paper.horizons[0]!;
    expect(first).toEqual(second);
    expect(first.headline).toMatchObject({ mean: 40 / 3, meanWithoutBest: 5, cumulativeFixedSizePnlSol: 0.04 });
    expect(first.headline.ciLow).toBeLessThan(first.headline.mean!);
    expect(first.headline.ciHigh).toBeGreaterThan(first.headline.mean!);
    expect(first.cohorts.highDepletion).toMatchObject({ n: 1, mean: -10 });
    expect(first.cohorts.belowHighDepletion).toMatchObject({ n: 2, mean: 25 });
    expect(first.cohorts.byCaller[0]).toMatchObject({ caller: 'W1', stats: { n: 2, mean: 5 } });
  });
});
