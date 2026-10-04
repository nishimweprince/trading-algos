import type { RunMode } from '../config/schema.ts';

/**
 * SQL fragment (leading " AND ...") restricting `positions` rows to one run
 * mode. Paper, dry-run and live all write the same table, and the dashboard
 * and breakers summed them together — so a live wallet could be judged on
 * dry-run losses. Live rows are mode='live' (or legacy NULL-mode rows that
 * carry an on-chain entry tx) and never simulated.
 *
 * `mode` is a typed enum, never user input. No mode → no filter (callers
 * that do not know the mode keep their old behaviour).
 */
export function positionsModeFilterSql(mode: RunMode | undefined, alias = ''): string {
  if (!mode) return '';
  const c = (col: string) => (alias ? `${alias}.${col}` : col);
  if (mode === 'live') {
    return ` AND (${c('mode')} = 'live' OR (${c('mode')} IS NULL AND ${c('entry_tx')} IS NOT NULL)) AND COALESCE(${c('simulated')}, 0) = 0`;
  }
  return ` AND (${c('mode')} = '${mode}' OR (${c('mode')} IS NULL AND ${c('entry_tx')} IS NULL))`;
}
