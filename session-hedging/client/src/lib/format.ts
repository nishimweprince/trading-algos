export function formatMoney(value: number): string {
  const abs = Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (value > 0) return `+${abs}`;
  if (value < 0) return `−${abs}`;
  return abs;
}

export function formatPips(value: number): string {
  return `${formatMoney(value)} pips`;
}

export function formatR(value: number): string {
  return `${formatMoney(value)}R`;
}

export function formatPipsAndR(pips: number, r: number): string {
  return `${formatPips(pips)} · ${formatR(r)}`;
}

export function formatDollars(value: number): string {
  const formatted = formatMoney(value);
  if (formatted.startsWith("+")) return `+$${formatted.slice(1)}`;
  if (formatted.startsWith("−")) return `−$${formatted.slice(1)}`;
  return `$${formatted}`;
}

export function formatPerformance(
  pips: number,
  dollars: number | null,
  unit: "pips" | "dollars",
): string {
  return unit === "dollars" && dollars !== null ? formatDollars(dollars) : formatPips(pips);
}

/** Format an amount already expressed in the report's unit. */
export function formatUnit(value: number, unit: "pips" | "dollars"): string {
  return unit === "dollars" ? formatDollars(value) : formatPips(value);
}

/** The unit-denominated amount beside its R multiple. R is a ratio and never converts. */
export function formatUnitAndR(value: number, r: number, unit: "pips" | "dollars"): string {
  return `${formatUnit(value, unit)} · ${formatR(r)}`;
}

/** A per-side cost in the report's unit, e.g. "3.8p / side" or "$38.00 / side". */
export function formatPerSide(value: number | null, unit: "pips" | "dollars"): string {
  if (value === null) return "—";
  return unit === "dollars"
    ? `${formatDollars(value)} / side`
    : `${value.toFixed(1)}p / side`;
}

export function formatPrice(value: number): string {
  return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function formatPct(value: number | null, digits = 0): string {
  if (value === null) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatPp(value: number | null, digits = 1): string {
  if (value === null) return "—";
  const abs = Math.abs(value).toFixed(digits);
  if (value > 0) return `+${abs} pp`;
  if (value < 0) return `−${abs} pp`;
  return `${abs} pp`;
}

export function formatWhen(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

export function dayStartUtc(date: string): string {
  return `${date}T00:00:00.000Z`;
}

export function dayEndUtc(date: string): string {
  return `${date}T23:59:59.999Z`;
}
