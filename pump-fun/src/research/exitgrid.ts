/**
 * Exit-parameter search, walk-forward only (work plan 2026-09-25 P3.5).
 *
 * F8: the ±15 % barriers were picked from ONE in-sample replay (predicted
 * WR 71 %, realized 43.7 %). Here every configuration is scored on tick paths
 * through the honest simulator (latency, worst-in-window stops, real fees),
 * the best one is chosen on training days only and evaluated on the next
 * day, and the whole search is charged for its size (DSR, PBO).
 */
import { tripleBarrier, type BarrierSpec, type CostSpec, type PathTick } from './labels.ts';
import { bootstrapCI, deflatedSharpe, mean, pboCscv, profitFactor, sharpe, variance } from './stats.ts';

export interface PathSample {
  mint: string;
  arm: string;
  t: number;
  path: PathTick[];
}

export interface GridConfig {
  name: string;
  spec: BarrierSpec;
}

export function defaultGrid(): GridConfig[] {
  const out: GridConfig[] = [];
  for (const [tp, sl] of [[15, 15], [20, 15], [25, 12], [30, 20]] as const) {
    for (const ts of [60_000, 180_000, 600_000]) {
      out.push({ name: `fixed tp${tp}/sl${sl}/t${ts / 1000}s`, spec: { mode: 'fixed', tpPct: tp, slPct: sl, timeStopMs: ts } });
    }
  }
  for (const k1 of [1, 1.5, 2, 3]) {
    for (const k2 of [1, 1.5, 2]) {
      for (const ts of [60_000, 180_000, 600_000]) {
        out.push({
          name: `vol k1=${k1}/k2=${k2}/t${ts / 1000}s`,
          spec: { mode: 'volatility', tpPct: 15, slPct: 15, k1, k2, minTpPct: 8, maxTpPct: 40, minSlPct: 8, maxSlPct: 25, volLookbackMs: 3_000, timeStopMs: ts },
        });
      }
    }
  }
  return out;
}

export interface GridResult {
  configs: number;
  samples: number;
  days: number;
  inSampleBest: { name: string; meanNet: number } | null;
  walkForward: {
    folds: number;
    trades: number;
    meanNet: number;
    ci: { lo: number; hi: number };
    profitFactor: number;
    picks: Array<{ day: string; name: string }>;
  } | null;
  dsr: number;
  pbo: number;
  table: Array<{ name: string; n: number; meanNet: number; winRate: number }>;
}

export function runExitGrid(samples: readonly PathSample[], grid: readonly GridConfig[], cost: CostSpec, seed: number): GridResult {
  // returns[i][k] = net return of sample i under config k (NaN when unlabelable).
  const returns = samples.map((s) => grid.map((g) => tripleBarrier(s.path, g.spec, cost)?.netReturn ?? NaN));
  const table = grid.map((g, k) => {
    const r = returns.map((row) => row[k]!).filter(Number.isFinite);
    return { name: g.name, n: r.length, meanNet: r.length ? mean(r) : NaN, winRate: r.length ? r.filter((x) => x > 0).length / r.length : NaN };
  });
  const bestIdx = table.reduce((b, row, k) => (Number.isFinite(row.meanNet) && (b < 0 || row.meanNet > table[b]!.meanNet) ? k : b), -1);

  const day = (t: number) => Math.floor(t / 86_400_000);
  const days = [...new Set(samples.map((s) => day(s.t)))].sort((a, b) => a - b);
  const oos: number[] = [];
  const picks: Array<{ day: string; name: string }> = [];
  for (let d = 1; d < days.length; d++) {
    const train = samples.map((s, i) => i).filter((i) => day(samples[i]!.t) < days[d]!);
    const test = samples.map((s, i) => i).filter((i) => day(samples[i]!.t) === days[d]!);
    let pick = -1;
    let pickMean = -Infinity;
    grid.forEach((_, k) => {
      const r = train.map((i) => returns[i]![k]!).filter(Number.isFinite);
      if (r.length >= 10 && mean(r) > pickMean) {
        pickMean = mean(r);
        pick = k;
      }
    });
    if (pick < 0) continue;
    picks.push({ day: new Date(days[d]! * 86_400_000).toISOString().slice(0, 10), name: grid[pick]!.name });
    for (const i of test) if (Number.isFinite(returns[i]![pick]!)) oos.push(returns[i]![pick]!);
  }
  const srs = table.map((_, k) => sharpe(returns.map((r) => r[k]!).filter(Number.isFinite))).filter(Number.isFinite);
  const matrix = returns.map((row) => row.map((v) => (Number.isFinite(v) ? v : 0)));
  const ci = oos.length ? bootstrapCI(oos, { seed, iterations: 5_000 }) : { lo: NaN, hi: NaN };
  return {
    configs: grid.length,
    samples: samples.length,
    days: days.length,
    inSampleBest: bestIdx >= 0 ? { name: grid[bestIdx]!.name, meanNet: table[bestIdx]!.meanNet } : null,
    walkForward: picks.length
      ? { folds: picks.length, trades: oos.length, meanNet: mean(oos), ci: { lo: ci.lo, hi: ci.hi }, profitFactor: profitFactor(oos), picks }
      : null,
    dsr: oos.length > 2 ? deflatedSharpe(oos, grid.length, srs.length > 1 ? variance(srs) : 0).dsr : NaN,
    pbo: samples.length >= 16 ? pboCscv(matrix, 8).pbo : NaN,
    table,
  };
}
