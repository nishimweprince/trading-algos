export interface PriceLevels {
  entry: number | null;
  sl: number | null;
  tp: number | null;
}

/** Which level the operator is currently placing from the chart. */
export type PriceLevelKey = keyof PriceLevels;

export const LEVEL_LABELS: Record<PriceLevelKey, string> = {
  entry: "Entry",
  sl: "Stop",
  tp: "Target",
};

/** Infer side from entry vs target when operator marks levels. */
export function inferSide(entry: number, tp: number): 1 | -1 {
  return tp > entry ? 1 : -1;
}
