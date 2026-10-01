import { describe, it, expect } from 'vitest';
import { buyFillPrice, sellProceedsSol } from '../src/positions/ammImpact.ts';

describe('ammImpact', () => {
  it('prices buy impact as 1 + S/Q', () => {
    expect(buyFillPrice(100, 1, 99)).toBeCloseTo(100 * (1 + 1 / 99), 12);
    expect(buyFillPrice(100, 0.04, 70)).toBeCloseTo(100 * (1 + 0.04 / 70), 12);
  });

  it('caps sell proceeds below the pool reserves for any value', () => {
    for (const value of [0.001, 0.04, 2.97, 100, 10_000]) {
      expect(sellProceedsSol(value, 0.28)).toBeLessThan(0.28);
    }
    expect(sellProceedsSol(0, 10)).toBe(0);
  });

  it('reproduces the CCPob dust-pool artifact: 0.04 SOL into 0.0325 SOL pays ~2.23x', () => {
    expect(buyFillPrice(1, 0.04, 0.0325)).toBeCloseTo(1 + 0.04 / 0.0325, 9);
  });

  it('is negligible on a 70 SOL pool (~0.06%)', () => {
    expect(buyFillPrice(1, 0.04, 70) - 1).toBeLessThan(0.001);
  });
});
