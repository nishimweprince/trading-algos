import { RR_BUCKETS } from "@/lib/constants";

/** Pip size for common instruments. Falls back to price-digit inference. */
export function pipSize(symbol: string, priceDigits = 5): number {
  const s = symbol.toUpperCase();
  if (s.includes("JPY")) return 0.01;
  if (s === "XAUUSD") return 0.1;
  if (s === "XAGUSD") return 0.01;
  if (s.endsWith("USD") || s.startsWith("EUR") || s.startsWith("GBP") || s.startsWith("AUD")) return 0.0001;
  return 10 ** -priceDigits;
}

export function pipsCaptured(side: 1 | -1, entry: number, price: number, size: number): number {
  if (size <= 0) return 0;
  return ((price - entry) / size) * side;
}

export function rMultiple(side: 1 | -1, entry: number, sl: number, price: number): number {
  const risk = Math.abs(entry - sl) || 1e-9;
  return ((price - entry) / risk) * side;
}

export function riskReward(side: 1 | -1, entry: number, sl: number, tp: number): number {
  const risk = Math.abs(entry - sl) || 1e-9;
  const reward = side === 1 ? tp - entry : entry - tp;
  return reward / risk;
}

/** Mirrors the server's rr_bucket, so a marked trade lands in the bucket its
 *  stored occurrences were filed under. */
export function rrBucket(rr: number): "low" | "standard" | "high" {
  const [low, high] = RR_BUCKETS;
  if (rr < low) return "low";
  if (rr >= high) return "high";
  return "standard";
}
