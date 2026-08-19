import { describe, expect, it } from "vitest";
import { filterBySession, sessionBreakdown, winRate } from "./stats";
import type { ClosedLeg } from "./types";

function trade(partial: Partial<ClosedLeg> & Pick<ClosedLeg, "session" | "bucket" | "pnl">): ClosedLeg {
  return {
    side: "long",
    entry: 2000,
    exit: 2010,
    ts: "2026-01-14T13:15:00Z",
    reason: "sl_or_tp",
    ...partial,
  };
}

describe("winRate", () => {
  it("returns null when there are no closed legs", () => {
    expect(winRate(0, 0, 0)).toBeNull();
  });

  it("divides wins by all closed outcomes including breakeven", () => {
    expect(winRate(2, 1, 1)).toBe(0.5);
  });
});

describe("filterBySession", () => {
  const trades = [
    trade({ session: "tokyo", bucket: "win", pnl: 10 }),
    trade({ session: "london", bucket: "loss", pnl: -5 }),
  ];

  it("returns every trade when no city is selected", () => {
    expect(filterBySession(trades, null)).toHaveLength(2);
  });

  it("keeps only the selected city", () => {
    expect(filterBySession(trades, "tokyo").map((row) => row.session)).toEqual(["tokyo"]);
  });
});

describe("sessionBreakdown", () => {
  it("groups pnl and buckets in tokyo-london-new_york order", () => {
    const rows = sessionBreakdown([
      trade({ session: "new_york", bucket: "win", pnl: 3 }),
      trade({ session: "tokyo", bucket: "loss", pnl: -1 }),
      trade({ session: "tokyo", bucket: "be", pnl: 0 }),
      trade({ session: "london", bucket: "win", pnl: 8 }),
    ]);
    expect(rows.map((row) => row.session)).toEqual(["tokyo", "london", "new_york"]);
    expect(rows[0]).toMatchObject({ wins: 0, be: 1, loss: 1, pnl: -1 });
    expect(rows[1]).toMatchObject({ wins: 1, be: 0, loss: 0, pnl: 8 });
  });
});
