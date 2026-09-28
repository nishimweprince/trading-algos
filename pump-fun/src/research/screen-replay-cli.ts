/**
 * Offline screening replay CLI (read-only).
 *
 *   npm run research:screen-replay -- [--db data/scalper.db] [--config config.yaml] [--out report.md]
 *
 * Opens the DB read-only (no migrations, no writes), replays the guardrail
 * engine at HEAD over every candidates row and prints the diff report.
 */
import { writeFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { DatabaseSync } from 'node:sqlite';
import { resolve } from 'node:path';
import { loadConfig } from '../config/load.ts';
import { Repositories } from '../persistence/repositories.ts';
import type { DB } from '../persistence/db.ts';
import { renderReplay, replayRow, type ReplayResult, type ReplayRow } from './screenReplay.ts';

const { values } = parseArgs({
  args: process.argv.slice(2),
  options: {
    db: { type: 'string', default: 'data/scalper.db' },
    config: { type: 'string', default: 'config.yaml' },
    out: { type: 'string' },
  },
});

const db = new DatabaseSync(resolve(values.db!), { readOnly: true }) as unknown as DB;
const config = loadConfig({ path: values.config! });
const repos = new Repositories(db);

const rows = db
  .prepare(
    `SELECT c.mint AS mint, c.enrichment_json AS enrichmentJson, c.hard_check_results AS hardCheckResults,
            c.verdict AS verdict, c.primary_veto_code AS primaryVetoCode,
            g.slot AS slot, g.venue AS venue, g.feed_source AS feedSource, g.pool_address AS poolAddress,
            CAST(strftime('%s', g.created_at) AS INTEGER) * 1000 AS detectedAtMs
     FROM candidates c
     LEFT JOIN graduations g ON g.rowid = (SELECT MAX(rowid) FROM graduations WHERE mint = c.mint)
     ORDER BY c.rowid`,
  )
  .all() as unknown as ReplayRow[];

const results: ReplayResult[] = [];
let skipped = 0;
for (const r of rows) {
  const out = replayRow(r, config, repos);
  if (out) results.push(out);
  else skipped += 1;
}
const md = renderReplay(results, skipped);
if (values.out) writeFileSync(values.out, md);
else console.log(md);
