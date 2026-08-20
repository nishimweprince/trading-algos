import { describe, expect, it } from "vitest";
import { filterBySession, pairSessionBreakdown, sessionBreakdown, winRate } from "./stats";
import type { ClosedLeg, TradePairResult } from "./types";

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

describe("pairSessionBreakdown", () => {
  const pair: TradePairResult = {
    id: "tokyo:2026-08-18",
    session: "tokyo",
    entry: 4421.77,
    entry_ts: "2026-08-18T00:30:00Z",
    status: "partial",
    primary: {
      side: "short",
      role: "primary",
      status: "open",
      exit: null,
      exit_ts: null,
      pnl_pips: 100,
      pnl_dollars: 1000,
      bucket: null,
      reason: null,
    },
    hedge: {
      side: "long",
      role: "hedge",
      status: "closed",
      exit: 4400.99,
      exit_ts: "2026-08-18T02:15:00Z",
      pnl_pips: -207.8,
      pnl_dollars: -2078,
      bucket: "loss",
      reason: "sl_or_tp",
    },
    unknown_legs: [],
    pnl_pips: -107.8,
    pnl_dollars: -1078,
  };

  it("counts closed outcomes while totals include open marked P&L", () => {
    expect(pairSessionBreakdown([pair], "pips")[0]).toMatchObject({
      session: "tokyo",
      loss: 1,
      pnl: -107.8,
    });
    expect(pairSessionBreakdown([pair], "dollars")[0].pnl).toBe(-1078);
  });
});
