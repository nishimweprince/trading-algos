/**
 * Statistics for edge measurement (work plan 2026-09-25 P1.4 / P3.4 / P3.6).
 *
 * Everything that draws random numbers takes an explicit seed so every report
 * is reproducible bit-for-bit (the P1 gate: "same seed -> same result").
 * Pure functions, no I/O, no dependencies.
 */

/** mulberry32 — small, fast, well-distributed 32-bit PRNG. Returns [0, 1). */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function sum(xs: readonly number[]): number {
  let s = 0;
  for (const x of xs) s += x;
  return s;
}

export function mean(xs: readonly number[]): number {
  return xs.length ? sum(xs) / xs.length : NaN;
}

/** Sample variance (n-1). */
export function variance(xs: readonly number[]): number {
  if (xs.length < 2) return NaN;
  const m = mean(xs);
  let s = 0;
  for (const x of xs) s += (x - m) ** 2;
  return s / (xs.length - 1);
}

export function stdev(xs: readonly number[]): number {
  return Math.sqrt(variance(xs));
}

/** Linear-interpolated quantile (type 7, numpy default). q in [0, 1]. */
export function quantile(xs: readonly number[], q: number): number {
  if (!xs.length) return NaN;
  const s = [...xs].sort((a, b) => a - b);
  const pos = (s.length - 1) * Math.min(1, Math.max(0, q));
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return s[lo]! + (s[hi]! - s[lo]!) * (pos - lo);
}

export function median(xs: readonly number[]): number {
  return quantile(xs, 0.5);
}

/** Third and fourth standardized moments (population, non-excess kurtosis). */
export function skewKurt(xs: readonly number[]): { skew: number; kurt: number } {
  const n = xs.length;
  if (n < 3) return { skew: 0, kurt: 3 };
  const m = mean(xs);
  let m2 = 0;
  let m3 = 0;
  let m4 = 0;
  for (const x of xs) {
    const d = x - m;
    m2 += d * d;
    m3 += d * d * d;
    m4 += d * d * d * d;
  }
  m2 /= n;
  m3 /= n;
  m4 /= n;
  if (m2 === 0) return { skew: 0, kurt: 3 };
  return { skew: m3 / m2 ** 1.5, kurt: m4 / m2 ** 2 };
}

export interface Interval {
  point: number;
  lo: number;
  hi: number;
}

/**
 * Percentile bootstrap CI of `stat` (default: mean). Deterministic for a
 * given seed. `level` is the two-sided confidence (0.95 -> 2.5/97.5 %).
 */
export function bootstrapCI(
  xs: readonly number[],
  opts: { seed: number; iterations?: number; level?: number; stat?: (s: number[]) => number } ,
): Interval {
  const stat = opts.stat ?? mean;
  const iterations = opts.iterations ?? 10_000;
  const level = opts.level ?? 0.95;
  const n = xs.length;
  if (n === 0) return { point: NaN, lo: NaN, hi: NaN };
  const rng = mulberry32(opts.seed);
  const stats = new Array<number>(iterations);
  const buf = new Array<number>(n);
  for (let i = 0; i < iterations; i++) {
    for (let j = 0; j < n; j++) buf[j] = xs[Math.floor(rng() * n)]!;
    stats[i] = stat(buf);
  }
  const a = (1 - level) / 2;
  return { point: stat([...xs]), lo: quantile(stats, a), hi: quantile(stats, 1 - a) };
}

/** Wilson score interval for a binomial proportion. */
export function wilson(successes: number, n: number, z = 1.959963984540054): Interval {
  if (n === 0) return { point: NaN, lo: NaN, hi: NaN };
  const p = successes / n;
  const z2 = z * z;
  const denom = 1 + z2 / n;
  const centre = (p + z2 / (2 * n)) / denom;
  const half = (z * Math.sqrt((p * (1 - p)) / n + z2 / (4 * n * n))) / denom;
  return { point: p, lo: centre - half, hi: centre + half };
}

/** Gross profit / gross loss. Infinity when there are no losses, NaN when empty. */
export function profitFactor(pnls: readonly number[]): number {
  let win = 0;
  let loss = 0;
  for (const p of pnls) {
    if (p > 0) win += p;
    else if (p < 0) loss -= p;
  }
  if (win === 0 && loss === 0) return NaN;
  return loss === 0 ? Infinity : win / loss;
}

/** Largest peak-to-trough fall of the cumulative sum, as a positive number. */
export function maxDrawdown(pnls: readonly number[], startEquity = 0): number {
  let eq = startEquity;
  let peak = startEquity;
  let dd = 0;
  for (const p of pnls) {
    eq += p;
    if (eq > peak) peak = eq;
    if (peak - eq > dd) dd = peak - eq;
  }
  return dd;
}

/** Per-observation Sharpe ratio (no annualisation; mean / sd). */
export function sharpe(xs: readonly number[]): number {
  const sd = stdev(xs);
  return sd > 0 ? mean(xs) / sd : NaN;
}

/**
 * Win rate required to break even given the average win and average loss
 * magnitudes: WR·W = (1-WR)·L  =>  WR = L / (W + L).
 */
export function breakevenWinRate(avgWin: number, avgLossAbs: number): number {
  return avgWin + avgLossAbs > 0 ? avgLossAbs / (avgWin + avgLossAbs) : NaN;
}

// ---------------------------------------------------------------------------
// Normal distribution helpers (for PSR / DSR)
// ---------------------------------------------------------------------------

/** Standard normal CDF (Abramowitz–Stegun 7.1.26 via erf, |err| < 1.5e-7). */
export function normCdf(x: number): number {
  const t = 1 / (1 + 0.3275911 * Math.abs(x) / Math.SQRT2);
  const y =
    1 -
    (((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t) *
      Math.exp(-(x * x) / 2);
  return x >= 0 ? 0.5 * (1 + y) : 0.5 * (1 - y);
}

/** Inverse standard normal CDF (Acklam's rational approximation, rel err < 1.2e-9). */
export function normInv(p: number): number {
  if (p <= 0) return -Infinity;
  if (p >= 1) return Infinity;
  const a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2, 1.38357751867269e2, -3.066479806614716e1, 2.506628277459239];
  const b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1];
  const c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783];
  const d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416];
  const pl = 0.02425;
  if (p < pl) {
    const q = Math.sqrt(-2 * Math.log(p));
    return (((((c[0]! * q + c[1]!) * q + c[2]!) * q + c[3]!) * q + c[4]!) * q + c[5]!) /
      ((((d[0]! * q + d[1]!) * q + d[2]!) * q + d[3]!) * q + 1);
  }
  if (p > 1 - pl) {
    const q = Math.sqrt(-2 * Math.log(1 - p));
    return -(((((c[0]! * q + c[1]!) * q + c[2]!) * q + c[3]!) * q + c[4]!) * q + c[5]!) /
      ((((d[0]! * q + d[1]!) * q + d[2]!) * q + d[3]!) * q + 1);
  }
  const q = p - 0.5;
  const r = q * q;
  return ((((((a[0]! * r + a[1]!) * r + a[2]!) * r + a[3]!) * r + a[4]!) * r + a[5]!) * q) /
    (((((b[0]! * r + b[1]!) * r + b[2]!) * r + b[3]!) * r + b[4]!) * r + 1);
}

/**
 * Probabilistic Sharpe Ratio (Bailey & López de Prado 2012): probability that
 * the true per-observation Sharpe exceeds `benchmarkSr`, correcting for sample
 * length, skew and (non-excess) kurtosis.
 */
export function probabilisticSharpe(
  sr: number,
  n: number,
  skew: number,
  kurt: number,
  benchmarkSr = 0,
): number {
  if (!(n > 1) || !Number.isFinite(sr)) return NaN;
  const denom = Math.sqrt(Math.max(1e-12, 1 - skew * sr + ((kurt - 1) / 4) * sr * sr));
  return normCdf(((sr - benchmarkSr) * Math.sqrt(n - 1)) / denom);
}

/**
 * Expected maximum Sharpe among `trials` independent strategies with true SR 0
 * and cross-trial SR variance `srVariance` (False Strategy Theorem).
 */
export function expectedMaxSharpe(trials: number, srVariance: number): number {
  if (trials <= 1) return 0;
  const g = 0.5772156649015329; // Euler–Mascheroni
  const e = Math.E;
  return Math.sqrt(srVariance) * ((1 - g) * normInv(1 - 1 / trials) + g * normInv(1 - 1 / (trials * e)));
}

/**
 * Deflated Sharpe Ratio (Bailey & López de Prado 2014): the PSR of the
 * selected strategy against the Sharpe you would expect from the best of
 * `trials` pure-noise variants. > 0.95 is the P3 gate.
 */
export function deflatedSharpe(
  returns: readonly number[],
  trials: number,
  srVarianceAcrossTrials: number,
): { sr: number; sr0: number; dsr: number } {
  const sr = sharpe(returns);
  const { skew, kurt } = skewKurt(returns);
  const sr0 = expectedMaxSharpe(trials, srVarianceAcrossTrials);
  return { sr, sr0, dsr: probabilisticSharpe(sr, returns.length, skew, kurt, sr0) };
}

/**
 * Probability of Backtest Overfitting via Combinatorially Symmetric
 * Cross-Validation (Bailey, Borwein, López de Prado, Zhu 2015).
 *
 * `matrix[t][k]` = performance of configuration k in time-block t (rows are
 * chronological observations, already aggregated or raw). Rows are split into
 * `blocks` contiguous groups; for every half/half combination the best IS
 * configuration's OOS rank is recorded. PBO = share of combinations where that
 * rank is at or below the OOS median (logit <= 0).
 */
export function pboCscv(
  matrix: readonly (readonly number[])[],
  blocks = 8,
  metric: (xs: number[]) => number = mean,
): { pbo: number; combinations: number; logits: number[] } {
  const T = matrix.length;
  const K = matrix[0]?.length ?? 0;
  if (K < 2 || T < blocks || blocks % 2 !== 0) return { pbo: NaN, combinations: 0, logits: [] };
  const size = Math.floor(T / blocks);
  const groups: number[][] = [];
  for (let b = 0; b < blocks; b++) {
    const end = b === blocks - 1 ? T : (b + 1) * size;
    groups.push(Array.from({ length: end - b * size }, (_, i) => b * size + i));
  }
  const logits: number[] = [];
  for (const isSet of combinations(blocks, blocks / 2)) {
    const inIs = new Set(isSet);
    const isRows = isSet.flatMap((b) => groups[b]!);
    const oosRows = groups.flatMap((g, b) => (inIs.has(b) ? [] : g));
    const perf = (rows: number[], k: number) => metric(rows.map((r) => matrix[r]![k]!));
    let best = 0;
    let bestPerf = -Infinity;
    for (let k = 0; k < K; k++) {
      const p = perf(isRows, k);
      if (p > bestPerf) {
        bestPerf = p;
        best = k;
      }
    }
    const oos = Array.from({ length: K }, (_, k) => perf(oosRows, k));
    const target = oos[best]!;
    // Relative OOS rank of the IS winner, omega = rank / (K + 1) in (0, 1),
    // rank 1 = worst. logit(omega) <= 0 means it fell to the OOS bottom half.
    let below = 0;
    for (const v of oos) if (v < target) below++;
    const w = (below + 1) / (K + 1);
    logits.push(Math.log(w / (1 - w)));
  }
  const overfit = logits.filter((l) => l <= 0).length;
  return { pbo: logits.length ? overfit / logits.length : NaN, combinations: logits.length, logits };
}

function combinations(n: number, k: number): number[][] {
  const out: number[][] = [];
  const cur: number[] = [];
  const rec = (start: number) => {
    if (cur.length === k) {
      out.push([...cur]);
      return;
    }
    for (let i = start; i < n; i++) {
      cur.push(i);
      rec(i + 1);
      cur.pop();
    }
  };
  rec(0);
  return out;
}

/** Summary of a vector of per-trade returns / pnls used across reports. */
export interface TradeSummary {
  n: number;
  total: number;
  mean: number;
  median: number;
  winRate: number;
  avgWin: number;
  avgLoss: number;
  breakevenWinRate: number;
  profitFactor: number;
  maxDrawdown: number;
  meanCI: Interval;
}

export function summarize(xs: readonly number[], seed: number, iterations = 10_000): TradeSummary {
  const wins = xs.filter((x) => x > 0);
  const losses = xs.filter((x) => x <= 0);
  const avgWin = wins.length ? mean(wins) : 0;
  const avgLoss = losses.length ? mean(losses) : 0;
  return {
    n: xs.length,
    total: sum(xs),
    mean: mean(xs),
    median: median(xs),
    winRate: xs.length ? wins.length / xs.length : NaN,
    avgWin,
    avgLoss,
    breakevenWinRate: breakevenWinRate(avgWin, Math.abs(avgLoss)),
    profitFactor: profitFactor(xs),
    maxDrawdown: maxDrawdown(xs),
    meanCI: bootstrapCI(xs, { seed, iterations }),
  };
}
