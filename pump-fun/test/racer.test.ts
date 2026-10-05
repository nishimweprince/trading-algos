import { describe, expect, it, vi } from 'vitest';
import { completionCostSol, curveProgress, evaluateFire, paperPnlPct } from '../src/racer/economics.ts';
import { RacerWatcher, type RacerEvent } from '../src/racer/watcher.ts';
import { PaperTracker, type PaperTrack } from '../src/racer/paper.ts';
import type { CurveReserves } from '../src/enrichment/curve.ts';

function reserves(over: Partial<CurveReserves> = {}): CurveReserves {
  return {
    curveAddress: 'curve',
    // Near-complete shape: most virtual tokens bought, little real left.
    virtualTokenReserves: 200_000_000_000n,
    virtualSolReserves: 150_000_000_000n,
    realTokenReserves: 1_000_000_000n,
    realSolReserves: 80_000_000_000n,
    tokenTotalSupply: 1_000_000_000_000_000n,
    complete: false,
    ...over,
  };
}

describe('completionCostSol', () => {
  it('prices a constant-product drain of the remaining real tokens', () => {
    // k = 2e11 * 1.5e11 = 3e22; remaining vT = 2e11 - 1e9 = 1.99e11;
    // cost = 3e22/1.99e11 - 1.5e11 ≈ 7.5377e8 lamports ≈ 0.7538 SOL.
    expect(completionCostSol(reserves())).toBeCloseTo(0.7538, 3);
  });
  it('returns 0 for complete/empty curves and null for unpriced shapes', () => {
    expect(completionCostSol(reserves({ complete: true }))).toBe(0);
    expect(completionCostSol(reserves({ realTokenReserves: 0n }))).toBe(0);
    expect(completionCostSol(reserves({ virtualTokenReserves: 0n }))).toBeNull();
    expect(completionCostSol(reserves({ virtualTokenReserves: 500n, realTokenReserves: 999n }))).toBeNull();
  });
});

describe('curveProgress / evaluateFire', () => {
  it('tracks graduation progress and gates on the cost ceiling', () => {
    expect(curveProgress(reserves())).toBeCloseTo(80 / 85, 4);
    expect(curveProgress(reserves({ complete: true }))).toBe(1);
    expect(evaluateFire(reserves(), 1).fire).toBe(true);
    expect(evaluateFire(reserves(), 0.1)).toMatchObject({ fire: false, reason: 'too-expensive' });
    expect(evaluateFire(null, 1).reason).toBe('no-curve');
    expect(evaluateFire(reserves({ complete: true }), 1).reason).toBe('complete');
  });
});

describe('paperPnlPct', () => {
  it('applies fee drag and rejects bad prices', () => {
    expect(paperPnlPct(100, 150, 0.01)).toBeCloseTo(48.5, 6);
    expect(paperPnlPct(0, 150, 0.01)).toBeNull();
  });
});

describe('RacerWatcher', () => {
  function setup(curveByMint: Record<string, CurveReserves | null>) {
    const events: RacerEvent[] = [];
    const w = new RacerWatcher(
      {
        fetchCurve: async (mint) => curveByMint[mint] ?? null,
        getSlot: async () => 1000,
        sink: (e) => void events.push(e),
        now: () => 0,
        buildBuyLeg: async () => ({ ok: true, ms: 120 }),
      },
      { maxCostSol: 1 },
    );
    return { w, events };
  }
  it('fires once when cost drops under the ceiling, with buy-leg timing', async () => {
    const { w, events } = setup({ M: reserves() });
    w.addLaunch('M');
    await w.tick();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ mint: 'M', kind: 'would-fire', slot: 1000, buyLegMs: 120, buyLegOk: true });
    expect(events[0]!.costSol).toBeCloseTo(0.7538, 3);
    await w.tick();
    expect(events).toHaveLength(1); // first-fire-only
  });
  it('emits complete-no-pool when the flipped curve has no readable pool', async () => {
    const events: RacerEvent[] = [];
    const curves: Record<string, CurveReserves | null> = { P: reserves(), N: reserves() };
    const w = new RacerWatcher(
      {
        fetchCurve: async (mint) => curves[mint] ?? null,
        getSlot: async () => 1000,
        sink: (e) => void events.push(e),
        now: () => 0,
        readPool: async (mint) => mint === 'P',
      },
      { maxCostSol: 100 },
    );
    w.addLaunch('P');
    w.addLaunch('N');
    await w.tick(); // both fire (ceiling 100)
    curves.P = reserves({ complete: true, realTokenReserves: 0n });
    curves.N = reserves({ complete: true, realTokenReserves: 0n });
    await w.tick();
    expect(events.map((e) => `${e.mint}:${e.kind}`)).toEqual(['P:would-fire', 'N:would-fire', 'P:migrated', 'N:complete-no-pool']);
  });
  it('reports migrated with lead slots and expires stale tracks', async () => {
    let now = 0;
    const events: RacerEvent[] = [];
    const curves: Record<string, CurveReserves | null> = { M: reserves(), S: reserves({ realTokenReserves: 999_999_999_999n }) };
    const w = new RacerWatcher(
      {
        fetchCurve: async (mint) => curves[mint] ?? null,
        getSlot: async () => 1000,
        sink: (e) => void events.push(e),
        now: () => now,
      },
      { maxCostSol: 1, trackWindowMs: 100 },
    );
    w.addLaunch('M');
    w.addLaunch('S');
    await w.tick(); // M fires; S too expensive
    expect(events.map((e) => e.kind)).toEqual(['would-fire']);
    curves.M = reserves({ complete: true, realTokenReserves: 0n });
    await w.tick(); // M migrates with 0-slot lead
    expect(events[1]).toMatchObject({ mint: 'M', kind: 'migrated', leadSlots: 0 });
    expect(w.size).toBe(1);
    now = 10_000;
    await w.tick(); // S expires
    expect(events[2]).toMatchObject({ mint: 'S', kind: 'expired' });
    expect(w.size).toBe(0);
  });
});

describe('PaperTracker', () => {
  it('prices entry then horizons and sinks the completed track', async () => {
    const tracks: PaperTrack[] = [];
    const jobs: Array<() => void> = [];
    const t = new PaperTracker(
      {
        readPrice: async () => ({ price: 100, poolAddress: 'pool', slot: 5 }),
        sink: (tr) => void tracks.push(tr),
        schedule: (_ms, fn) => void jobs.push(fn),
      },
      { horizonsSec: [30], sizeSol: 0.2, feeFraction: 0 },
    );
    t.track('M');
    await vi.waitFor(() => expect(jobs.length).toBe(1));
    expect(t.pendingCount).toBe(1);
    jobs[0]!();
    await vi.waitFor(() => expect(tracks.length).toBe(1));
    expect(tracks[0]).toMatchObject({ mint: 'M', entryPrice: 100, sizeSol: 0.2 });
    expect(tracks[0]!.horizons).toEqual([{ sec: 30, price: 100, pnlPct: 0 }]);
    expect(t.pendingCount).toBe(0);
  });
  it('abandons after entry retries and caps pending tracks', async () => {
    const tracks: PaperTrack[] = [];
    const jobs: Array<() => void> = [];
    const t = new PaperTracker(
      {
        readPrice: async () => null,
        sink: (tr) => void tracks.push(tr),
        schedule: (_ms, fn) => void jobs.push(fn),
      },
      { entryRetries: 2, entryRetryMs: 1, maxPending: 1 },
    );
    t.track('A');
    t.track('B'); // over cap
    expect(t.abandoned).toBe(1);
    await vi.waitFor(() => expect(jobs.length).toBe(1));
    jobs[0]!();
    await vi.waitFor(() => expect(t.pendingCount).toBe(0));
    expect(t.abandoned).toBe(2);
    expect(tracks).toHaveLength(0);
  });
});
