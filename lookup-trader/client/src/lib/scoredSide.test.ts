import { describe, expect, it } from "vitest";
import { inferScoredSide } from "@/lib/scoredSide";

describe("inferScoredSide", () => {
  it("prefers explicit form side", () => {
    expect(inferScoredSide({ formSide: "-1", entry: null, tp: null })).toBe(-1);
  });

  it("infers from marked levels", () => {
    expect(inferScoredSide({ entry: 100, tp: 105, trendState: "down" })).toBe(1);
    expect(inferScoredSide({ entry: 100, tp: 95, trendState: "up" })).toBe(-1);
  });

  it("follows trend when side is open", () => {
    expect(inferScoredSide({ entry: null, tp: null, trendState: "down" })).toBe(-1);
    expect(inferScoredSide({ entry: null, tp: null, trendState: "up" })).toBe(1);
  });

  it("defaults to long", () => {
    expect(inferScoredSide({ entry: null, tp: null })).toBe(1);
  });
});
