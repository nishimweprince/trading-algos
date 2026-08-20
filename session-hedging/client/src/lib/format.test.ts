import { describe, expect, it } from "vitest";
import { formatDollars, formatPerformance, formatPips } from "./format";

describe("performance formatting", () => {
  it("labels pips and dollars explicitly", () => {
    expect(formatPips(12.5)).toBe("+12.50 pips");
    expect(formatDollars(-125)).toBe("−$125.00");
  });

  it("uses the selected unit and safely falls back to pips", () => {
    expect(formatPerformance(12.5, 125, "dollars")).toBe("+$125.00");
    expect(formatPerformance(12.5, null, "dollars")).toBe("+12.50 pips");
  });
});
