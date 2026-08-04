/** Mirrors server `settings.max_bars` — keep in sync with labeler. */
export const MAX_BARS = 24;

export const AMBIGUOUS_POLICY = "conservative" as const;

/** Mirrors server `settings.rr_buckets` — the cut points /compare filters on. */
export const RR_BUCKETS: [number, number] = [1.5, 2.5];

/**
 * Mirrors server `settings.touch_levels` — the ATR distances the bar feature
 * store records a first touch for. /base-rate only prices these, so a marked
 * stop or target has to be snapped onto the ladder before it can be asked about.
 */
export const TOUCH_LEVELS = [0.5, 1.0, 1.5, 2.0, 3.0] as const;

/** Mirrors server `settings.feature_horizons`. */
export const FEATURE_HORIZONS = [6, 12, 24, 48] as const;

/** Nearest ATR distance the store can answer for. */
export function snapToTouchLevel(value: number): number {
  return TOUCH_LEVELS.reduce((best, level) =>
    Math.abs(level - value) < Math.abs(best - value) ? level : best,
  );
}
