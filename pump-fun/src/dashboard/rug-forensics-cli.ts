/**
 * Rug forensics CLI (LIVE_PILOT_PLAN §4 S3).
 *   npm run report:rugs -- --track dry --range 7d --rug-pct -80 --out reports/rugs
 *
 * Run it where the trades live (the server DB, or a copy pointed at with
 * CONFIG_PATH / persistence.dbPath). Read-only.
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { loadConfig } from '../config/load.ts';
import { openDb } from '../persistence/db.ts';
import { buildRugForensics, renderRugForensicsMarkdown, type ForensicsTrack } from './rugForensics.ts';

function arg(name: string, fallback?: string): string | undefined {
  const idx = process.argv.indexOf(name);
  if (idx < 0) return fallback;
  return process.argv[idx + 1] ?? fallback;
}

function main(): void {
  const track: ForensicsTrack = arg('--track', 'dry') === 'live' ? 'live' : 'dry';
  const range = arg('--range', '7d') ?? '7d';
  const rugPnlPct = Number(arg('--rug-pct', '-80'));
  const outDir = resolve(arg('--out', 'reports/rugs') ?? 'reports/rugs');
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  const db = openDb({ path: config.persistence.dbPath });

  const report = buildRugForensics(db, { track, range, rugPnlPct });
  mkdirSync(outDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  writeFileSync(resolve(outDir, `rug-forensics-${track}-${range}-${stamp}.json`), JSON.stringify(report, null, 2));
  const md = renderRugForensicsMarkdown(report);
  writeFileSync(resolve(outDir, 'SUMMARY.md'), md);
  // eslint-disable-next-line no-console
  console.log(md);
  db.close();
}

main();
