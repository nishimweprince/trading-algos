import { describe, it, expect } from 'vitest';
import {
  screenPregrad,
  curveCompletionPct,
  CURVE_INITIAL_REAL_TOKENS,
  CURVE_STANDARD_SUPPLY,
  type PregradScreenInput,
  type PregradScreenConfig,
} from '../src/guardrails/pregrad.ts';

const CFG: PregradScreenConfig = {
  minCompletionPct: 80,
  minRealSol: 50,
  maxTop10Pct: 45,
  maxCreatorPct: 8,
  allowUnindexed: false,
};

function lateCurve(overrides: Partial<PregradScreenInput> = {}): PregradScreenInput {
  return {
    mint: 'm',
    priced: true,
    complete: false,
    tokenTotalSupply: CURVE_STANDARD_SUPPLY,
    // 90% sold off.
    realTokenReserves: (CURVE_INITIAL_REAL_TOKENS * 10n) / 100n,
    realSol: 70,
    top10Share: 30,
    creatorShare: 5,
    mintIndexed: true,
    ...overrides,
  };
}

describe('curveCompletionPct', () => {
  it('maps fresh → 0% and drained → 100%, clamping over/under', () => {
    expect(curveCompletionPct(CURVE_INITIAL_REAL_TOKENS)).toBe(0);
    expect(curveCompletionPct(0n)).toBe(100);
    expect(curveCompletionPct((CURVE_INITIAL_REAL_TOKENS * 10n) / 100n)).toBeCloseTo(90, 9);
    expect(curveCompletionPct(null)).toBeNull();
  });
});

describe('screenPregrad', () => {
  it('accepts an indexed late curve with no unknowns', () => {
    const v = screenPregrad(lateCurve(), CFG);
    expect(v.verdict).toBe('accept');
    expect(v.reasons).toEqual([]);
    expect(v.unknowns).toEqual([]);
    expect(v.completionPct).toBeCloseTo(90, 9);
    expect(v.relaxed).toBe(false);
  });

  it('vetoes complete, unpriced, early, floored, and nonstandard-supply curves', () => {
    expect(screenPregrad(lateCurve({ complete: true }), CFG).reasons).toContain('CURVE_COMPLETE');
    expect(screenPregrad(lateCurve({ priced: false }), CFG).reasons).toContain('CURVE_UNPRICED');
    expect(
      screenPregrad(lateCurve({ realTokenReserves: CURVE_INITIAL_REAL_TOKENS, realSol: 1 }), CFG).reasons,
    ).toContain('CURVE_EARLY');
    expect(screenPregrad(lateCurve({ realSol: 10 }), CFG).reasons).toContain('CURVE_FLOOR');
    expect(screenPregrad(lateCurve({ tokenTotalSupply: 1n }), CFG).reasons).toContain('CURVE_SUPPLY');
  });

  it('vetoes concentration breaches but records (never vetoes) unknowns', () => {
    expect(screenPregrad(lateCurve({ top10Share: 80 }), CFG).reasons).toContain('TOP10');
    expect(screenPregrad(lateCurve({ creatorShare: 50 }), CFG).reasons).toContain('CREATOR');
    const v = screenPregrad(lateCurve({ top10Share: null, creatorShare: null, realSol: null }), CFG);
    expect(v.verdict).toBe('accept');
    expect(v.unknowns).toEqual(expect.arrayContaining(['top10', 'creator', 'realSol']));
  });

  it('vetoes unindexed mints unless allowed, tagging blind accepts relaxed', () => {
    expect(screenPregrad(lateCurve({ mintIndexed: false }), CFG).reasons).toContain('UNINDEXED_MINT');
    const v = screenPregrad(lateCurve({ mintIndexed: false }), { ...CFG, allowUnindexed: true });
    expect(v.verdict).toBe('accept');
    expect(v.relaxed).toBe(true);
  });
});
