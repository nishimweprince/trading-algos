export function formatPrice(value: number | null | undefined, digits = 5): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

/** Widest decimal place seen in a sample of closes — 5 for FX, 2 for indices. */
const DIGITS_SAMPLE = 50;
const MAX_DIGITS = 5;

export function inferPriceDigits(prices: { close: number }[]): number {
  let digits = 0;
  for (const { close } of prices.slice(0, DIGITS_SAMPLE)) {
    const decimals = String(close).split(".")[1];
    if (decimals) digits = Math.max(digits, decimals.length);
    if (digits >= MAX_DIGITS) return MAX_DIGITS;
  }
  // No data yet: assume the finest instrument rather than rounding prices away.
  return prices.length === 0 ? MAX_DIGITS : digits;
}

export function formatPercent(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

export function formatTs(ts: string | null | undefined, blinded = false): string {
  if (!ts || blinded) return "•••";
  const d = new Date(ts);
  return d.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}

/**
 * Calendar date of a picked Date as `yyyy-MM-dd`, read off the local calendar so
 * the day the operator clicked is the day that gets sent. Feeds toIsoStart/toIsoEnd,
 * which anchor it to UTC — candles are UTC everywhere.
 */
export function toDateKey(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

export function toIsoStart(date: string): string {
  return new Date(`${date}T00:00:00Z`).toISOString();
}

export function toIsoEnd(date: string): string {
  return new Date(`${date}T23:59:59Z`).toISOString();
}

/** Normalize any parseable timestamp to UTC ISO-8601 with Z suffix. */
export function toUtcIso(ts: string | Date): string {
  return new Date(ts).toISOString();
}
