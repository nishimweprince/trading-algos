/** Confluence options for multi-select tagging. */
export const CONFLUENCE_TAGS = [
  "key_level",
  "fibonacci",
  "round_number",
  "moving_average",
  "previous_swing",
  "trendline",
  "supply_demand",
] as const;

export const CONFLUENCE_LABELS: Record<(typeof CONFLUENCE_TAGS)[number], string> = {
  key_level: "Key level",
  fibonacci: "Fibonacci",
  round_number: "Round number",
  moving_average: "Moving average",
  previous_swing: "Previous swing",
  trendline: "Trendline",
  supply_demand: "Supply / demand",
};

export const MARKET_STRUCTURES = ["continuation", "reversal", "range", "breakout"] as const;
export const HTF_ALIGNMENTS = ["aligned", "counter", "neutral"] as const;
export const ENTRY_QUALITIES = ["clean", "messy", "early", "late"] as const;
export const OBSERVED_TRENDS = ["up", "down", "range", "choppy"] as const;

export const MARKET_STRUCTURE_LABELS: Record<(typeof MARKET_STRUCTURES)[number], string> = {
  continuation: "Continuation",
  reversal: "Reversal",
  range: "Range",
  breakout: "Breakout",
};

export const HTF_ALIGNMENT_LABELS: Record<(typeof HTF_ALIGNMENTS)[number], string> = {
  aligned: "Aligned",
  counter: "Counter",
  neutral: "Neutral",
};

export const ENTRY_QUALITY_LABELS: Record<(typeof ENTRY_QUALITIES)[number], string> = {
  clean: "Clean",
  messy: "Messy",
  early: "Early",
  late: "Late",
};

export const OBSERVED_TREND_LABELS: Record<(typeof OBSERVED_TRENDS)[number], string> = {
  up: "Uptrend",
  down: "Downtrend",
  range: "Range",
  choppy: "Choppy",
};
