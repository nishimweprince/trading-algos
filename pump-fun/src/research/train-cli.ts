/**
 * npm run research:train -- [--db data/scalper.db] [--arms confirm_15000,veto]
 *   [--min-samples 300] [--out-model models/meta-<date>.json] [--report reports/model-<date>.md]
 *   [--seed 1] [--force]
 *
 * Builds triple-barrier labels from path_ticks (honest simulator: exit
 * latency, worst-in-window stops, real tier fees), trains the L2-logistic
 * meta-model under purged/embargoed k-fold, and writes the model JSON plus a
 * markdown report with DSR / PBO / walk-forward. Below --min-samples it
 * writes the report only (no model) and exits 3, unless --force.
 */
import { mkdirSync, writeFileSync, copyFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { parseArgs } from 'node:util';
import { openDb } from '../persistence/db.ts';
import { buildDataset } from './dataset.ts';
import { trainMetaModel, type TrainResult } from './train.ts';
import { FEATURE_NAMES } from './featureSpec.ts';
import type { BarrierSpec, CostSpec } from './labels.ts';

const { values } = parseArgs({
  options: {
    db: { type: 'string', default: 'data/scalper.db' },
    arms: { type: 'string' },
    'min-samples': { type: 'string', default: '300' },
    'out-model': { type: 'string' },
    report: { type: 'string' },
    seed: { type: 'string', default: '1' },
    'tp-pct': { type: 'string', default: '15' },
    'sl-pct': { type: 'string', default: '15' },
    'time-stop-ms': { type: 'string', default: '600000' },
    'exit-latency-ms': { type: 'string', default: '1257' },
    'size-sol': { type: 'string', default: '0.04' },
    'tx-cost-sol': { type: 'string', default: '0.0002' },
    force: { type: 'boolean', default: false },
  },
});

const today = new Date().toISOString().slice(0, 10);
const barrier: BarrierSpec = {
  mode: 'fixed',
  tpPct: Number(values['tp-pct']),
  slPct: Number(values['sl-pct']),
  timeStopMs: Number(values['time-stop-ms']),
};
const cost: CostSpec = {
  exitLatencyMs: Number(values['exit-latency-ms']),
  sizeSol: Number(values['size-sol']),
  txCostSol: Number(values['tx-cost-sol']),
};
const db = openDb({ path: values.db! });
const samples = buildDataset(db, {
  barrier,
  cost,
  canonicalOnly: true,
  ...(values.arms ? { arms: values.arms.split(',') } : {}),
});
const minSamples = Number(values['min-samples']);
const result = trainMetaModel(samples, {
  lambdas: [0.001, 0.01, 0.1, 1],
  thresholds: [0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7],
  folds: 5,
  embargoMs: 30 * 60_000,
  seed: Number(values.seed),
  minTaken: Math.max(30, Math.floor(samples.length * 0.1)),
});

const reportPath = values.report ?? `reports/model-${today}.md`;
mkdirSync(dirname(reportPath), { recursive: true });
writeFileSync(reportPath, renderReport(result, { db: values.db!, barrier, cost, minSamples }));
console.log(`wrote ${reportPath} (n=${result.n})`);

if (!result.model || (result.n < minSamples && !values.force)) {
  console.error(`not enough labelled samples for a model (${result.n} < ${minSamples}) — no model written`);
  process.exit(3);
}
const version = `meta-${today}-n${result.n}`;
const modelPath = values['out-model'] ?? `models/${version}.json`;
mkdirSync(dirname(modelPath), { recursive: true });
writeFileSync(
  modelPath,
  JSON.stringify({ version, createdAt: new Date().toISOString(), ...result.model, trainedOn: { n: result.n, barrier, cost }, oos: result.oos }, null, 2),
);
copyFileSync(modelPath, 'models/meta-latest.json');
console.log(`wrote ${modelPath} and models/meta-latest.json`);

function renderReport(r: TrainResult, ctx: { db: string; barrier: BarrierSpec; cost: CostSpec; minSamples: number }): string {
  const pct = (x: number) => (Number.isFinite(x) ? `${(x * 100).toFixed(2)} %` : '—');
  const lines = [
    `# Meta-model training — ${today}`,
    '',
    `DB \`${ctx.db}\` · labels: triple barrier +${ctx.barrier.tpPct} % / −${ctx.barrier.slPct} % / ${ctx.barrier.timeStopMs / 1000} s, exit latency ${ctx.cost.exitLatencyMs} ms (worst-in-window stops), real tier fees, tx ${ctx.cost.txCostSol} SOL @ ${ctx.cost.sizeSol} SOL · ${FEATURE_NAMES.length} feature columns`,
    '',
    `| Metric | Value |`,
    `|---|---|`,
    `| Labelled samples | ${r.n} (gate needs ≥ ${ctx.minSamples}) |`,
    `| Base rate (profitable after costs) | ${pct(r.baseRate)} |`,
    `| Take-everything mean net | ${pct(r.baselineMeanNet)} |`,
  ];
  if (r.oos && r.chosen) {
    lines.push(
      `| Chosen λ / threshold | ${r.chosen.lambda} / ${r.chosen.threshold} |`,
      `| OOS trades taken | ${r.oos.taken} |`,
      `| OOS mean net, 95 % CI | ${pct(r.oos.meanNet)} [${pct(r.oos.ci.lo)}, ${pct(r.oos.ci.hi)}] |`,
      `| OOS profit factor | ${r.oos.profitFactor.toFixed(2)} |`,
      `| OOS log loss | ${r.oos.logLoss.toFixed(4)} |`,
      `| Deflated Sharpe (${r.variants.length} variants) | ${r.oos.dsr.toFixed(3)} (SR₀ ${r.oos.sr0.toFixed(3)}) |`,
      `| PBO (CSCV) | ${r.oos.pbo.toFixed(3)} |`,
      `| Walk-forward by day | ${r.oos.walkForward.folds} folds, ${r.oos.walkForward.taken} taken, mean ${pct(r.oos.walkForward.meanNet)} |`,
    );
  } else {
    lines.push(`| Result | no eligible variant (too few samples / trades) |`);
  }
  lines.push('', '## Variants (purged 5-fold OOS)', '', '| λ | threshold | taken | mean net | Sharpe |', '|---|---|---|---|---|');
  for (const v of r.variants) lines.push(`| ${v.lambda} | ${v.threshold} | ${v.taken} | ${pct(v.meanNet)} | ${Number.isFinite(v.sharpe) ? v.sharpe.toFixed(3) : '—'} |`);
  if (r.calibration.length) {
    lines.push('', '## Calibration (OOS)', '', '| p bin | n | mean p | observed |', '|---|---|---|---|');
    for (const b of r.calibration) if (b.n) lines.push(`| ${b.lo.toFixed(1)}–${b.hi.toFixed(1)} | ${b.n} | ${b.meanP.toFixed(3)} | ${b.freq.toFixed(3)} |`);
  }
  if (r.model) {
    const w = r.model.featureNames.map((f, i) => ({ f, w: r.model!.weights[i]! })).sort((a, b) => Math.abs(b.w) - Math.abs(a.w)).slice(0, 15);
    lines.push('', '## Largest standardized weights', '', '| feature | weight |', '|---|---|', ...w.map((x) => `| ${x.f} | ${x.w.toFixed(4)} |`));
  }
  lines.push('', 'Adopt only if the OOS CI lower bound > 0, DSR > 0.95 and PBO < 0.5 (work plan P3.6). Until then the model runs in shadow (`model.enabled: false`).', '');
  return lines.join('\n');
}
