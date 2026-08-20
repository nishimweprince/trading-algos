import { describe, expect, it } from "vitest";
import {
  formatDollars,
  formatPerSide,
  formatPerformance,
  formatPips,
  formatPipsAndR,
  formatPp,
  formatR,
  formatUnit,
  formatUnitAndR,
} from "./format";

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

  it("renders an amount in whichever unit the run used", () => {
    expect(formatUnit(137.5, "pips")).toBe("+137.50 pips");
    expect(formatUnit(1375, "dollars")).toBe("+$1,375.00");
    expect(formatUnit(-42, "dollars")).toBe("−$42.00");
  });

  it("keeps R beside the unit amount, because R never converts", () => {
    expect(formatUnitAndR(137.5, 0.5, "pips")).toBe("+137.50 pips · +0.50R");
    expect(formatUnitAndR(1375, 0.5, "dollars")).toBe("+$1,375.00 · +0.50R");
  });

  it("labels per-side costs in the selected unit", () => {
    expect(formatPerSide(3.79, "pips")).toBe("3.8p / side");
    expect(formatPerSide(37.9, "dollars")).toBe("+$37.90 / side");
    expect(formatPerSide(null, "pips")).toBe("—");
  });
});
