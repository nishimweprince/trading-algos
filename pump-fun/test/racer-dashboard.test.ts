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
    expect(first.headline.fixedSizePnlCurve.map((p) => p.mint)).toEqual(['A', 'B', 'C']);
    expect(first.headline.fixedSizePnlCurve.at(-1)!.cumulativePnlSol).toBeCloseTo(0.04);
    expect(first.cohorts.highDepletion).toMatchObject({ n: 1, mean: -10 });
    expect(first.cohorts.belowHighDepletion).toMatchObject({ n: 2, mean: 25 });
    expect(first.cohorts.byCaller[0]).toMatchObject({ caller: 'W1', stats: { n: 2, mean: 5 } });
  });

  it('reconciles migration coverage, keeps unknown depletion separate, and reports missing reprices', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const migration = (mint: string, reason: string | null = null) => repos.recordRacerMigration({
      mint, signature: `sig-${mint}`, migrateSlot: 10, caller: 'C', tracked: true, admitSlot: 8,
      fireSlot: 8, completeSlot: 9, curveBuyBefore: false, poolBuyAfter: false, bundledTokens: null,
      poolAddress: `pool-${mint}`, openPrice: 1, reachPrice: 1, reachQuoteSol: 12,
      unpricedReason: reason, initialQuoteDepthSol: reason ? 8 : 12,
      observedQuoteDepthSol: reason ? 9 : 12, depthThresholdSol: 10,
    });
    migration('A'); migration('B', 'thin-pool'); migration('C'); migration('D');
    repos.markRacerMigrationUnpriced('D', 'paper-capacity');
    repos.recordRacerPaperTrack({ mint: 'A', poolAddress: 'pool-A', entrySlot: 10, sizeSol: 0.1,
      openPrice: 1, openQuoteSol: 12, reachPrice: 1, reachQuoteSol: 12,
      entriesJson: JSON.stringify({ slot1mid: { price: 1, quoteSol: 12 } }),
      horizonsJson: JSON.stringify([{ sec: 30, price: 1.1, quoteSol: 12, exitPrice: 1.1,
        exitQuoteSol: 12, exitRealQuoteSol: 10, pnlOpenPct: 5, pnlReachPct: 5, pnl: { slot1mid: 5 } }]),
      entryModelVersion: 2, entryQuality: 'verified', sameSlotDepletionPct: null, liveCostSol: 0.001,
      entryFeaturesJson: JSON.stringify({ version: 1, observedAtMs: 100, migrateSlot: 10,
        fireToCompleteSlots: 1, completeToMigrateSlots: 1, caller: 'C', quoteVaultSol: 12,
        tokenProgram: null, sameSlotDepletionPct: null, knownFailedMigrateAttempts: 0 }),
    });
    repos.recordRacerPoolBuyAttempt({ signature: 'ok', poolAddress: 'pool-A', slot: 11, ok: true, buyerOwner: 'buyer', quoteVolumeSol: 2 });
    repos.recordRacerPoolBuyAttempt({ signature: 'fail', poolAddress: 'pool-A', slot: 11, ok: false, networkFeeSol: 0.001 });
    repos.recordRacerPath('A', JSON.stringify({ version: 1, states: [{ atMs: 1, mark: 'gone' }] }),
      JSON.stringify({ time300: { reason: 'time', pnlPct: 4, exitAtMs: 1000, fill: 'unchanged' }, stop20: { reason: 'stop', pnlPct: -30, exitAtMs: 900 } }));
    const s = getRacerSummary(db, { proposedSizeSol: 0.05 });
    expect(s.migrations.coverage).toEqual({ verified: 1, 'unpriced:thin-pool': 1, 'missing-track': 1, 'unpriced:paper-capacity': 1 });
    expect(Object.values(s.migrations.coverage).reduce((a, b) => a + b, 0)).toBe(s.migrations.total);
    expect(listRacerMigrations(db).find((r) => r.mint === 'B')).toMatchObject({ initialQuoteDepthSol: 8, observedQuoteDepthSol: 9, depthThresholdSol: 10 });
    expect(s.paper.horizons[0]!.cohorts.unknownDepletion.n).toBe(1);
    expect(s.paper.horizons[0]!.cohorts.belowHighDepletion.n).toBe(0);
    expect(s.paper.horizons[0]!.headline.fixedSizePnlCurve).toHaveLength(1);
    expect(s.paper.horizons[0]!.sizeScenario).toMatchObject({ sizeSol: 0.05, priced: 1, unavailable: 0 });
    expect(s.execution).toMatchObject({ slot1BuyAttempts: 2, slot1Failed: 1, observedOnChainFillRate: 0.5, offChainDropRate: null });
    expect(s.execution).toMatchObject({ slot1BuyVolumeSol: 2, slot1DistinctBuyerOwners: 1 });
    expect(s.paper.horizons[0]!.sizeScenario.onChainFillAdjustedPnlSol).toBeCloseTo(s.paper.horizons[0]!.sizeScenario.pnlSol / 2 - 0.0005);
    expect(s.paper.pathRules.find((x) => x.rule === 'time300')).toMatchObject({ n: 1, unpriced: 0, unchangedFills: 1,
      result: { mean: 4 }, sameTradeTimeBaseline: { mean: 5 } });
    expect(s.paper.pathRules.find((x) => x.rule === 'stop20')).toMatchObject({ n: 1, unchangedFills: 0, notCaptured: 0 });
  });
});

describe('racer latency block', () => {
  it('reports detection, build, outside-landing and exit stats with provenance-specific sources', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    for (const v of [-1, 0, 0, 1]) repos.recordLatencySample({ kind: 'racer_detect_slots', latencyMs: v, mint: 'M' });
    repos.upsertRacerCurve({ mint: 'A', curve: 'cA', buyLegMs: 300, buyLegOk: true });
    repos.upsertRacerCurve({ mint: 'B', curve: 'cB', buyLegMs: 500, buyLegOk: true });
    repos.upsertRacerCurve({ mint: 'C', curve: 'cC', buyLegMs: 9_999, buyLegOk: false });
    const mig = (mint: string, slot: number) =>
      repos.recordRacerMigration({
        mint, signature: `s-${mint}`, migrateSlot: slot, caller: 'W', tracked: true, admitSlot: null, fireSlot: null, completeSlot: null,
        curveBuyBefore: false, poolBuyAfter: false, bundledTokens: null, poolAddress: `pool-${mint}`, openPrice: null, reachPrice: null, reachQuoteSol: null,
      });
    mig('A', 100);
    mig('B', 200);
    repos.recordRacerPoolBuyAttempt({ signature: 'a0', poolAddress: 'pool-A', slot: 100, ok: true });
    repos.recordRacerPoolBuyAttempt({ signature: 'a1', poolAddress: 'pool-A', slot: 101, ok: true });
    repos.recordRacerPoolBuyAttempt({ signature: 'b-fail', poolAddress: 'pool-B', slot: 201, ok: false });
    repos.recordRacerPoolBuyAttempt({ signature: 'b2', poolAddress: 'pool-B', slot: 202, ok: true });
    const l = getRacerSummary(db, { exitLandMs: 450, modeledEntrySlot: 1 }).latency;
    expect(l.detection.racerSlots).toEqual({ n: 4, p50: 0, p90: 0.7000000000000002 });
    for (const v of [1_500, 2_500]) repos.recordLatencySample({ kind: 'racer_send_ready_us', latencyMs: v, mint: 'M' });
    const l2 = getRacerSummary(db, { exitLandMs: 450 }).latency;
    expect(l2.sending.sendReadyMs).toMatchObject({ n: 2, p50: 2 });
    // Feasible landing: A lands S+1 though its unfloored estimate was S (still open); B lands S+2.
    db.prepare(`UPDATE racer_migrations SET feasible_slot = 101, feasible_slot_raw = 100, receipt_slot = 100, send_ready_us = 2500 WHERE mint = 'A'`).run();
    db.prepare(`UPDATE racer_migrations SET feasible_slot = 202, feasible_slot_raw = 202, receipt_slot = 201 WHERE mint = 'B'`).run();
    const l3 = getRacerSummary(db).latency;
    expect(l3.landing.feasibleOffsetSlots).toMatchObject({ n: 2, p50: 1.5 });
    expect(l3.landing.migrateSlotStillOpenShare).toBe(0.5);
    expect(listRacerMigrations(db).find((r) => r.mint === 'A')).toMatchObject({ migrateSlot: 100, receiptSlot: 100, feasibleSlot: 101, sendReadyUs: 2500 });
    expect(l.sending.liveDetectToSendMs.n).toBe(0);
    // First successful outside buy: A in the migrate slot (0), B two slots later (failed S+1 ignored).
    expect(l.landing.firstOutsideBuySlots).toMatchObject({ n: 2, p50: 1 });
    expect(l.landing.sameSlotBuyShare).toBe(0.5);
    expect(l.landing.modeledEntrySlot).toBe(1);
    expect(l.exit.assumedExitLandMs).toBe(450);
    expect(l.exit.liveExitSendToLandedMs.n).toBe(0);
  });
});

describe('racer bootstrap interval', () => {
  it('does not collapse to the mean for small power-of-two samples (LCG low-bit period)', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const mig = (mint: string) =>
      repos.recordRacerMigration({
        mint, signature: `s-${mint}`, migrateSlot: 1, caller: 'W', tracked: true, admitSlot: null, fireSlot: null, completeSlot: null,
        curveBuyBefore: false, poolBuyAfter: false, bundledTokens: null, poolAddress: `p-${mint}`, openPrice: 1e-7, reachPrice: 1e-7, reachQuoteSol: 85,
      });
    ['A', 'B', 'C', 'D'].forEach(mig);
    const pnl = { A: -60, B: -20, C: 10, D: 40 } as const;
    for (const [mint, v] of Object.entries(pnl)) {
      repos.recordRacerPaperTrack({
        mint, poolAddress: `p-${mint}`, entrySlot: 1, sizeSol: 0.1, openPrice: 1e-7, openQuoteSol: 85, reachPrice: 1e-7, reachQuoteSol: 85,
        horizonsJson: JSON.stringify([{ sec: 300, price: 1e-7, quoteSol: 85, pnlOpenPct: v, pnlReachPct: v, exitPrice: 1e-7, exitQuoteSol: 85, pnl: { reach: v, slot1mid: v } }]),
        entriesJson: JSON.stringify({ slot1mid: { price: 1e-7, quoteSol: 85 } }), armed: true, entryModelVersion: 2, entryQuality: 'verified', sameSlotDepletionPct: 0,
      });
    }
    const h = getRacerSummary(db).paper.horizons.find((x) => x.sec === 300)!;
    expect(h.headline.mean).toBeCloseTo(-7.5, 6);
    expect(h.headline.ciLow!).toBeLessThan(-30);
    expect(h.headline.ciHigh!).toBeGreaterThan(15);
  });
});
