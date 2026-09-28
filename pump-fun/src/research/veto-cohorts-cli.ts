/**
 * Veto-cohort evidence CLI.
 *
 *   npm run research:vetoes -- [--db data/scalper.db] [--range 7d|30d|all]
 *                            [--version exit_fsm_v3_amm] [--out report.md] [--seed 1]
 *
 * Reads shadow_outcomes joined to the latest candidates row per mint and
 * renders the cohort markdown to stdout (or --out). Run on the server DB
 * once v3 outcomes accumulate; a threshold change needs a flagged cohort.
 */
import { writeFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { openDb } from '../persistence/db.ts';
import {
  buildReport,
  h12ReasonFromHardChecks,
  renderMarkdown,
  type VetoRow,
} from './vetoCohorts.ts';

const { values } = parseArgs({
  args: process.argv.slice(2),
  options: {
    db: { type: 'string', default: 'data/scalper.db' },
    range: { type: 'string', default: '7d' },
    version: { type: 'string', default: 'exit_fsm_v3_amm' },
    out: { type: 'string' },
    seed: { type: 'string', default: '1' },
  },
});

function rangeCutoff(range: string): string | null {
  if (range === 'all') return null;
  const m = /^(\d+)d$/.exec(range);
  if (!m) throw new Error(`--range must be Nd or all (got ${range})`);
  return `datetime('now','-${Number(m[1])} days')`;
}

function loadRows(db: ReturnType<typeof openDb>, version: string, range: string): VetoRow[] {
  const cutoff = rangeCutoff(range);
  const rows = db
    .prepare(
      `SELECT s.mint AS mint, s.primary_veto_code AS primaryVetoCode,
              s.veto_codes_json AS vetoCodesJson, s.net_pnl_sol AS netPnlSol,
              s.pnl_pct AS pnlPct, s.exit_reason AS exitReason,
              c.pool_sol_at_entry AS poolSol, c.hard_check_results AS hardChecks
       FROM shadow_outcomes s
       LEFT JOIN candidates c ON c.rowid = (SELECT MAX(rowid) FROM candidates WHERE mint = s.mint)
       WHERE s.outcome_version = ? AND s.verdict = 'veto'
       ${cutoff ? `AND s.created_at >= ${cutoff}` : ''}`,
    )
    .all(version) as Array<{
    mint: string;
    primaryVetoCode: string | null;
    vetoCodesJson: string | null;
    netPnlSol: number | null;
    pnlPct: number | null;
    exitReason: string | null;
    poolSol: number | null;
    hardChecks: string | null;
  }>;
  return rows.map((r) => {
    let vetoCodes: string[] = [];
    try {
      const parsed: unknown = r.vetoCodesJson ? JSON.parse(r.vetoCodesJson) : [];
      if (Array.isArray(parsed)) vetoCodes = parsed.filter((x): x is string => typeof x === 'string');
    } catch {
      vetoCodes = [];
    }
    if (vetoCodes.length === 0 && r.primaryVetoCode) vetoCodes = [r.primaryVetoCode];
    return {
      mint: r.mint,
      primaryVetoCode: r.primaryVetoCode,
      vetoCodes,
      h12Reason: h12ReasonFromHardChecks(r.hardChecks),
      poolSol: r.poolSol,
      netPnlSol: r.netPnlSol,
      pnlPct: r.pnlPct,
      exitReason: r.exitReason,
    };
  });
}

const db = openDb({ path: values.db! });
try {
  const rows = loadRows(db, values.version!, values.range!);
  const report = buildReport(rows, { outcomeVersion: values.version!, range: values.range!, seed: Number(values.seed) });
  const text = renderMarkdown(report);
  if (values.out) {
    writeFileSync(values.out, text);
    console.log(`wrote ${values.out}`);
  } else {
    process.stdout.write(text);
  }
} finally {
  db.close();
}
