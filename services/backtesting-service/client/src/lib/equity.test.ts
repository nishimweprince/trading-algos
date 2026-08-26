import { describe, expect, it } from "vitest";
import { buildEquityChartData } from "./equity";

describe("equity chart data", () => {
  it("sorts points, coalesces timestamps, and plots drawdown below zero", () => {
    const data = buildEquityChartData([
      { ts: "2026-08-24T10:15:00Z", net_equity: 8, net_drawdown: 2 },
      { ts: "invalid", net_equity: 99, net_drawdown: 99 },
      { ts: "2026-08-24T10:00:00Z", net_equity: 10, net_drawdown: 0 },
      { ts: "2026-08-24T10:15:00Z", net_equity: 7, net_drawdown: 3 },
    ]);

    expect(data).toHaveLength(2);
    expect(data.map((point) => point.equity)).toEqual([10, 7]);
    expect(data.map((point) => point.drawdown)).toEqual([-0, -3]);
    expect(data[0].time).toBeLessThan(data[1].time);
  });
});
