/**
 * L2-regularised logistic regression (work plan 2026-09-25 P3.4). Small,
 * deterministic, dependency-free, and fully inspectable: the prediction is a
 * sum of per-feature contributions w_i·z_i, which the dashboard shows per
 * trade (exact attributions — no SHAP approximation needed).
 *
 * Features are standardised with the TRAINING fold's mean/std only.
 */
export interface LogRegModel {
  featureNames: string[];
  mean: number[];
  std: number[];
  weights: number[];
  bias: number;
  lambda: number;
}

export function sigmoid(z: number): number {
  if (z >= 0) return 1 / (1 + Math.exp(-z));
  const e = Math.exp(z);
  return e / (1 + e);
}

export function fitLogReg(
  X: readonly (readonly number[])[],
  y: readonly number[],
  opts: { lambda: number; iterations?: number; learningRate?: number; featureNames: readonly string[]; sampleWeights?: readonly number[] },
): LogRegModel {
  const n = X.length;
  const d = X[0]?.length ?? 0;
  const mean = new Array<number>(d).fill(0);
  const std = new Array<number>(d).fill(0);
  for (const row of X) for (let j = 0; j < d; j++) mean[j]! += row[j]! / n;
  for (const row of X) for (let j = 0; j < d; j++) std[j]! += (row[j]! - mean[j]!) ** 2 / n;
  for (let j = 0; j < d; j++) std[j] = Math.sqrt(std[j]!) || 1;
  const Z = X.map((row) => row.map((v, j) => (v - mean[j]!) / std[j]!));
  const w = new Array<number>(d).fill(0);
  let bias = 0;
  const iters = opts.iterations ?? 500;
  const lr = opts.learningRate ?? 0.5;
  const sw = opts.sampleWeights ?? new Array<number>(n).fill(1);
  const wsum = sw.reduce((a, b) => a + b, 0) || 1;
  for (let it = 0; it < iters; it++) {
    const gw = new Array<number>(d).fill(0);
    let gb = 0;
    for (let i = 0; i < n; i++) {
      const zi = Z[i]!;
      let s = bias;
      for (let j = 0; j < d; j++) s += w[j]! * zi[j]!;
      const err = (sigmoid(s) - y[i]!) * sw[i]!;
      gb += err;
      for (let j = 0; j < d; j++) gw[j]! += err * zi[j]!;
    }
    bias -= (lr * gb) / wsum;
    for (let j = 0; j < d; j++) w[j]! -= lr * (gw[j]! / wsum + opts.lambda * w[j]!);
  }
  return { featureNames: [...opts.featureNames], mean, std, weights: w, bias, lambda: opts.lambda };
}

export function predictProba(m: LogRegModel, x: readonly number[]): number {
  let s = m.bias;
  for (let j = 0; j < m.weights.length; j++) s += m.weights[j]! * ((x[j]! - m.mean[j]!) / m.std[j]!);
  return sigmoid(s);
}

/** Per-feature contribution to the logit, largest magnitude first. */
export function contributions(m: LogRegModel, x: readonly number[], top = 8): Array<{ feature: string; logit: number }> {
  return m.weights
    .map((w, j) => ({ feature: m.featureNames[j]!, logit: w * ((x[j]! - m.mean[j]!) / m.std[j]!) }))
    .filter((c) => c.logit !== 0)
    .sort((a, b) => Math.abs(b.logit) - Math.abs(a.logit))
    .slice(0, top);
}

/** Reliability bins: mean predicted vs observed frequency. */
export function calibrationBins(p: readonly number[], y: readonly number[], bins = 10): Array<{ lo: number; hi: number; n: number; meanP: number; freq: number }> {
  const out = Array.from({ length: bins }, (_, i) => ({ lo: i / bins, hi: (i + 1) / bins, n: 0, meanP: 0, freq: 0 }));
  p.forEach((pi, i) => {
    const b = out[Math.min(bins - 1, Math.floor(pi * bins))]!;
    b.n++;
    b.meanP += pi;
    b.freq += y[i]!;
  });
  for (const b of out) if (b.n) {
    b.meanP /= b.n;
    b.freq /= b.n;
  }
  return out;
}

/** Log loss with clipping. */
export function logLoss(p: readonly number[], y: readonly number[]): number {
  let s = 0;
  p.forEach((pi, i) => {
    const q = Math.min(1 - 1e-12, Math.max(1e-12, pi));
    s -= y[i]! * Math.log(q) + (1 - y[i]!) * Math.log(1 - q);
  });
  return p.length ? s / p.length : NaN;
}
