import type { OutcomeDirection } from "@/types";

function probability(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

export function formatShadowDirection(result: OutcomeDirection): string {
  return `${result.direction === "long" ? "Long" : "Short"}  W ${probability(result.p_win)}  L ${probability(result.p_loss)}  T ${probability(result.p_timeout)}`;
}
