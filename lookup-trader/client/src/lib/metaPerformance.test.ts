import { describe, expect, it } from "vitest";
import { grossPips, netPips } from "@/lib/metaPerformance";

const event = {
  symbol: "XAUUSD",
  side: 1 as const,
  entry_price: 4300,
  exit_price: 4302,
};

describe("meta-event pip performance", () => {
  it("signs long wins and losses correctly", () => {
    expect(grossPips(event)).toBeCloseTo(20);
    expect(netPips(event)).toBeCloseTo(15);
    expect(netPips({ ...event, exit_price: 4298 })).toBeCloseTo(-25);
  });

  it("signs short wins and losses correctly", () => {
    expect(netPips({ ...event, side: -1, exit_price: 4298 })).toBeCloseTo(15);
    expect(netPips({ ...event, side: -1, exit_price: 4302 })).toBeCloseTo(-25);
  });

  it("returns null until both entry and exit are known", () => {
    expect(netPips({ ...event, exit_price: null })).toBeNull();
    expect(netPips({ ...event, entry_price: null })).toBeNull();
  });
});
