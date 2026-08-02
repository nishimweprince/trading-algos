export interface PriceLevels {
  entry: number | null;
  sl: number | null;
  tp: number | null;
}

export const EMPTY_LEVELS: PriceLevels = { entry: null, sl: null, tp: null };

/** Which level the operator is currently placing from the chart. */
export type PriceLevelKey = keyof PriceLevels;

export const LEVEL_LABELS: Record<PriceLevelKey, string> = {
  entry: "Entry",
  sl: "Stop",
  tp: "Target",
};

/** A level picked off the chart, on its way into the trade form. */
export interface LevelPick {
  field: PriceLevelKey;
  price: number;
  /** Makes two picks of the same price distinct, so the form re-applies it. */
  nonce: number;
}

/** Infer side from entry vs target when operator marks levels. */
export function inferSide(entry: number, tp: number): 1 | -1 {
  return tp > entry ? 1 : -1;
}
