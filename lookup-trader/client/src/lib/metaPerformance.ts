import { pipsCaptured, pipSize } from "@/lib/pips";

export const META_NET_PIP_COST = 5;

type PipEvent = {
  symbol: string;
  side: 1 | -1;
  entry_price: number | null;
  exit_price: number | null;
};

export function grossPips(event: PipEvent): number | null {
  if (event.entry_price == null || event.exit_price == null) return null;
  return pipsCaptured(event.side, event.entry_price, event.exit_price, pipSize(event.symbol));
}

export function netPips(event: PipEvent, roundTripCost = META_NET_PIP_COST): number | null {
  const gross = grossPips(event);
  return gross == null ? null : gross - roundTripCost;
}
