/**
 * Paper-twin calibration loop (paper-honesty Unit 10): do the models' entry-time
 * predictions match what happened next?
 *
 * - Fill-p: each track stores the trailing fill probability it priced
 *   (fill_p_json). The loop replays the pool-buy attempts observed in a
 *   FORWARD window after each track's entry and compares the realized land
 *   rate against the predicted p, per (offset, contested) bucket. A trailing
 *   estimator is only honest while the regime holds — the gap series is the
 *   regime detector.
 * - Costs: measured buy fee/tip medians over the trailing window vs the prior
 *   window (regime drift) and vs the configured live cost (stale assumption).
 * - Landing: OUR landing offsets are unobservable by construction (zero sends)
 *   — calibration stays pending until live sends exist. Stated, not faked.
 *
 * Pure over caller-supplied rows: the dashboard (queries.ts) and the
 * racer:calibration CLI share getRacerCalibration, which runs the SQL.
 */
import { FILL_BUCKET_MIN_N } from './fills.ts';
import { median } from './costs.ts';

export const CALIBRATION_DEFAULTS = {
  /** Forward outcome window after each track's entry, hours. */
  forwardHours: 24,
  /** Minimum predictions AND observations per bucket (mirrors the fill model). */
  minN: FILL_BUCKET_MIN_N,
  /** |observed − predicted| above this raises a fill-gap alert. */
  fillGapAlert: 0.15,
  /** |drift| % above this raises a cost alert. */
  costDriftAlertPct: 25,
  /** Cost regime windows, hours (trailing vs prior). */
  costWindowHours: 24,
} as const;

export interface FillPrediction {
  offset: number;
  contested: boolean;
  p: number;
}

export interface FillObservation {
  offset: number;
  contested: boolean;
  ok: number;
  n: number;
}

export interface FillCalibrationBucket {
  offset: number;
  contested: boolean;
  predictedP: number | null;
  observedRate: number | null;
  predictedN: number;
  observedN: number;
  /** observed − predicted; positive = the model under-called the fill rate. */
  gap: number | null;
}

export interface FillCalibration {
  buckets: FillCalibrationBucket[];
  /** Mean |gap| over calibrated buckets; null when none qualify. */
  meanAbsGap: number | null;
  /** Mean gap over calibrated buckets (systematic bias); null when none qualify. */
  bias: number | null;
  tracks: number;
}

function bucketKey(offset: number, contested: boolean): string {
  return `${offset}:${contested ? 1 : 0}`;
}

/** Compare entry-time fill predictions against forward-realized land rates. */
export function calibrateFill(
  predictions: readonly FillPrediction[],
  observations: readonly FillObservation[],
  tracks: number,
  minN: number = CALIBRATION_DEFAULTS.minN,
): FillCalibration {
  const pred = new Map<string, { sum: number; n: number }>();
  for (const p of predictions) {
    if (!Number.isFinite(p.p) || p.p < 0 || p.p > 1) continue;
    const k = bucketKey(p.offset, p.contested);
    const e = pred.get(k) ?? { sum: 0, n: 0 };
    e.sum += p.p;
    e.n++;
    pred.set(k, e);
  }
  const obs = new Map<string, { ok: number; n: number }>();
  for (const o of observations) {
    const k = bucketKey(o.offset, o.contested);
    const e = obs.get(k) ?? { ok: 0, n: 0 };
    e.ok += o.ok;
    e.n += o.n;
    obs.set(k, e);
  }
  const buckets: FillCalibrationBucket[] = [];
  for (const [k, p] of pred) {
    const o = obs.get(k);
    const [offset, contested] = k.split(':');
    const predictedP = p.n >= minN ? p.sum / p.n : null;
    const observedRate = o && o.n >= minN && o.n > 0 ? o.ok / o.n : null;
    buckets.push({
      offset: Number(offset), contested: contested === '1',
      predictedP, observedRate, predictedN: p.n, observedN: o?.n ?? 0,
      gap: predictedP !== null && observedRate !== null ? observedRate - predictedP : null,
    });
  }
  buckets.sort((a, b) => a.offset - b.offset || Number(a.contested) - Number(b.contested));
  const gaps = buckets.map((b) => b.gap).filter((g): g is number => g !== null);
  return {
    buckets,
    meanAbsGap: gaps.length ? gaps.reduce((s, g) => s + Math.abs(g), 0) / gaps.length : null,
    bias: gaps.length ? gaps.reduce((s, g) => s + g, 0) / gaps.length : null,
    tracks,
  };
}

export interface CostSample {
  feeSol: number;
  tipSol: number;
  atMs: number;
}

export interface CostDrift {
  window: { medianFeeSol: number | null; medianTipSol: number | null; n: number };
  prior: { medianFeeSol: number | null; medianTipSol: number | null; n: number };
  /** Median measured total (fee + tip + sell) vs the configured live cost, %. */
  measuredVsConfigPct: number | null;
  feeDriftPct: number | null;
  tipDriftPct: number | null;
}

function driftPct(next: number | null, prev: number | null): number | null {
  if (next === null || prev === null || !(prev > 0)) return null;
  return ((next - prev) / prev) * 100;
}

/** Regime drift in measured buy costs, plus staleness vs the configured assumption. */
export function costDrift(
  samples: readonly CostSample[],
  nowMs: number,
  config: { sellCostSol: number; liveCostSol: number; windowHours?: number },
): CostDrift {
  const windowMs = (config.windowHours ?? CALIBRATION_DEFAULTS.costWindowHours) * 3_600_000;
  const win = samples.filter((s) => s.atMs >= nowMs - windowMs);
  const prior = samples.filter((s) => s.atMs < nowMs - windowMs && s.atMs >= nowMs - 2 * windowMs);
  const med = (rows: readonly CostSample[]) => ({
    medianFeeSol: median(rows.map((r) => r.feeSol).sort((a, b) => a - b)),
    medianTipSol: median(rows.map((r) => r.tipSol).sort((a, b) => a - b)),
    n: rows.length,
  });
  const w = med(win);
  const p = med(prior);
  const measuredTotal = w.medianFeeSol !== null && w.medianTipSol !== null
    ? w.medianFeeSol + w.medianTipSol + config.sellCostSol
    : null;
  return {
    window: w,
    prior: p,
    measuredVsConfigPct: measuredTotal !== null && config.liveCostSol > 0
      ? ((measuredTotal - config.liveCostSol) / config.liveCostSol) * 100
      : null,
    feeDriftPct: driftPct(w.medianFeeSol, p.medianFeeSol),
    tipDriftPct: driftPct(w.medianTipSol, p.medianTipSol),
  };
}

export interface CalibrationAlert {
  code: 'fill_gap' | 'cost_drift_fee' | 'cost_drift_tip' | 'cost_vs_config_stale';
  detail: string;
}

/** Alert when a calibrated bucket or cost series drifts past its threshold. */
export function calibrationAlerts(
  fill: FillCalibration,
  cost: CostDrift,
  opts: { fillGapAlert?: number | undefined; costDriftAlertPct?: number | undefined } = {},
): CalibrationAlert[] {
  const alerts: CalibrationAlert[] = [];
  const gapT = opts.fillGapAlert ?? CALIBRATION_DEFAULTS.fillGapAlert;
  const costT = opts.costDriftAlertPct ?? CALIBRATION_DEFAULTS.costDriftAlertPct;
  for (const b of fill.buckets) {
    if (b.gap !== null && Math.abs(b.gap) > gapT) {
      alerts.push({
        code: 'fill_gap',
        detail: `offset ${b.offset}${b.contested ? ' contested' : ''}: predicted ${b.predictedP!.toFixed(3)}, observed ${b.observedRate!.toFixed(3)} (n=${b.predictedN}/${b.observedN})`,
      });
    }
  }
  if (cost.feeDriftPct !== null && Math.abs(cost.feeDriftPct) > costT) {
    alerts.push({ code: 'cost_drift_fee', detail: `buy fee median drifted ${cost.feeDriftPct.toFixed(1)}% window-over-window` });
  }
  if (cost.tipDriftPct !== null && Math.abs(cost.tipDriftPct) > costT) {
    alerts.push({ code: 'cost_drift_tip', detail: `buy tip median drifted ${cost.tipDriftPct.toFixed(1)}% window-over-window` });
  }
  if (cost.measuredVsConfigPct !== null && Math.abs(cost.measuredVsConfigPct) > costT) {
    alerts.push({ code: 'cost_vs_config_stale', detail: `measured total ${cost.measuredVsConfigPct.toFixed(1)}% vs configured live cost` });
  }
  return alerts;
}
