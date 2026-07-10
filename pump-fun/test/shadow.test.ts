import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { getShadowVetoQuality } from '../src/dashboard/queries.ts';
import { ShadowTracker } from '../src/guardrails/shadow.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { PoolRef } from '../src/positions/pricing.ts';

// PricePoller only touches rpc while started; these tests never start(), so a
// stub that is never called is sufficient.
const stubRpc = { getMultipleAccountsBase64: async () => [] } as unknown as RpcClient;

function poolRef(mint: string): PoolRef {
  return { mint, baseVault: `${mint}-base`, quoteVault: `${mint}-quote`, baseDecimals: 6 };
}

describe('shadow veto-quality aggregation', () => {
  it('groups counterfactual outcomes by primary veto reason', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const base = {
      baselinePrice: 1,
      troughPrice: 0.9,
      maxMaePct: -10,
      samples: 5,
      trackedMs: 1200,
    };
    // Two H7 vetoes: one pumped +60% (would-be false positive), one flat.
    repos.recordShadowOutcome({
      ...base,
      mint: 'm1',
      verdict: 'veto',
      primaryVetoCode: 'H7',
      peakPrice: 1.6,
      peakMfePct: 60,
      hit25: true,
      hit50: true,
    });
    repos.recordShadowOutcome({
      ...base,
      mint: 'm2',
      verdict: 'veto',
      primaryVetoCode: 'H7',
      peakPrice: 1.05,
      peakMfePct: 5,
      hit25: false,
      hit50: false,
    });
    // One UNKNOWN:H4 veto that did nothing.
    repos.recordShadowOutcome({
      ...base,
      mint: 'm3',
      verdict: 'veto',
      primaryVetoCode: 'UNKNOWN:H4',
      peakPrice: 1.1,
      peakMfePct: 10,
      hit25: false,
      hit50: false,
    });

    const q = getShadowVetoQuality(db, { range: 'all' });
    expect(q.totalTracked).toBe(3);
    const byReason = Object.fromEntries(q.byReason.map((r) => [r.primaryVetoCode, r]));
    expect(byReason['H7']!.n).toBe(2);
    expect(byReason['H7']!.avgPeakMfePct).toBeCloseTo(32.5);
    expect(byReason['H7']!.hit50Pct).toBeCloseTo(50); // 1 of 2 hit +50%
    expect(byReason['UNKNOWN:H4']!.hit50Pct).toBe(0);
    db.close();
  });
});

describe('ShadowTracker registration guards', () => {
  it('dedupes, caps, and ignores unpriceable candidates', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const tracker = new ShadowTracker(stubRpc, repos, { maxConcurrent: 2 });

    const track = (mint: string, baselinePrice: number) =>
      tracker.track({ mint, verdict: 'veto', primaryVetoCode: 'H7', baselinePrice, poolRef: poolRef(mint) });

    track('a', 1);
    track('a', 1); // duplicate — ignored
    expect(tracker.size).toBe(1);

    track('b', 0); // baseline 0 — can't price, ignored
    expect(tracker.size).toBe(1);

    track('c', 2);
    expect(tracker.size).toBe(2);

    track('d', 2); // at capacity (2) — dropped
    expect(tracker.size).toBe(2);

    tracker.stop();
    db.close();
  });
});
