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

export const OBSERVED_RESULTS = ["win", "loss", "timeout", "unsure"] as const;

export const OBSERVED_RESULT_LABELS: Record<(typeof OBSERVED_RESULTS)[number], string> = {
  win: "Win",
  loss: "Loss",
  timeout: "Timed out",
  unsure: "Unsure",
};

/**
 * Why a setup you spotted was not taken. Every skip still names the setup — a
 * skip is "I saw this and passed", which is the negative example worth having.
 * Bars with nothing on them are not recorded one at a time.
 */
export const SKIP_REASONS = [
  "setup_poor_location",
  "wrong_session",
  "low_conviction",
  "news_risk",
  "rr_too_low",
  "missed_entry",
] as const;

export const SKIP_REASON_LABELS: Record<(typeof SKIP_REASONS)[number], string> = {
  setup_poor_location: "Poor location",
  wrong_session: "Wrong session",
  low_conviction: "Low conviction",
  news_risk: "News risk",
  rr_too_low: "R:R too low",
  missed_entry: "Missed entry",
};
