/** Mirrors server `settings.max_bars` — keep in sync with labeler. */
export const MAX_BARS = 24;

export const AMBIGUOUS_POLICY = "conservative" as const;

/** Mirrors server `settings.rr_buckets` — the cut points /compare filters on. */
export const RR_BUCKETS: [number, number] = [1.5, 2.5];
