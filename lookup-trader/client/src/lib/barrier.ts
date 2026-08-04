import type { Candle } from "@/types";

export type BarrierHit = "tp" | "sl" | "both" | null;
export type LiveResult = "win" | "loss" | "timeout";

export function checkBar(
  bar: Candle,
  { side, sl, tp }: { side: 1 | -1; sl: number; tp: number },
): BarrierHit {
  const hitTp = side === 1 ? bar.high >= tp : bar.low <= tp;
  const hitSl = side === 1 ? bar.low <= sl : bar.high >= sl;
  if (hitTp && hitSl) return "both";
  if (hitTp) return "tp";
  if (hitSl) return "sl";
  return null;
}

export function resolveHit(
  hit: BarrierHit,
  policy: "conservative" | "drop" | "optimistic" = "conservative",
): LiveResult | null {
  if (hit === "tp") return "win";
  if (hit === "sl") return "loss";
  if (hit === "both") {
    return { conservative: "loss", drop: null, optimistic: "win" }[policy] as LiveResult | null;
  }
  return null;
}

export function exitPriceFromResult(
  result: LiveResult,
  _side: 1 | -1,
  _entry: number,
  sl: number,
  tp: number,
  bar: Candle,
): number {
  if (result === "win") return tp;
  if (result === "loss") return sl;
  return bar.close;
}
