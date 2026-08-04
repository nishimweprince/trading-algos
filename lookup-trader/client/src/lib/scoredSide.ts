/** Infer which direction the context base rate should score when side is not explicit. */

export function inferScoredSide(
  options: {
    formSide?: string;
    entry: number | null;
    tp: number | null;
    trendState?: string | null;
  },
  anySentinel = "__any",
): 1 | -1 {
  if (options.formSide && options.formSide !== anySentinel) {
    return Number(options.formSide) as 1 | -1;
  }
  if (options.entry != null && options.tp != null) {
    return options.tp > options.entry ? 1 : -1;
  }
  if (options.trendState === "down") return -1;
  if (options.trendState === "up") return 1;
  return 1;
}
