import { describe, expect, it } from "vitest";
import { formatDollars, formatPerformance, formatPips, formatPipsAndR, formatPp, formatR } from "./format";

describe("performance formatting", () => {
  it("labels pips and dollars explicitly", () => {
    expect(formatPips(12.5)).toBe("+12.50 pips");
    expect(formatDollars(-125)).toBe("−$125.00");
  });

  it("uses the selected unit and safely falls back to pips", () => {
    expect(formatPerformance(12.5, 125, "dollars")).toBe("+$125.00");
    expect(formatPerformance(12.5, null, "dollars")).toBe("+12.50 pips");
  });

  it("shows pips and R together", () => {
    expect(formatR(-0.74)).toBe("−0.74R");
    expect(formatPipsAndR(137.5, 0.5)).toBe("+137.50 pips · +0.50R");
    expect(formatPp(2.7)).toBe("+2.7 pp");
  });
});
