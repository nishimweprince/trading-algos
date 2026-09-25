/**
 * Out-of-sample validation for anything tuned on this data (work plan
 * 2026-09-25 P3.4 / P3.5; [R7]–[R9]). Two schemes:
 *
 *  - purgedKFold: contiguous time folds; training samples whose label window
 *    overlaps the test fold are purged, and an embargo after the test fold is
 *    dropped too, so no label information leaks across the boundary.
 *  - walkForwardByDay: train on all days < d, test on day d.
 *
 * Every sample carries its decision time `t` and label end `tEnd` (ms).
 */
export interface Timed {
  t: number;
  tEnd: number;
}

export interface Fold {
  train: number[];
  test: number[];
}

export function purgedKFold(samples: readonly Timed[], k: number, embargoMs: number): Fold[] {
  const order = samples.map((_, i) => i).sort((a, b) => samples[a]!.t - samples[b]!.t);
  const size = Math.ceil(order.length / k);
  const folds: Fold[] = [];
  for (let f = 0; f < k; f++) {
    const test = order.slice(f * size, (f + 1) * size);
    if (!test.length) continue;
    const tStart = Math.min(...test.map((i) => samples[i]!.t));
    const tStop = Math.max(...test.map((i) => samples[i]!.tEnd));
    const testSet = new Set(test);
    const train = order.filter((i) => {
      if (testSet.has(i)) return false;
      const s = samples[i]!;
      const overlaps = s.tEnd >= tStart && s.t <= tStop; // purge
      const embargoed = s.t > tStop && s.t <= tStop + embargoMs; // embargo
      return !overlaps && !embargoed;
    });
    folds.push({ train, test });
  }
  return folds;
}

export function walkForwardByDay(samples: readonly Timed[], minTrainDays = 1): Fold[] {
  const day = (t: number) => Math.floor(t / 86_400_000);
  const days = [...new Set(samples.map((s) => day(s.t)))].sort((a, b) => a - b);
  const folds: Fold[] = [];
  for (let d = minTrainDays; d < days.length; d++) {
    const cut = days[d]!;
    const train = samples.map((_, i) => i).filter((i) => day(samples[i]!.tEnd) < cut);
    const test = samples.map((_, i) => i).filter((i) => day(samples[i]!.t) === cut);
    if (train.length && test.length) folds.push({ train, test });
  }
  return folds;
}
