import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { getRacerSummary, listRacerPaper } from '../src/dashboard/queries.ts';
import { adjustLegForFill, attemptCostSol, legFill, rateFor, type FillStats } from '../src/racer/fills.ts';
import { PaperTracker, type PaperTrack } from '../src/racer/paper.ts';

const STATS: FillStats = {
  buckets: [
    { offset: 1, contested: false, ok: 64, n: 100 },
    { offset: 1, contested: true, ok: 10, n: 40 },
    { offset: 2, contested: false, ok: 8, n: 10 },
  ],
  meanFailedFeeSol: 0.00001,
};

describe('rateFor', () => {
  it('reads the exact bucket', () => {
    expect(rateFor(STATS, 1, false)).toBe(0.64);
    expect(rateFor(STATS, 1, true)).toBe(0.25);
  });
  it('falls back to the offset-pooled rate below min n, then to null', () => {
    expect(rateFor(STATS, 2, true, 10)).toBe(0.8); // pooled over contested
    expect(rateFor(STATS, 2, false, 20)).toBeNull(); // pooled n = 10 < 20
    expect(rateFor(STATS, 3, false)).toBeNull(); // no bucket at all
  });
});

describe('attemptCostSol', () => {
  it('charges expected failures at the mean failed fee', () => {
    expect(attemptCostSol(1, 0.00001)).toBe(0);
    expect(attemptCostSol(0.5, 0.00001)).toBeCloseTo(0.00001, 10); // (1-0.5)/0.5 = 1 failure
    expect(attemptCostSol(0.25, 0.00001)).toBeCloseTo(0.00003, 10); // 3 failures
  });
  it('is null-safe', () => {
    expect(attemptCostSol(null, 0.00001)).toBeNull();
    expect(attemptCostSol(0, 0.00001)).toBeNull();
    expect(attemptCostSol(0.5, null)).toBeNull();
    expect(attemptCostSol(1.5, 0.00001)).toBeNull();
  });
});

describe('legFill / adjustLegForFill', () => {
  it('prices a leg from trailing stats', () => {
    expect(legFill(STATS, 1, false)).toEqual({ p: 0.64, costSol: (0.36 / 0.64) * 0.00001 });
    expect(legFill(STATS, 3, false)).toBeNull();
  });
  it('weights the conditional value and always charges attempts', () => {
    // Winner: 10% × 0.5 minus 0.00001/0.1 = 0.01%.
    expect(adjustLegForFill(10, { p: 0.5, costSol: 0.00001 }, 0.1)).toBeCloseTo(4.99, 9);
    // Loser: loses less often, still pays attempts.
    expect(adjustLegForFill(-10, { p: 0.5, costSol: 0.00001 }, 0.1)).toBeCloseTo(-5.01, 9);
    expect(adjustLegForFill(10, { p: 0.5, costSol: 0.00001 }, 0)).toBeNull();
  });
});

describe('trailingFillStats', () => {
  it('buckets attempts by offset and contention with the mean failed fee', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const mig = { signature: 's', caller: 'W', tracked: true, admitSlot: 1, fireSlot: 2, completeSlot: 3,
      curveBuyBefore: false, poolBuyAfter: false, bundledTokens: null, reachPrice: 1e-7, reachQuoteSol: 90 };
    repos.recordRacerMigration({ ...mig, mint: 'C', migrateSlot: 100, poolAddress: 'PC', openPrice: 1e-7 });
    repos.recordRacerMigration({ ...mig, mint: 'U', migrateSlot: 200, poolAddress: 'PU', openPrice: 1e-7 });
    repos.recordRacerMigrateTx({ signature: 'f1', mint: 'C', slot: 100, ok: false, landed: false, caller: 'L', curveBuyBefore: false, poolBuyAfter: false });
    for (let i = 0; i < 25; i++) {
      repos.recordRacerPoolBuyAttempt({ signature: `c-ok-${i}`, poolAddress: 'PC', slot: 101, ok: i < 10, networkFeeSol: 0.00001 });
      repos.recordRacerPoolBuyAttempt({ signature: `u-ok-${i}`, poolAddress: 'PU', slot: 201, ok: i < 20, networkFeeSol: 0.00002 });
    }
    const s = repos.trailingFillStats(24);
    expect(s.meanFailedFeeSol).toBeCloseTo((15 * 0.00001 + 5 * 0.00002) / 20, 10);
    const contested = s.buckets.find((b) => b.offset === 1 && b.contested)!;
    const calm = s.buckets.find((b) => b.offset === 1 && !b.contested)!;
    expect(contested).toMatchObject({ ok: 10, n: 25 });
    expect(calm).toMatchObject({ ok: 20, n: 25 });
  });
});

describe('paper expectancy legs', () => {
  const drive = async (fill: PaperTrack['fill']) => {
    const tracks: PaperTrack[] = [];
    const p = new PaperTracker(
      { readMark: async () => ({ price: 2e-7, quoteSol: 1e9 }), sink: (t) => void tracks.push(t), schedule: (_ms, fn) => fn() },
      { horizonsSec: [30], sizeSol: 0.1 },
    );
    p.track('M', { poolAddress: 'P', slot: 1, open: null, reach: { price: 1e-7, quoteSol: 1e9 },
      delayed: { slot1: { price: 1e-7, quoteSol: 1e9 } }, fill });
    await new Promise((r) => setTimeout(r, 0));
    return { p, track: tracks.at(-1)! };
  };
  it('prints _adj legs from the fill model', async () => {
    const { track } = await drive({ slot1: { p: 0.5, costSol: 0.00001 } });
    const h = track.horizons[0]!;
    expect(h.pnl.slot1).not.toBeNull();
    expect(h.pnl.slot1_adj).toBeCloseTo(h.pnl.slot1! * 0.5 - 0.01, 9);
  });
  it('prints no _adj legs without a fill model', async () => {
    const { track } = await drive({});
    expect(track.horizons[0]!.pnl.slot1_adj).toBeUndefined();
  });
  it('re-costs _adj legs by deltaPct × p', async () => {
    const scheduled: Array<() => void> = [];
    const p = new PaperTracker(
      { readMark: async () => ({ price: 2e-7, quoteSol: 1e9 }), sink: () => undefined, schedule: (_ms, fn) => void scheduled.push(fn) },
      { horizonsSec: [30, 60], sizeSol: 0.1 },
    );
    p.track('M', { poolAddress: 'P', slot: 1, open: null, reach: { price: 1e-7, quoteSol: 1e9 },
      delayed: { slot1: { price: 1e-7, quoteSol: 1e9 } }, fill: { slot1: { p: 0.5, costSol: 0.00001 } } });
    scheduled[0]!(); // mark the 30 s horizon; the track stays live (60 s pending)
    await new Promise((r) => setTimeout(r, 0));
    const track = (p as unknown as { live: Map<string, PaperTrack> }).live.get('M')!;
    const before = track.horizons[0]!.pnl.slot1_adj!;
    p.setLiveCost('M', 0.002); // liveCostSol was 0 → deltaPct = 2
    expect(track.horizons[0]!.pnl.slot1_adj!).toBeCloseTo(before - 2 * 0.5, 9);
    expect(track.horizons[0]!.pnl.slot1).not.toBeNull();
  });
});

describe('expectancy legs in queries', () => {
  it('flow into the medians and the paper rows', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    repos.recordRacerPaperTrack({
      mint: 'F', poolAddress: 'P', entrySlot: 1, sizeSol: 0.1, openPrice: 1e-7, openQuoteSol: 85,
      reachPrice: 1e-7, reachQuoteSol: 85,
      horizonsJson: JSON.stringify([{ sec: 30, price: 2e-7, quoteSol: 120, pnlOpenPct: 15, pnlReachPct: 15,
        pnl: { open: 15, reach: 15, slot1: 10, slot1_adj: 4.99 } }]),
      entriesJson: JSON.stringify({ slot1: { price: 1.5e-7, quoteSol: 100 } }),
      entryModelVersion: 2, entryQuality: 'verified', fillPJson: JSON.stringify({ slot1: { p: 0.5, costSol: 0.00001 } }),
    });
    const s = getRacerSummary(db);
    expect(s.paper.horizons.find((h) => h.sec === 30)!.entries.slot1_adj).toMatchObject({ n: 1, median: 4.99 });
    expect(listRacerPaper(db, { all: true })[0]!.fill).toEqual({ slot1: { p: 0.5, costSol: 0.00001 } });
  });
});
