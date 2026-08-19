export function formatMoney(value: number): string {
  const abs = Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (value > 0) return `+${abs}`;
  if (value < 0) return `−${abs}`;
  return abs;
}

export function formatPrice(value: number): string {
  return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function formatPct(value: number | null): string {
  if (value === null) return "—";
  return `${(value * 100).toFixed(0)}%`;
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
