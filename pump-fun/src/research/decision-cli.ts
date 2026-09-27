/**
 * Decision model (Jev) research CLI — feat/jev-shadow plan phase 4.
 *
 *   npm run research:decision -- replay [--dry] [--max-calls 500] [--concurrency 4] [--include-v1]
 *                                       [--provider stub|jev] [--db data/scalper.db] [--config config.yaml]
 *   npm run research:decision -- report [--arms veto,confirm_5000] [--provider jev] [--out r.md]
 *                                       [--tp-pct 15 --sl-pct 15 --time-stop-ms 600000 ...]
 *
 * replay: scores history once — every canonical candidate not yet scored by
 *   this provider + model version — through the SAME state builder and
 *   question set as live, writing decision_calls with mode 'replay'. `--dry`
 *   prints one state, the call count and the cost estimate, and calls nothing.
 * report: calibration (Brier / log loss / ECE / reliability, Platt fit) and
 *   the configured gate's policy lift against triple-barrier labels, with the
 *   learned filter on the same rows as the baseline.
 */
import { writeFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import { openDb } from '../persistence/db.ts';
import { Repositories } from '../persistence/repositories.ts';
import { loadConfig } from '../config/load.ts';
import { createDecisionClient } from '../decision/index.ts';
import { EntryDecider } from '../decision/entryDecider.ts';
import { buildEntryState } from '../decision/entryState.ts';
import { buildEntryQuestions } from '../decision/entryQuestions.ts';
import { estimateTokens } from '../decision/types.ts';
import { buildDecisionReport, labelsByMint, loadDecisionRows, loadReplayInputs, renderDecisionReport } from './decisionReport.ts';
import type { BarrierSpec, CostSpec } from './labels.ts';

const cmd = process.argv[2];
const { values } = parseArgs({
  args: process.argv.slice(3),
  options: {
    db: { type: 'string', default: 'data/scalper.db' },
    config: { type: 'string', default: 'config.yaml' },
    provider: { type: 'string' },
    dry: { type: 'boolean', default: false },
    'max-calls': { type: 'string', default: '500' },
    concurrency: { type: 'string', default: '4' },
    'include-v1': { type: 'boolean', default: false },
    arms: { type: 'string', default: 'veto,confirm_5000,confirm_15000' },
    out: { type: 'string' },
    seed: { type: 'string', default: '1' },
    'tp-pct': { type: 'string', default: '15' },
    'sl-pct': { type: 'string', default: '15' },
    'time-stop-ms': { type: 'string', default: '600000' },
    'exit-latency-ms': { type: 'string', default: '1257' },
    'size-sol': { type: 'string', default: '0.04' },
    'tx-cost-sol': { type: 'string', default: '0.0002' },
  },
});

const config = loadConfig({ path: values.config! });
if (values.provider) config.decision.provider = values.provider as typeof config.decision.provider;
const db = openDb({ path: values.db! });
const repos = new Repositories(db);

if (cmd === 'replay') {
  const client = createDecisionClient(config);
  if (!client) {
    console.error('decision.provider is none — pass --provider stub|jev or set it in config');
    process.exit(2);
  }
  // Stub reports its own version; Jev is pinned by config.
  const modelVersion = client.name === 'stub' ? 'stub-v1' : config.decision.jev.model;
  const maxCalls = Number(values['max-calls']);
  const inputs = loadReplayInputs(db, { provider: client.name, modelVersion, includeV1: values['include-v1'], limit: maxCalls });
  const questions = buildEntryQuestions(config);
  const tokens = inputs.reduce((s, x) => s + estimateTokens({ state: buildEntryState(x.input), questions }), 0);
  const usd = (tokens / 1e6) * config.decision.jev.usdPerMInputTokens;
  console.log(`replay: ${inputs.length} canonical candidates to score with ${client.name} ${modelVersion} (cap ${maxCalls})`);
  console.log(`estimated input: ~${tokens} tokens, ~$${usd.toFixed(4)} at $${config.decision.jev.usdPerMInputTokens}/M (output free)`);
  if (values.dry) {
    if (inputs[0]) console.log(`first state (${inputs[0].mint}):\n${JSON.stringify(buildEntryState(inputs[0].input), null, 2)}`);
    process.exit(0);
  }
  const decider = new EntryDecider({ config, repos, client });
  const concurrency = Math.max(1, Number(values.concurrency));
  let next = 0;
  let ok = 0;
  let failed = 0;
  const worker = async () => {
    while (next < inputs.length) {
      const item = inputs[next++]!;
      // Replay has no entry deadline — give the provider room, measure latency honestly.
      const d = await decider.score(item.input, Math.max(config.decision.timeoutMs, 5_000));
      decider.persist(item.mint, d, 'replay');
      if (d.ok) ok++;
      else failed++;
      if (d.error === 'breaker_open') {
        console.error('breaker open — provider failing repeatedly; stopping replay (resume later, scored mints are skipped)');
        next = inputs.length;
      }
    }
  };
  await Promise.all(Array.from({ length: concurrency }, worker));
  console.log(`replay done: ${ok} ok, ${failed} failed`, decider.stats());
} else if (cmd === 'report') {
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
  const arms = values.arms!.split(',').map((a) => a.trim()).filter(Boolean);
  const rows = loadDecisionRows(db, values.provider ? { provider: values.provider } : {});
  const labels = labelsByMint(db, { barrier, cost, arms });
  const report = buildDecisionReport(rows, labels, { gate: config.decision.gate, seed: Number(values.seed) });
  const text = renderDecisionReport(report, { db: values.db!, barrier, arms });
  if (values.out) {
    writeFileSync(values.out, text);
    console.log(`wrote ${values.out}`);
  } else process.stdout.write(text);
} else {
  console.error('usage: decision-cli.ts replay|report [options]');
  process.exit(2);
}
