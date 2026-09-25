/**
 * Meta-model training with honest validation (work plan 2026-09-25 P3.4).
 *
 * Primary model = the rule set (H12 population + confirm gate); the meta
 * model predicts P(primary signal is profitable after costs) and decides
 * take/skip [R6]. Every (lambda, threshold) variant is scored OUT OF SAMPLE on
 * purged, embargoed k-fold; the selection is then charged for the search:
 * Deflated Sharpe over all variants tried and PBO via CSCV [R7][R9].
 */
import { fitLogReg, predictProba, calibrationBins, logLoss, type LogRegModel } from './logreg.ts';
import { purgedKFold, walkForwardByDay } from './validate.ts';
import { bootstrapCI, deflatedSharpe, mean, pboCscv, profitFactor, sharpe, variance } from './stats.ts';
import { FEATURE_NAMES } from './featureSpec.ts';
import type { Sample } from './dataset.ts';

export interface TrainOptions {
  lambdas: readonly number[];
  thresholds: readonly number[];
  folds: number;
  embargoMs: number;
  seed: number;
  /** A variant must take at least this many OOS trades to be eligible. */
  minTaken: number;
}

export interface VariantResult {
  lambda: number;
  threshold: number;
  taken: number;
  meanNet: number;
  sharpe: number;
}

export interface TrainResult {
  n: number;
  baseRate: number;
  baselineMeanNet: number;
  chosen: VariantResult | null;
  variants: VariantResult[];
  oos: {
    taken: number;
    meanNet: number;
    ci: { lo: number; hi: number };
    profitFactor: number;
    logLoss: number;
    dsr: number;
    sr0: number;
    pbo: number;
    walkForward: { folds: number; taken: number; meanNet: number };
  } | null;
  calibration: ReturnType<typeof calibrationBins>;
  model: (LogRegModel & { threshold: number }) | null;
}

function oosProbs(samples: readonly Sample[], lambda: number, folds: ReturnType<typeof purgedKFold>): number[] {
  const p = new Array<number>(samples.length).fill(NaN);
  for (const f of folds) {
    if (f.train.length < 10) continue;
    const m = fitLogReg(
      f.train.map((i) => samples[i]!.x),
      f.train.map((i) => samples[i]!.label),
      { lambda, featureNames: FEATURE_NAMES },
    );
    for (const i of f.test) p[i] = predictProba(m, samples[i]!.x);
  }
  return p;
}

export function trainMetaModel(samples: readonly Sample[], o: TrainOptions): TrainResult {
  const n = samples.length;
  const y = samples.map((s) => s.label);
  const net = samples.map((s) => s.netReturn);
  const base: Omit<TrainResult, 'chosen' | 'variants' | 'oos' | 'calibration' | 'model'> = {
    n,
    baseRate: n ? mean(y) : NaN,
    baselineMeanNet: n ? mean(net) : NaN,
  };
  if (n < 20) return { ...base, chosen: null, variants: [], oos: null, calibration: [], model: null };

  const folds = purgedKFold(samples, o.folds, o.embargoMs);
  const probsByLambda = new Map(o.lambdas.map((l) => [l, oosProbs(samples, l, folds)] as const));

  const variants: VariantResult[] = [];
  // Per-opportunity return matrix for PBO: rows = samples in time order.
  const matrix: number[][] = samples.map(() => []);
  for (const l of o.lambdas) {
    const p = probsByLambda.get(l)!;
    for (const thr of o.thresholds) {
      const takenRets: number[] = [];
      samples.forEach((s, i) => {
        const take = Number.isFinite(p[i]!) && p[i]! >= thr;
        matrix[i]!.push(take ? s.netReturn : 0);
        if (take) takenRets.push(s.netReturn);
      });
      variants.push({ lambda: l, threshold: thr, taken: takenRets.length, meanNet: takenRets.length ? mean(takenRets) : NaN, sharpe: sharpe(takenRets) });
    }
  }
  const eligible = variants.filter((v) => v.taken >= o.minTaken && Number.isFinite(v.meanNet));
  const chosen = eligible.sort((a, b) => b.meanNet - a.meanNet)[0] ?? null;
  const p = chosen ? probsByLambda.get(chosen.lambda)! : probsByLambda.get(o.lambdas[0]!)!;
  const valid = p.map((v, i) => [v, i] as const).filter(([v]) => Number.isFinite(v));
  const calibration = calibrationBins(valid.map(([v]) => v), valid.map(([, i]) => y[i]!));

  let oos: TrainResult['oos'] = null;
  if (chosen) {
    const taken = samples.filter((_, i) => Number.isFinite(p[i]!) && p[i]! >= chosen.threshold).map((s) => s.netReturn);
    const srs = variants.filter((v) => Number.isFinite(v.sharpe)).map((v) => v.sharpe);
    const dsr = deflatedSharpe(taken, variants.length, srs.length > 1 ? variance(srs) : 0);
    const ci = bootstrapCI(taken, { seed: o.seed, iterations: 5_000 });
    // Walk-forward by day with the chosen hyper-parameters.
    const wf = walkForwardByDay(samples);
    const wfRets: number[] = [];
    for (const f of wf) {
      const m = fitLogReg(f.train.map((i) => samples[i]!.x), f.train.map((i) => y[i]!), { lambda: chosen.lambda, featureNames: FEATURE_NAMES });
      for (const i of f.test) if (predictProba(m, samples[i]!.x) >= chosen.threshold) wfRets.push(samples[i]!.netReturn);
    }
    oos = {
      taken: taken.length,
      meanNet: mean(taken),
      ci: { lo: ci.lo, hi: ci.hi },
      profitFactor: profitFactor(taken),
      logLoss: logLoss(valid.map(([v]) => v), valid.map(([, i]) => y[i]!)),
      dsr: dsr.dsr,
      sr0: dsr.sr0,
      pbo: pboCscv(matrix, Math.min(8, Math.max(2, 2 * Math.floor(n / 40)))).pbo,
      walkForward: { folds: wf.length, taken: wfRets.length, meanNet: wfRets.length ? mean(wfRets) : NaN },
    };
  }
  const model = chosen
    ? { ...fitLogReg(samples.map((s) => s.x), y, { lambda: chosen.lambda, featureNames: FEATURE_NAMES }), threshold: chosen.threshold }
    : null;
  return { ...base, chosen, variants, oos, calibration, model };
}
