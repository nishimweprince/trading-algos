import { describe, expect, it } from 'vitest';
import { baseReserveWhole, buyImpactSol, estimatePaperFees, sellImpactSol } from '../src/positions/paperFees.ts';

const SOL = 10n ** 9n;

describe('estimatePaperFees', () => {
  it('charges priority per tx and swap fee on both legs', () => {
    // entry + 1 exit = 2 txs
    expect(estimatePaperFees(0.1, 1, { swapFeePct: 0.25, estPriorityTipSolPerTx: 0.0002 })).toBeCloseTo(
      0.0004 + 0.0005,
      9,
    );
  });
  it('adds the Jito tip only when configured', () => {
    const noJito = estimatePaperFees(0.1, 1, { swapFeePct: 0.25, estPriorityTipSolPerTx: 0.0002 });
    const jito = estimatePaperFees(0.1, 1, { swapFeePct: 0.25, estPriorityTipSolPerTx: 0.0002, jitoTipSolPerTx: 0.0001 });
    expect(jito - noJito).toBeCloseTo(0.0002, 9);
  });
});

describe('constant-product impact', () => {
  it('buy impact is s·s/(Y+s): 0.1 SOL into a 25 SOL pool costs ~0.04% of size', () => {
    const impact = buyImpactSol(0.1, 25n * SOL);
    expect(impact).toBeCloseTo(0.1 * (0.1 / 25.1), 9);
    expect(impact / 0.1).toBeLessThan(0.005);
  });
  it('buy impact is zero without reserves (injected test ticks)', () => {
    expect(buyImpactSol(0.1, 0n)).toBe(0);
  });
  it('sell impact fraction is dx/(X+dx) of the fill value', () => {
    // pool holds 1e6 tokens, we sell 1e4 (1%): impact ~0.99% of the fill
    const impact = sellImpactSol(0.5, 1e4, 1e6);
    expect(impact).toBeCloseTo(0.5 * (1e4 / (1e6 + 1e4)), 9);
  });
  it('sell impact is zero for an unknown reserve', () => {
    expect(sellImpactSol(0.5, 1e4, 0)).toBe(0);
    expect(baseReserveWhole(0n, 6)).toBe(0);
    expect(baseReserveWhole(10n ** 15n, 6)).toBe(1e9);
  });
});
