/**
 * Headless paper-soak / ops report writer.
 * Usage: npm run report:soak -- --range 7d --out reports/
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { loadConfig } from '../config/load.ts';
import { openDb } from '../persistence/db.ts';
import { buildSoakReport, buildTradeBlotterCsv, buildOpsReport } from './queries.ts';

function arg(name: string, fallback?: string): string | undefined {
  const idx = process.argv.indexOf(name);
  if (idx < 0) return fallback;
  return process.argv[idx + 1] ?? fallback;
}

function main(): void {
  const rangeArg = arg('--range', '7d');
  const range =
    rangeArg === '24h' || rangeArg === '7d' || rangeArg === '30d' || rangeArg === 'all' ? rangeArg : '7d';
  const outDir = resolve(arg('--out', 'reports') ?? 'reports');
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  const db = openDb({ path: config.persistence.dbPath });

  mkdirSync(outDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const soak = buildSoakReport(db, config, { range });
  const ops = buildOpsReport(db, config, null);
  const blotter = buildTradeBlotterCsv(db, { range });

  writeFileSync(resolve(outDir, `soak-${range}-${stamp}.json`), JSON.stringify(soak, null, 2));
  writeFileSync(resolve(outDir, `ops-${stamp}.json`), JSON.stringify(ops, null, 2));
  writeFileSync(resolve(outDir, `trades-${range}-${stamp}.csv`), blotter);

  db.close();
  // eslint-disable-next-line no-console
  console.log(`Wrote soak/ops/trades reports to ${outDir} (range=${range}, mode=${config.mode})`);
}

main();
