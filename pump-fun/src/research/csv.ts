/**
 * Minimal RFC-4180 CSV reader/writer for research CLIs (quoted fields,
 * doubled quotes, embedded commas/newlines). The trade blotter writes JSON
 * arrays into cells ("[""relaxed_h5_top10""]"), so naive split(',') breaks.
 */
export function parseCsv(text: string): Array<Record<string, string>> {
  const rows = parseRows(text);
  const header = rows.shift();
  if (!header) return [];
  return rows
    .filter((r) => !(r.length === 1 && r[0] === ''))
    .map((r) => Object.fromEntries(header.map((h, i) => [h, r[i] ?? ''])));
}

export function parseRows(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!;
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else quoted = false;
      } else field += ch;
      continue;
    }
    if (ch === '"') quoted = true;
    else if (ch === ',') {
      row.push(field);
      field = '';
    } else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
    } else field += ch;
  }
  if (field !== '' || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

export function toCsv(headers: readonly string[], rows: ReadonlyArray<Record<string, unknown>>): string {
  const esc = (v: unknown): string => {
    if (v === null || v === undefined) return '';
    const s = String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [headers.join(','), ...rows.map((r) => headers.map((h) => esc(r[h])).join(','))].join('\n') + '\n';
}

/** Number or null for empty / non-numeric cells. */
export function num(v: string | undefined): number | null {
  if (v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
