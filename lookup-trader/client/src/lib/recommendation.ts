/** Layman-readable verdict from historical compare / base-rate stats. */

export type LaymanVerdict =
  | "buy"
  | "sell"
  | "lean_long"
  | "lean_short"
  | "wait"
  | "insufficient_data";

export interface RecommendationInput {
  side: 1 | -1;
  winRate: number | null;
  wilsonLow: number | null;
  wilsonHigh: number | null;
  expectancyR: number | null;
  decided: number;
  minSamples: number;
  levelUsed?: string;
  effectiveN?: number | null;
  overlapRatio?: number | null;
  breakEvenWinRate: number;
  setupDelta?: number | null;
}

export interface RecommendationResult {
  verdict: LaymanVerdict;
  headline: string;
  rationale: string;
  caveats: string[];
}

/** Break-even win rate from stop/target geometry: stop / (stop + target). */
export function breakEvenFromGeometry(stop: number, target: number): number {
  if (stop <= 0 || target <= 0) return 0.4;
  return stop / (stop + target);
}

/** Default 1R stop / 1.5R target → 40%. */
export function breakEvenFromRr(rr: number | null): number {
  if (rr == null || rr <= 0) return 1 / (1 + 1.5);
  return 1 / (1 + rr);
}

export function deriveRecommendation(input: RecommendationInput): RecommendationResult {
  const {
    side,
    wilsonLow,
    expectancyR,
    decided,
    minSamples,
    levelUsed,
    effectiveN,
    overlapRatio,
    breakEvenWinRate,
    setupDelta,
  } = input;

  const caveats: string[] = [];

  if (levelUsed === "no_signal" || decided < minSamples) {
    return {
      verdict: "insufficient_data",
      headline: "Insufficient data",
      rationale: "Not enough resolved history to trust this yet.",
      caveats,
    };
  }

  if (overlapRatio != null && overlapRatio > 0.4) {
    caveats.push("Sample may be overstated — overlapping holding windows.");
  }
  if (setupDelta != null && Math.abs(setupDelta) <= 0.01) {
    caveats.push("Your setup isn't beating the context prior.");
  }
  if (effectiveN != null && decided > 0 && effectiveN < decided / 3) {
    caveats.push(`Only ~${Math.round(effectiveN)} independent bars behind this.`);
  }

  const favorable =
    expectancyR != null &&
    expectancyR > 0 &&
    wilsonLow != null &&
    wilsonLow >= breakEvenWinRate;

  if (expectancyR != null && expectancyR <= 0) {
    return {
      verdict: "wait",
      headline: "Wait",
      rationale: "Past bars in this context don't support taking the trade.",
      caveats,
    };
  }

  if (wilsonLow != null && wilsonLow < breakEvenWinRate) {
    return {
      verdict: side === 1 ? "lean_long" : "lean_short",
      headline: side === 1 ? "Lean long" : "Lean short",
      rationale: "Historical expectancy is positive, but uncertainty still crosses zero.",
      caveats,
    };
  }

  if (favorable) {
    const direction = side === 1 ? "buying" : "selling";
    return {
      verdict: side === 1 ? "buy" : "sell",
      headline: side === 1 ? "Buy" : "Sell",
      rationale: `Past bars in this context slightly favor ${direction}.`,
      caveats,
    };
  }

  return {
    verdict: "wait",
    headline: "Wait",
    rationale: "Mixed — the edge is too small to act on from history alone.",
    caveats,
  };
}
