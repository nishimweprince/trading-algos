import type { EquityCurvePoint } from "./types";

export interface EquityChartDatum {
  time: number;
  ts: string;
  equity: number;
  drawdown: number;
}

/** Normalize API observations for charting and render drawdown below the zero line. */
export function buildEquityChartData(points: EquityCurvePoint[]): EquityChartDatum[] {
  const byTime = new Map<number, EquityChartDatum>();
  for (const point of points) {
    const milliseconds = Date.parse(point.ts);
    if (!Number.isFinite(milliseconds)) continue;
    const time = Math.floor(milliseconds / 1000);
    byTime.set(time, {
      time,
      ts: point.ts,
      equity: point.net_equity,
      drawdown: -Math.abs(point.net_drawdown),
    });
  }
  return [...byTime.values()].sort((left, right) => left.time - right.time);
}
