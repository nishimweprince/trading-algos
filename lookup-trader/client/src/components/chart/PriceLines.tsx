export interface PriceLevels {
  entry: number | null;
  sl: number | null;
  tp: number | null;
}

export const EMPTY_LEVELS: PriceLevels = { entry: null, sl: null, tp: null };

/** Infer side from entry vs target when operator marks levels. */
export function inferSide(entry: number, tp: number): 1 | -1 {
  return tp > entry ? 1 : -1;
}
