import { describe, it, expect } from 'vitest';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { realizedVolPct, tripleBarrier, type PathTick } from '../src/research/labels.ts';
import { fitLogReg, predictProba, contributions } from '../src/research/logreg.ts';
import { purgedKFold, walkForwardByDay } from '../src/research/validate.ts';
import { FEATURE_NAMES, toFeatureVector } from '../src/research/featureSpec.ts';
import { trainMetaModel } from '../src/research/train.ts';
import type { Sample } from '../src/research/dataset.ts';
import { MetaModel, sizeFactorForProb } from '../src/guardrails/model.ts';
import { gateP2, gateP3, gateP4 } from '../src/research/gate.ts';
import { defaultGrid, runExitGrid } from '../src/research/exitgrid.ts';
import { mulberry32 } from '../src/research/stats.ts';
import { AdaptiveExit } from '../src/exits/adaptive.ts';
import { PaperPosition } from '../src/positions/position.ts';
import { ConfigSchema } from '../src/config/schema.ts';

const COST = { exitLatencyMs: 1_000, sizeSol: 0.04, txCostSol: 0.0002 };
const ENTRY = 3.8e-7; // 380 SOL mcap -> 125 bps tier
const path = (pts: Array<[number, number]>): PathTick[] => pts.map(([tMs, m]) => ({ tMs, price: ENTRY * m }));

describe('triple barrier (P3.4)', () => {
  it('stops fill at the worst price within the exit latency, net of real fees', () => {
    const p = path([[0, 1], [2_000, 0.84], [2_500, 0.7], [3_500, 0.9]]);
    const r = tripleBarrier(p, { mode: 'fixed', tpPct: 15, slPct: 15, timeStopMs: 600_000 }, COST)!;
    expect(r.barrier).toBe('lower');
    expect(r.grossReturn).toBeCloseTo(-0.3, 9); // worst in [2.0 s, 3.0 s] = 0.70
    expect(r.netReturn).toBeLessThan(r.grossReturn - 0.0125);
    expect(r.label).toBe(0);
  });

  it('take-profit fills at the price when the confirm lands', () => {
    const p = path([[0, 1], [1_000, 1.16], [1_800, 1.2], [2_200, 1.05]]);
    const r = tripleBarrier(p, { mode: 'fixed', tpPct: 15, slPct: 15, timeStopMs: 600_000 }, COST)!;
    expect(r.barrier).toBe('upper');
    expect(r.grossReturn).toBeCloseTo(0.2, 9);
    expect(r.label).toBe(1);
  });

  it('a +2 % scratch is a LOSS after 2 x 1.25 % fees', () => {
    const p = path([[0, 1], [700_000, 1.02]]);
    const r = tripleBarrier(p, { mode: 'fixed', tpPct: 15, slPct: 15, timeStopMs: 600_000 }, COST)!;
    expect(r.barrier).toBe('vertical');
    expect(r.label).toBe(0);
  });

  it('volatility barriers scale with realized vol and clip', () => {
    const p = path([[0, 1], [1_000, 1.05], [2_000, 0.97], [3_000, 1.04], [4_000, 1]]);
    expect(realizedVolPct(p, 3_000)).toBeGreaterThan(5);
    const r = tripleBarrier(p, { mode: 'volatility', tpPct: 15, slPct: 15, k1: 100, k2: 0.01, minTpPct: 8, maxTpPct: 40, minSlPct: 8, maxSlPct: 25, volLookbackMs: 3_000, timeStopMs: 600_000 }, COST)!;
    expect(r.tpPct).toBe(40);
    expect(r.slPct).toBe(8);
  });
});

describe('validation splits', () => {
  const samples = Array.from({ length: 100 }, (_, i) => ({ t: i * 60_000, tEnd: i * 60_000 + 5 * 60_000 }));
  it('purges overlapping labels and embargoes after the test fold', () => {
    const folds = purgedKFold(samples, 5, 10 * 60_000);
    expect(folds).toHaveLength(5);
    const f = folds[1]!;
    const tStart = Math.min(...f.test.map((i) => samples[i]!.t));
    const tStop = Math.max(...f.test.map((i) => samples[i]!.tEnd));
    for (const i of f.train) {
      const s = samples[i]!;
      expect(s.tEnd < tStart || s.t > tStop + 10 * 60_000).toBe(true);
    }
  });
  it('walk-forward trains strictly on earlier days', () => {
    const days = Array.from({ length: 30 }, (_, i) => ({ t: Math.floor(i / 10) * 86_400_000 + 1_000, tEnd: Math.floor(i / 10) * 86_400_000 + 2_000 }));
    const folds = walkForwardByDay(days);
    expect(folds).toHaveLength(2);
    for (const f of folds) expect(Math.max(...f.train.map((i) => days[i]!.t))).toBeLessThan(Math.min(...f.test.map((i) => days[i]!.t)));
  });
});

describe('feature spec + logistic regression', () => {
  it('encodes values with missing indicators in a stable order', () => {
    const v = toFeatureVector({ earlyFlowNetSol: 1.5, featuresJson: JSON.stringify({ manipulation: { copycat: { isCopycat: true } } }) });
    expect(v).toHaveLength(FEATURE_NAMES.length);
    expect(v[FEATURE_NAMES.indexOf('early_flow_net_sol')]).toBe(1.5);
    expect(v[FEATURE_NAMES.indexOf('early_flow_net_sol__missing')]).toBe(0);
    expect(v[FEATURE_NAMES.indexOf('pool_sol__missing')]).toBe(1);
    expect(v[FEATURE_NAMES.indexOf('copycat')]).toBe(1);
  });

  it('learns a separable signal and attributes it', () => {
    const rng = mulberry32(4);
    const X = Array.from({ length: 400 }, () => [rng() * 2 - 1, rng() * 2 - 1]);
    const y = X.map(([a]) => (a! > 0 ? 1 : 0));
    const m = fitLogReg(X, y, { lambda: 0.001, featureNames: ['signal', 'noise'], iterations: 800 });
    expect(predictProba(m, [0.8, 0])).toBeGreaterThan(0.8);
    expect(predictProba(m, [-0.8, 0])).toBeLessThan(0.2);
    expect(contributions(m, [0.8, 0.1])[0]!.feature).toBe('signal');
  });
});

function syntheticSamples(n: number, signal: boolean): Sample[] {
  const rng = mulberry32(signal ? 21 : 22);
  const idx = FEATURE_NAMES.indexOf('early_flow_net_sol');
  return Array.from({ length: n }, (_, i) => {
    const flow = rng() * 4 - 2;
    const x = toFeatureVector({ earlyFlowNetSol: flow, poolSolAtEntry: 80 });
    x[idx] = flow;
    const good = signal ? flow + (rng() - 0.5) > 0.3 : rng() > 0.55;
    const t = Date.UTC(2026, 8, 1) + Math.floor(i / 60) * 86_400_000 + (i % 60) * 60_000;
    return { mint: `m${i}`, arm: 'veto', t, tEnd: t + 60_000, x, label: good ? 1 : 0, netReturn: good ? 0.12 : -0.15, barrier: good ? 'upper' : 'lower' } as Sample;
  });
}

describe('meta-model training (purged OOS, DSR, PBO)', () => {
  const opts = { lambdas: [0.01, 0.1], thresholds: [0.5, 0.6, 0.7], folds: 5, embargoMs: 60 * 60_000, seed: 1, minTaken: 30 };

  it('finds a real signal out of sample', () => {
    const r = trainMetaModel(syntheticSamples(360, true), opts);
    expect(r.chosen).not.toBeNull();
    expect(r.oos!.meanNet).toBeGreaterThan(r.baselineMeanNet);
    expect(r.oos!.ci.lo).toBeGreaterThan(0);
    expect(r.model!.featureNames).toEqual([...FEATURE_NAMES]);
  });

  it('does not certify noise', () => {
    const r = trainMetaModel(syntheticSamples(360, false), opts);
    expect(!r.oos || r.oos.ci.lo <= 0 || r.oos.dsr < 0.95).toBe(true);
  });

  it('round-trips through the inference loader and rejects a mismatched spec', () => {
    const r = trainMetaModel(syntheticSamples(360, true), opts);
    const dir = mkdtempSync(join(tmpdir(), 'meta-'));
    const good = join(dir, 'm.json');
    writeFileSync(good, JSON.stringify({ version: 'test', createdAt: '', ...r.model }));
    const m = MetaModel.load(good)!;
    expect(m.score({ earlyFlowNetSol: 1.8, poolSolAtEntry: 80 }).prob).toBeGreaterThan(m.score({ earlyFlowNetSol: -1.8, poolSolAtEntry: 80 }).prob);
    const bad = join(dir, 'bad.json');
    writeFileSync(bad, JSON.stringify({ version: 'x', createdAt: '', ...r.model, featureNames: ['a'] }));
    expect(MetaModel.load(bad)).toBeNull();
    expect(MetaModel.load(join(dir, 'missing.json'))).toBeNull();
    expect(sizeFactorForProb(0.9, 0.6)).toBe(1.25);
    expect(sizeFactorForProb(0.2, 0.6)).toBe(0.5);
  });
});

describe('gates (P3.6)', () => {
  const t = (net: number, over: Partial<{ mint: string; exitReason: string; relaxed: boolean; feesSol: number }> = {}) => ({
    mint: over.mint ?? 'Xpump', sizeSol: 0.04, netPnlSol: net, feesSol: over.feesSol ?? 0.001, exitReason: over.exitReason ?? 'STOP_LOSS', relaxed: over.relaxed ?? false,
  });
  it('P2 fails on relaxed / non-pump trades and emergency-heavy losses', () => {
    const g = gateP2([t(-0.01, { relaxed: true }), t(-0.02, { exitReason: 'EMERGENCY_EXIT' }), t(0.005, { mint: 'Y' })]);
    expect(g.met).toBe(false);
    expect(g.criteria.filter((c) => !c.pass).map((c) => c.name)).toEqual([
      'Relaxed-risk trades', 'Non-segment-A (non-`pump`) trades', 'Emergency-exit share of net loss',
    ]);
  });
  it('P3 passes only a large, robustly positive OOS sample', () => {
    const rng = mulberry32(3);
    const good = Array.from({ length: 400 }, () => t(0.04 * (rng() < 0.62 ? 0.2 : -0.12)));
    expect(gateP3(good, { capitalSol: 5, trials: 1, seed: 1 }).met).toBe(true);
    expect(gateP3(good.slice(0, 100), { capitalSol: 5, trials: 1, seed: 1 }).met).toBe(false);
  });
  it('P4 needs drag data', () => {
    const g = gateP4([t(0.01)], { failedEntries: 0, attemptedEntries: 1, dragPctPerTrade: null, minTrades: 1 });
    expect(g.criteria.find((c) => c.name.startsWith('Live − twin'))!.pass).toBe(false);
  });
});

describe('exit grid (P3.5)', () => {
  it('scores every config and reports walk-forward picks across days', () => {
    const rng = mulberry32(8);
    const samples = Array.from({ length: 60 }, (_, i) => {
      const pts: Array<[number, number]> = [[0, 1]];
      let m = 1;
      for (let s = 1; s <= 120; s++) {
        m *= 1 + (rng() - 0.5) * 0.06;
        pts.push([s * 1_000, m]);
      }
      return { mint: `m${i}`, arm: 'veto', t: Date.UTC(2026, 8, 1 + Math.floor(i / 20)), path: path(pts) };
    });
    const r = runExitGrid(samples, defaultGrid(), COST, 1);
    expect(r.configs).toBe(defaultGrid().length);
    expect(r.days).toBe(3);
    expect(r.walkForward?.folds).toBe(2);
    expect(r.table.every((row) => row.n === 60)).toBe(true);
  });
});

describe('adaptive exits (P3.5)', () => {
  it('re-sets TP1 / stop once after the lookback, never after a take-profit', () => {
    const cfg = ConfigSchema.parse({ exits: { mode: 'volatility', vol: { k1: 3, k2: 2, lookbackMs: 3_000 } } }).exits;
    const a = new AdaptiveExit(cfg, 0, 1);
    expect(a.observe(1.04, 1_000)).toBeNull();
    expect(a.observe(0.98, 2_000)).toBeNull();
    const b = a.observe(1.03, 3_000)!;
    expect(b.sigmaPct).toBeGreaterThan(0);
    expect(b.tpPct).toBeGreaterThanOrEqual(cfg.vol.minTpPct);
    expect(a.observe(1.1, 4_000)).toBeNull(); // once

    const pos = new PaperPosition({ mint: 'M', sizeSol: 1, entryPrice: 1, openedAtMs: 0, highVolatility: false, cfg });
    expect(pos.retune(12, 9)).toBe(true);
    expect(pos.onPrice(0.915, 5_000)).toEqual([]); // above the retuned 9 % stop (0.91)
    expect(pos.onPrice(0.9, 6_000)[0]?.trigger).toBe('STOP_LOSS');
  });
});
