import { describe, it, expect } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { computeEntrySizeSol, entrySizeLadder, sizeFromWalletPct } from '../src/config/sizing.ts';

describe('percent-of-wallet sizing', () => {
  const cfg = ConfigSchema.parse({
    entry: { minSizeWalletPct: 5, baseSizeWalletPct: 8, maxSizeWalletPct: 10, minAbsoluteSol: 0.01 },
    guardrails: { relaxedRiskMaxSizeWalletPct: 3 },
  });

  it('floors a zero wallet at minAbsoluteSol', () => {
    expect(sizeFromWalletPct(0, 8, 0.01)).toBe(0.01);
    const ladder = entrySizeLadder(cfg, 0);
    expect(ladder.baseSol).toBe(0.01);
    expect(ladder.minSol).toBe(0.01);
  });

  it('scales the current 0.328 SOL wallet to the pilot rungs', () => {
    const ladder = entrySizeLadder(cfg, 0.328);
    expect(ladder.minSol).toBeCloseTo(0.0164, 4);
    expect(ladder.baseSol).toBeCloseTo(0.02624, 4);
    expect(ladder.maxSol).toBeCloseTo(0.0328, 4);
    expect(ladder.relaxedMaxSol).toBeCloseTo(0.01, 4); // 3% of 0.328 = 0.00984 → dust floor
  });

  it('clamps score/momentum size into [min, max]', () => {
    const full = computeEntrySizeSol(cfg, 0.328, 1, 1, false);
    expect(full).toBeCloseTo(0.02624, 4);
    const weak = computeEntrySizeSol(cfg, 0.328, 0.4, 0.4, false);
    expect(weak).toBeCloseTo(0.0164, 4); // floored to min 5%
    const pumped = computeEntrySizeSol(cfg, 0.328, 1.25, 1, false);
    expect(pumped).toBeCloseTo(0.0328, 4);
  });

  it('caps relaxed-risk below the min rung', () => {
    const size = computeEntrySizeSol(cfg, 0.328, 1, 1, true);
    expect(size).toBeCloseTo(0.01, 4);
  });
});
