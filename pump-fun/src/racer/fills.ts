/**
 * Empirical fill probability for racer paper legs (pure).
 *
 * Paper legs are entry PRICES; a live buy sent for slot k also needs to LAND
 * in slot k. The fill probability comes from the racer's own observed pool-buy
 * attempts (racer_pool_buy_attempts): of outside buys seen in slot migrate+k,
 * the fraction with ok = 1 — bucketed by slot offset and by whether the
 * migration was contested (failed migrate attempts at/under the migrate slot),
 * trailing over a lookback window. Assumption, documented: our send behaves
 * like the observed +k-slot buyer population (priority fees + tips are already
 * in liveCostSol; this models only land-vs-not).
 *
 * Failed attempts still pay network fees. With fill-p p per attempt, the
 * expected attempt count is 1/p and the expected FAILED count (1-p)/p, each
 * charged at the trailing mean failed fee. The successful attempt's fee is
 * already inside liveCostSol — only failures are charged here, so there is no
 * double count. Open/reach legs are idealised (be-the-migration) and carry no
 * fill model.
 */

export interface FillBucket {
  offset: number;
  contested: boolean;
  ok: number;
  n: number;
}

export interface FillStats {
  buckets: FillBucket[];
  meanFailedFeeSol: number | null;
}

/** Buckets below this n fall back to the offset-pooled rate, then to null. */
export const FILL_BUCKET_MIN_N = 20;

/** Trailing ok-rate for (offset, contested): exact bucket → offset-pooled → null. */
export function rateFor(stats: FillStats, offset: number, contested: boolean, minN = FILL_BUCKET_MIN_N): number | null {
  const exact = stats.buckets.find((b) => b.offset === offset && b.contested === contested);
  if (exact && exact.n >= minN && exact.n > 0) return exact.ok / exact.n;
  let ok = 0;
  let n = 0;
  for (const b of stats.buckets) {
    if (b.offset !== offset) continue;
    ok += b.ok;
    n += b.n;
  }
  if (n >= minN && n > 0) return ok / n;
  return null;
}

/** Expected failed-attempt cost in SOL: E[failures] × mean failed fee. p = 1 costs nothing. */
export function attemptCostSol(p: number | null, meanFailedFeeSol: number | null): number | null {
  if (!(p !== null && p > 0 && p <= 1)) return null;
  if (!(meanFailedFeeSol !== null && meanFailedFeeSol >= 0 && Number.isFinite(meanFailedFeeSol))) return null;
  return ((1 - p) / p) * meanFailedFeeSol;
}

export interface LegFill {
  p: number;
  costSol: number;
}

/** Fill model for one entry leg, or null when the trailing data cannot price it. */
export function legFill(stats: FillStats, offset: number, contested: boolean, minN = FILL_BUCKET_MIN_N): LegFill | null {
  const p = rateFor(stats, offset, contested, minN);
  if (p === null) return null;
  const costSol = attemptCostSol(p, stats.meanFailedFeeSol);
  if (costSol === null) return null;
  return { p, costSol };
}

/** Expectancy of a conditional-on-fill leg value: fill it with p, pay attempts regardless. */
export function adjustLegForFill(valuePct: number, fill: LegFill, sizeSol: number): number | null {
  if (!(sizeSol > 0) || !Number.isFinite(valuePct)) return null;
  return valuePct * fill.p - (fill.costSol / sizeSol) * 100;
}
