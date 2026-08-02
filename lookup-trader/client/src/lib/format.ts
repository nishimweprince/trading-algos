export function formatPrice(value: number | null | undefined, digits = 5): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
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

export function toIsoStart(date: string): string {
  return new Date(`${date}T00:00:00Z`).toISOString();
}

export function toIsoEnd(date: string): string {
  return new Date(`${date}T23:59:59Z`).toISOString();
}
