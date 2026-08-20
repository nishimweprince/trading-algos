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
