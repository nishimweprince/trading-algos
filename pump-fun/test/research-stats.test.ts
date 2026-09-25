import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import {
  bootstrapCI,
  breakevenWinRate,
  deflatedSharpe,
  expectedMaxSharpe,
  maxDrawdown,
  mulberry32,
  normCdf,
  normInv,
  pboCscv,
  probabilisticSharpe,
  profitFactor,
  quantile,
  wilson,
} from '../src/research/stats.ts';
import { parseCsv } from '../src/research/csv.ts';
import { feeBpsForMcap, mcapFromPrice, tierForMcap } from '../src/positions/feeTiers.ts';
import { loadTrades, recostTrade, type Trade } from '../src/research/trades.ts';
import { recostTotals } from '../src/research/recost.ts';

describe('research stats', () => {
  it('bootstrap CI is deterministic per seed and brackets the mean', () => {
    const rng = mulberry32(7);
    const xs = Array.from({ length: 300 }, () => rng() * 2 - 0.9);
    const a = bootstrapCI(xs, { seed: 1, iterations: 2000 });
    const b = bootstrapCI(xs, { seed: 1, iterations: 2000 });
    const c = bootstrapCI(xs, { seed: 2, iterations: 2000 });
    expect(a).toEqual(b);
    expect(a).not.toEqual(c);
    expect(a.lo).toBeLessThan(a.point);
    expect(a.hi).toBeGreaterThan(a.point);
  });

  it('quantile matches numpy type-7', () => {
    expect(quantile([1, 2, 3, 4], 0.5)).toBe(2.5);
    expect(quantile([1, 2, 3, 4], 0.25)).toBe(1.75);
  });

  it('wilson interval matches the textbook value', () => {
    // 45/100 at 95 %: [0.3561, 0.5476]
    const w = wilson(45, 100);
    expect(w.lo).toBeCloseTo(0.3561, 3);
    expect(w.hi).toBeCloseTo(0.5476, 3);
  });

  it('normal CDF / inverse round-trip', () => {
    for (const p of [0.001, 0.025, 0.3, 0.5, 0.9, 0.975, 0.999]) {
      expect(normCdf(normInv(p))).toBeCloseTo(p, 6);
    }
    expect(normInv(0.975)).toBeCloseTo(1.959964, 5);
  });

  it('breakeven WR, profit factor, drawdown', () => {
    // The review's numbers: avg win +17.2 %, avg loss -22.0 % -> 56.1 %.
    expect(breakevenWinRate(17.2, 22.0)).toBeCloseTo(0.5612, 3);
    expect(profitFactor([2, -1, 1, -1])).toBe(1.5);
    expect(maxDrawdown([1, -2, -1, 3, -4])).toBe(4);
  });

  it('PSR: positive Sharpe on long samples is significant, zero Sharpe is a coin flip', () => {
    expect(probabilisticSharpe(0.2, 500, 0, 3)).toBeGreaterThan(0.99);
    expect(probabilisticSharpe(0, 500, 0, 3)).toBeCloseTo(0.5, 6);
  });

  it('DSR deflates for the number of trials', () => {
    const rng = mulberry32(3);
    const xs = Array.from({ length: 400 }, () => 0.08 + (rng() - 0.5));
    const one = deflatedSharpe(xs, 1, 0.01);
    const many = deflatedSharpe(xs, 200, 0.01);
    expect(expectedMaxSharpe(200, 0.01)).toBeGreaterThan(0);
    expect(many.dsr).toBeLessThan(one.dsr);
  });

  it('PBO ~ 0 when one config truly dominates, high when configs are pure noise', () => {
    const rng = mulberry32(11);
    const T = 400;
    const K = 10;
    const real = Array.from({ length: T }, () => Array.from({ length: K }, (_, k) => (k === 3 ? 0.5 : 0) + (rng() - 0.5)));
    expect(pboCscv(real, 8).pbo).toBeLessThan(0.1);
    const noise = Array.from({ length: T }, () => Array.from({ length: K }, () => rng() - 0.5));
    const r = pboCscv(noise, 8);
    expect(r.combinations).toBe(70);
    expect(r.pbo).toBeGreaterThan(0.25);
  });

  it('csv parser handles quoted JSON cells', () => {
    const rows = parseCsv('a,b,c\n1,"[""x"",""y""]",3\n');
    expect(rows[0]).toEqual({ a: '1', b: '["x","y"]', c: '3' });
  });
});

describe('PumpSwap fee tiers (F4)', () => {
  it('selects tiers by mcap like the SDK calculateFeeTier', () => {
    expect(feeBpsForMcap(0)).toBe(125);
    expect(feeBpsForMcap(380)).toBe(125);
    expect(feeBpsForMcap(420)).toBe(120);
    expect(feeBpsForMcap(1_469.9)).toBe(120);
    expect(feeBpsForMcap(1_470)).toBe(115);
    expect(feeBpsForMcap(98_240)).toBe(30);
    expect(feeBpsForMcap(1e9)).toBe(30);
    expect(feeBpsForMcap(null)).toBe(125); // unknown -> most expensive (graduation) tier
    expect(tierForMcap(380).protocolBps).toBe(93);
  });

  it('mcap from price uses the 1e9 pump supply', () => {
    expect(mcapFromPrice(3.8e-7)).toBeCloseTo(380, 9);
    expect(mcapFromPrice(0)).toBeNull();
  });
});

describe('re-cost', () => {
  const base: Trade = {
    mint: 'Xpump', exitReason: 'STOP_LOSS', sizeSol: 0.03, entryPrice: 3.8e-7, exitPrice: 3.23e-7,
    grossPnlSol: -0.0045, feesSol: 0.03 * 0.005 + 0.0004 + 0.0001, netPnlSol: 0, slippageSol: 0.0001,
    holdMs: 4000, score: 85, relaxed: false, pumpSuffix: true, mfePct: 1, maePct: -15, openedAt: '', mode: 'dry-run',
    mcapSolAtEntry: null, feeTierBps: null,
  };

  it('0.03 SOL round trip at 380 SOL mcap costs 2 x 1.25 % (on each leg notional) + tx costs', () => {
    const t = { ...base, grossPnlSol: 0, exitPrice: base.entryPrice };
    const r = recostTrade(t, { loggedSwapFeePct: 0.25 });
    expect(r.entryBps).toBe(125);
    expect(r.exitBps).toBe(125);
    expect(r.txCostsSol).toBeCloseTo(0.0004, 12);
    expect(r.swapFeesSol).toBeCloseTo(2 * 0.0125 * 0.03, 12);
    expect(r.feesSol).toBeCloseTo(2 * 0.0125 * 0.03 + 0.0004 + 0.0001, 12);
  });

  it('charges the exit leg on proceeds, at the exit tier', () => {
    const t = { ...base, grossPnlSol: 0.03, exitPrice: 7.6e-7 }; // doubled -> 760 SOL mcap
    const r = recostTrade(t, { loggedSwapFeePct: 0.25 });
    expect(r.exitBps).toBe(120);
    expect(r.swapFeesSol).toBeCloseTo(0.0125 * 0.03 + 0.012 * 0.06, 12);
  });

  const csvPath = 'reports/baseline-2026-09-25/trades-live-7d.csv';
  it.runIf(existsSync(csvPath))('baseline week re-costs to ~ -0.62 SOL (work plan P1.1 acceptance)', () => {
    const trades = loadTrades(readFileSync(csvPath, 'utf8'));
    const t = recostTotals(trades, { seed: 1, loggedSwapFeePct: 0.25, iterations: 500 });
    expect(t.n).toBe(524);
    expect(t.loggedNetSol).toBeCloseTo(-0.348, 3);
    expect(t.recostNetSol).toBeGreaterThan(-0.63);
    expect(t.recostNetSol).toBeLessThan(-0.61);
    expect(t.grossPctMean).toBeCloseTo(-1.15, 2);
  });
});
