import type { BarTag } from "@/types";

/**
 * Match quality a tag must clear before it fills the form for the operator.
 *
 * The server's scale runs [0.6, 1.0], where 0.6 is a bar that exactly meets
 * every threshold and nothing more. 0.75 is roughly "clears every dimension by
 * 1.4x" — enough that the read is not arguable.
 */
export const AUTO_FILL_MIN_CONFIDENCE = 0.75;

/** Fully-formed patterns only. A `forming` structure is not yet a pattern. */
export function completeTags(tags: BarTag[] | undefined): BarTag[] {
  return (tags ?? []).filter((t) => t.state === "complete");
}

/** Complete tag to condition the base rate on; forming tags never qualify. */
export function baseRateTag(
  tags: BarTag[] | undefined,
  selectedSetup?: string | null,
  primarySetup?: string | null,
): BarTag | null {
  const complete = completeTags(tags);
  const setupId = selectedSetup || primarySetup;
  return complete.find((tag) => tag.setup_id === setupId) ?? null;
}

/**
 * The one tag confident enough to fill the setup field, or null.
 *
 * "Exactly one" is deliberate. A bar can read as two patterns at once — a small
 * bearish body with a long lower wick is both a bearish engulfing and a bullish
 * pin bar — and picking the marginally higher score for the operator would hide
 * a genuine ambiguity behind a filled field.
 */
export function autoFillTag(
  tags: BarTag[] | undefined,
  minConfidence = AUTO_FILL_MIN_CONFIDENCE,
): BarTag | null {
  const confident = completeTags(tags).filter((t) => t.confidence >= minConfidence);
  return confident.length === 1 ? confident[0] : null;
}

/**
 * Why the setup was not auto-filled, or null when there is nothing to explain.
 *
 * Silence is the common case: no tags means no message, and a clean auto-fill
 * speaks for itself.
 */
export function tagHint(
  tags: BarTag[] | undefined,
  minConfidence = AUTO_FILL_MIN_CONFIDENCE,
): string | null {
  const complete = completeTags(tags);
  if (complete.length === 0) return null;

  const confident = complete.filter((t) => t.confidence >= minConfidence);
  if (confident.length > 1) {
    return `${confident.length} patterns on this bar — pick one.`;
  }
  if (confident.length === 0) {
    return complete.length === 1
      ? "Marginal match — setup not filled in for you."
      : "Marginal matches — setup not filled in for you.";
  }
  return null;
}
