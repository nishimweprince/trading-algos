/**
 * Headless reports.
 *   npm run report:soak -- --range 7d --out reports/
 *   npm run report:strategy -- --range 7d --out reports/strategy-week
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { loadConfig } from '../config/load.ts';
import { openDb } from '../persistence/db.ts';
import { buildSoakReport, buildTradeBlotterCsv, buildOpsReport } from './queries.ts';
import {
  buildStrategyWeekReport,
  listStrategyTrades,
  renderStrategyWeekMarkdown,
  strategyFillsCsv,
  strategyTradesCsv,
} from './strategyReport.ts';

function arg(name: string, fallback?: string): string | undefined {
  const idx = process.argv.indexOf(name);
  if (idx < 0) return fallback;
  return process.argv[idx + 1] ?? fallback;
}

function parseRange(raw: string | undefined): '24h' | '7d' | '30d' | 'all' {
  return raw === '24h' || raw === '7d' || raw === '30d' || raw === 'all' ? raw : '7d';
}

function main(): void {
  const kind = process.env.REPORT_KIND === 'strategy' || process.argv.includes('--strategy') ? 'strategy' : 'soak';
  const range = parseRange(arg('--range', '7d'));
  const outDir = resolve(arg('--out', kind === 'strategy' ? 'reports/strategy-week' : 'reports') ?? 'reports');
  const allowMixed = process.argv.includes('--allow-mixed-modes');
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  const db = openDb({ path: config.persistence.dbPath });

  mkdirSync(outDir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');

  if (kind === 'strategy') {
    const report = buildStrategyWeekReport(db, config, {
      range,
      mode: allowMixed ? null : config.mode,
      allowMixedModes: allowMixed,
    });
    const trades = listStrategyTrades(db, range, allowMixed ? null : config.mode);
    writeFileSync(resolve(outDir, `strategy-week-${range}-${stamp}.json`), JSON.stringify(report, null, 2));
    writeFileSync(resolve(outDir, 'SUMMARY.md'), renderStrategyWeekMarkdown(report));
    writeFileSync(resolve(outDir, `trades-${range}-${stamp}.csv`), strategyTradesCsv(trades));
    writeFileSync(resolve(outDir, `fills-${range}-${stamp}.csv`), strategyFillsCsv(db, range));
    writeFileSync(resolve(outDir, `strata-${range}-${stamp}.json`), JSON.stringify(report.strata, null, 2));
    writeFileSync(resolve(outDir, 'config-sessions.json'), JSON.stringify(report.configSessions, null, 2));
    // eslint-disable-next-line no-console
    console.log(
      `Strategy week package written to ${outDir} (range=${range}, mode=${config.mode}, n=${report.headline.tradeCount})`,
    );
  } else {
    const soak = buildSoakReport(db, config, { range });
    const ops = buildOpsReport(db, config, null);
    const blotter = buildTradeBlotterCsv(db, { range });
    writeFileSync(resolve(outDir, `soak-${range}-${stamp}.json`), JSON.stringify(soak, null, 2));
    writeFileSync(resolve(outDir, `ops-${stamp}.json`), JSON.stringify(ops, null, 2));
    writeFileSync(resolve(outDir, `trades-${range}-${stamp}.csv`), blotter);
    // eslint-disable-next-line no-console
    console.log(`Wrote soak/ops/trades reports to ${outDir} (range=${range}, mode=${config.mode})`);
  }

  db.close();
}

main();
