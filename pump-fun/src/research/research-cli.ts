/**
 * Research CLIs (work plan 2026-09-25 P3.1 / P3.5 / P3.6):
 *
 *   npm run research:exitgrid -- --db data/scalper.db [--arms veto,confirm_15000] [--seed 1] [--out r.md]
 *   npm run research:buckets  -- --csv trades.csv [--seed 1]
 *   npm run research:gate     -- --phase P2 --csv trades.csv
 *   npm run research:gate     -- --phase P3 --csv oos-trades.csv --capital 1 --trials 36
 *   npm run research:gate     -- --phase P4 --db data/scalper.db
 *
 * Gates exit 0 when met and 1 when not, so they can guard a deploy step.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { openDb } from '../persistence/db.ts';
import { loadTrades, recostTrade, type Trade } from './trades.ts';
import { parseCsv, num } from './csv.ts';
import { bootstrapCI, mean } from './stats.ts';
import { defaultGrid, runExitGrid, type PathSample } from './exitgrid.ts';
import { gateP2, gateP3, gateP4, renderGate, type GateTrade } from './gate.ts';
import { getExecutionDragComparison } from '../dashboard/queries.ts';

const cmd = process.argv[2];
const { values } = parseArgs({
  args: process.argv.slice(3),
  options: {
    db: { type: 'string', default: 'data/scalper.db' },
    csv: { type: 'string' },
    arms: { type: 'string' },
    seed: { type: 'string', default: '1' },
    out: { type: 'string' },
    phase: { type: 'string' },
    capital: { type: 'string', default: '1' },
    trials: { type: 'string', default: '1' },
    'logged-swap-fee-pct': { type: 'string' },
    'exit-latency-ms': { type: 'string', default: '1257' },
    'size-sol': { type: 'string', default: '0.04' },
    'tx-cost-sol': { type: 'string', default: '0.0002' },
  },
});
const seed = Number(values.seed);
const emit = (text: string) => (values.out ? (writeFileSync(values.out, text), console.log(`wrote ${values.out}`)) : process.stdout.write(text));

/**
 * Post-P1 exports carry fee_tier_bps and are already charged real tiers; older
 * ones are re-costed from the flat 0.25 %/leg they were logged with.
 */
function netOf(csvText: string): { trades: Trade[]; net: (t: Trade) => number } {
  const trades = loadTrades(csvText);
  const tiered = trades.some((t) => t.feeTierBps !== null);
  const logged = values['logged-swap-fee-pct'] !== undefined ? Number(values['logged-swap-fee-pct']) : tiered ? null : 0.25;
  return { trades, net: (t) => (logged === null ? t.netPnlSol : recostTrade(t, { loggedSwapFeePct: logged }).netPnlSol) };
}

function toGate(trades: readonly Trade[], net: (t: Trade) => number): GateTrade[] {
  return trades.map((t) => ({
    mint: t.mint,
    sizeSol: t.sizeSol,
    netPnlSol: net(t),
    feesSol: t.feesSol + (t.netPnlSol - net(t)),
    exitReason: t.exitReason,
    relaxed: t.relaxed,
  }));
}

if (cmd === 'exitgrid') {
  const db = openDb({ path: values.db! });
  const arms = values.arms?.split(',');
  const pairs = db.prepare(`SELECT mint, arm, MIN(created_at) AS c FROM path_ticks GROUP BY mint, arm`).all() as Array<{ mint: string; arm: string; c: string }>;
  const tick = db.prepare(`SELECT t_ms AS tMs, price FROM path_ticks WHERE mint = ? AND arm = ? ORDER BY t_ms`);
  const samples: PathSample[] = pairs
    .filter((p) => !arms || arms.includes(p.arm))
    .map((p) => ({ mint: p.mint, arm: p.arm, t: Date.parse(`${p.c.replace(' ', 'T')}Z`), path: tick.all(p.mint, p.arm) as Array<{ tMs: number; price: number }> }))
    .filter((s) => s.path.length >= 5);
  const r = runExitGrid(samples, defaultGrid(), {
    exitLatencyMs: Number(values['exit-latency-ms']),
    sizeSol: Number(values['size-sol']),
    txCostSol: Number(values['tx-cost-sol']),
  }, seed);
  const p = (x: number) => (Number.isFinite(x) ? `${(x * 100).toFixed(2)} %` : '—');
  const lines = [
    `# Exit grid (walk-forward) — ${new Date().toISOString().slice(0, 10)}`,
    '',
    `${r.samples} paths over ${r.days} days · ${r.configs} configurations · honest simulator (exit latency ${values['exit-latency-ms']} ms, worst-in-window stops, real tier fees)`,
    '',
    `| Metric | Value |`,
    `|---|---|`,
    `| In-sample best (NOT a recommendation) | ${r.inSampleBest ? `${r.inSampleBest.name}: ${p(r.inSampleBest.meanNet)}` : '—'} |`,
    `| Walk-forward OOS | ${r.walkForward ? `${r.walkForward.trades} trades, mean ${p(r.walkForward.meanNet)} [${p(r.walkForward.ci.lo)}, ${p(r.walkForward.ci.hi)}], PF ${r.walkForward.profitFactor.toFixed(2)}` : 'needs ≥ 2 days of paths'} |`,
    `| Deflated Sharpe | ${Number.isFinite(r.dsr) ? r.dsr.toFixed(3) : '—'} |`,
    `| PBO (CSCV) | ${Number.isFinite(r.pbo) ? r.pbo.toFixed(3) : '—'} |`,
    '',
    '## Walk-forward picks',
    '',
    ...(r.walkForward?.picks.map((x) => `- ${x.day}: ${x.name}`) ?? ['- none']),
    '',
    '## All configurations (in-sample, for inspection only)',
    '',
    '| Config | n | mean net | win rate |',
    '|---|---|---|---|',
    ...r.table.map((row) => `| ${row.name} | ${row.n} | ${p(row.meanNet)} | ${p(row.winRate)} |`),
    '',
    'Adopt a configuration only when the walk-forward CI lower bound > 0 after fees and PBO < 0.5 (P3.6).',
    '',
  ];
  emit(lines.join('\n'));
} else if (cmd === 'buckets') {
  if (!values.csv) throw new Error('--csv required');
  const text = readFileSync(values.csv, 'utf8');
  const { trades, net } = netOf(text);
  const windows = new Map<string, number[]>();
  const rows = parseCsv(text).filter((r) => (r.state ?? 'CLOSED') === 'CLOSED');
  trades.forEach((t, i) => {
    const w = num(rows[i]?.momentum_window_ms);
    const key = w === null ? 'unknown' : String(w);
    windows.set(key, [...(windows.get(key) ?? []), (net(t) / t.sizeSol) * 100]);
  });
  const stats = [...windows.entries()]
    .map(([w, r]) => ({ w, n: r.length, ci: bootstrapCI(r, { seed, iterations: 5_000 }) }))
    .sort((a, b) => Number(a.w) - Number(b.w));
  const zero = stats.find((s) => s.w === '0');
  const lines = [
    '# Momentum window buckets (P3.1)',
    '',
    `Source \`${values.csv}\` · net %/trade at real fees · a window is kept only if its CI lies entirely above window 0's.`,
    '',
    '| Window ms | n | mean net % | 95 % CI | beats 0 (non-overlapping) |',
    '|---|---|---|---|---|',
    ...stats.map((s) => `| ${s.w} | ${s.n} | ${s.ci.point.toFixed(2)} | [${s.ci.lo.toFixed(2)}, ${s.ci.hi.toFixed(2)}] | ${zero && s.w !== '0' ? (s.ci.lo > zero.ci.hi ? '✅' : '—') : ''} |`),
    '',
  ];
  emit(lines.join('\n'));
} else if (cmd === 'gate') {
  let g;
  if (values.phase === 'P2' || values.phase === 'P3') {
    if (!values.csv) throw new Error('--csv required for P2/P3');
    const { trades, net } = netOf(readFileSync(values.csv, 'utf8'));
    const gt = toGate(trades, net);
    g = values.phase === 'P2' ? gateP2(gt) : gateP3(gt, { capitalSol: Number(values.capital), trials: Number(values.trials), seed });
  } else if (values.phase === 'P4') {
    const db = openDb({ path: values.db! });
    const live = db
      .prepare(
        `SELECT mint, size_sol AS sizeSol, COALESCE(net_pnl_sol, pnl_sol) AS netPnlSol, COALESCE(fees_sol, 0) AS feesSol,
                exit_reason AS exitReason, relaxed_risk AS relaxed
         FROM positions WHERE rowid IN (SELECT MAX(rowid) FROM positions GROUP BY mint) AND state = 'CLOSED' AND mode = 'live'`,
      )
      .all() as unknown as Array<Omit<GateTrade, 'relaxed'> & { relaxed: number }>;
    const counts = db
      .prepare(
        `SELECT SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END) AS failed, COUNT(*) AS attempted
         FROM positions WHERE rowid IN (SELECT MAX(rowid) FROM positions GROUP BY mint) AND mode = 'live'`,
      )
      .get() as { failed: number | null; attempted: number };
    const drag = getExecutionDragComparison(db, { range: 'all' });
    const both = drag.rows.filter((r) => r.cohort === 'both' && r.liveNetPnlSol !== null && r.dryNetPnlSol !== null && r.liveSizeSol);
    const dragPct = both.length ? mean(both.map((r) => ((r.netPnlDeltaSol ?? 0) / (r.liveSizeSol ?? 1)) * 100)) : null;
    g = gateP4(live.map((t) => ({ ...t, relaxed: Boolean(t.relaxed) })), {
      failedEntries: counts.failed ?? 0,
      attemptedEntries: counts.attempted,
      dragPctPerTrade: dragPct,
    });
  } else {
    throw new Error('--phase P2|P3|P4');
  }
  emit(renderGate(g));
  process.exit(g.met ? 0 : 1);
} else {
  console.error('usage: research-cli.ts exitgrid|buckets|gate ...');
  process.exit(2);
}
