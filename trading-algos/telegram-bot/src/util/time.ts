export function toIso(d: Date = new Date()): string {
  return d.toISOString();
}

export function formatSmsUtc(d: Date): string {
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const min = String(d.getUTCMinutes()).padStart(2, '0');
  return `${mm} ${hh}:${min}Z`;
}
