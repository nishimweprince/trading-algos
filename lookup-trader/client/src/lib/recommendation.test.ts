import { describe, expect, it } from "vitest";
import { breakEvenFromRr, deriveRecommendation } from "@/lib/recommendation";

const base = {
  side: 1 as const,
  winRate: 0.55,
  wilsonLow: 0.48,
  wilsonHigh: 0.62,
  expectancyR: 0.12,
  decided: 50,
  minSamples: 30,
  breakEvenWinRate: 0.4,
};

describe("deriveRecommendation", () => {
  it("returns insufficient_data when sample is thin", () => {
    const r = deriveRecommendation({ ...base, decided: 5, minSamples: 30 });
    expect(r.verdict).toBe("insufficient_data");
    expect(r.headline).toBe("Insufficient data");
  });

  it("returns wait on negative expectancy", () => {
    const r = deriveRecommendation({ ...base, expectancyR: -0.05, wilsonLow: 0.35 });
    expect(r.verdict).toBe("wait");
  });

  it("returns buy when edge clears break-even", () => {
    const r = deriveRecommendation(base);
    expect(r.verdict).toBe("buy");
    expect(r.headline).toBe("Buy");
  });

  it("returns sell for short side", () => {
    const r = deriveRecommendation({ ...base, side: -1 });
    expect(r.verdict).toBe("sell");
  });

  it("adds overlap caveat", () => {
    const r = deriveRecommendation({ ...base, overlapRatio: 0.6 });
    expect(r.caveats.some((c) => c.includes("overlapping"))).toBe(true);
  });
});

describe("breakEvenFromRr", () => {
  it("defaults to 1.5R geometry", () => {
    expect(breakEvenFromRr(null)).toBeCloseTo(1 / 2.5);
  });
});
