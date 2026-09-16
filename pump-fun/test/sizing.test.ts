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

describe('1 SOL live pilot ladder (config.yaml 2026-09-16)', () => {
  // Same knobs as config.yaml: 5 / 10 / 12 %, dust floor 0.03.
  const cfg = ConfigSchema.parse({
    entry: { minSizeWalletPct: 5, baseSizeWalletPct: 10, maxSizeWalletPct: 12, minAbsoluteSol: 0.03 },
    guardrails: { relaxedRiskMaxSizeWalletPct: 5 },
  });

  it('spreads sizes across 0.05–0.12 SOL so momentum sizing is no longer flat', () => {
    // The 7-day dry run ran min=base=max=5%, so every trade sat at the same
    // size and momentumSizeFloorMultiplier (0.4) could never bite.
    expect(computeEntrySizeSol(cfg, 1, 1, 1, false)).toBeCloseTo(0.1, 9);
    expect(computeEntrySizeSol(cfg, 1, 1.25, 1, false)).toBeCloseTo(0.12, 9); // capped at max
    expect(computeEntrySizeSol(cfg, 1, 1, 0.4, false)).toBeCloseTo(0.05, 9); // floored at min
    expect(computeEntrySizeSol(cfg, 1, 1, 0.7, false)).toBeCloseTo(0.07, 9); // in between
    expect(computeEntrySizeSol(cfg, 1, 1, 1, true)).toBeCloseTo(0.05, 9); // relaxed cap
  });

  it('keeps every rung fee-efficient (round-trip < 3% of size)', () => {
    const roundTrip = 0.0002 * 2 + 0.0025 * 2 * 0.05; // priority ×2 + swap both legs on the min rung
    expect(roundTrip / 0.05).toBeLessThan(0.03);
  });
});
