import { describe, it, expect } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { brier, ece, fitPlatt } from '../src/research/logreg.ts';
import { buildDecisionReport, labelsByMint, loadDecisionRows, loadReplayInputs, renderDecisionReport } from '../src/research/decisionReport.ts';
import { EntryDecider } from '../src/decision/entryDecider.ts';
import { StubDecisionClient } from '../src/decision/stubClient.ts';
import { recalibrate } from '../src/decision/policy.ts';
import type { CandidateVerdict } from '../src/core/types.ts';
import type { DecisionClient, DecisionResult } from '../src/decision/types.ts';
import type { BarrierSpec, CostSpec } from '../src/research/labels.ts';

const BARRIER: BarrierSpec = { mode: 'fixed', tpPct: 15, slPct: 15, timeStopMs: 600_000 };
const COST: CostSpec = { exitLatencyMs: 0, sizeSol: 0.04, txCostSol: 0 };

describe('calibration maths', () => {
  it('Brier and ECE on known vectors', () => {
    expect(brier([1, 0, 0.5], [1, 0, 1])).toBeCloseTo(0.25 / 3, 12);
    expect(brier([], [])).toBeNaN();
    // Perfectly calibrated bins -> ECE 0; always-0.9 on a 50 % base rate -> 0.4.
    expect(ece([0.25, 0.25, 0.25, 0.25], [1, 0, 0, 0])).toBeCloseTo(0, 12);
    expect(ece([0.9, 0.9, 0.9, 0.9], [1, 0, 1, 0])).toBeCloseTo(0.4, 12);
  });

  it('Platt fit recovers an overconfident model and is ~identity on a calibrated one', () => {
    // Overconfident: says 0.9 / 0.1, truth is 70 % / 30 %.
    const p: number[] = [];
    const y: number[] = [];
    for (let i = 0; i < 100; i++) {
      p.push(0.9, 0.1);
      y.push(i < 70 ? 1 : 0, i < 30 ? 1 : 0);
    }
    const cal = fitPlatt(p, y);
    expect(recalibrate(0.9, cal)).toBeCloseTo(0.7, 3);
    expect(recalibrate(0.1, cal)).toBeCloseTo(0.3, 3);
  });
});

/** Answers continuation = a fixed prob per mint, so the report has something real to measure. */
class ScriptedClient implements DecisionClient {
  readonly name = 'scripted';
  private readonly byNet: (state: object) => number;
  constructor(byNet: (state: object) => number) {
    this.byNet = byNet;
  }
  async decide(state: object): Promise<DecisionResult> {
    const p = this.byNet(state);
    return {
      provider: this.name,
      modelVersion: 's-1',
      latencyMs: 5,
      inputTokens: 100,
      answers: [
        { id: 'continuation', value: p, confidence: 1 },
        { id: 'toxic_flow', value: 1 - p, confidence: 1 },
        { id: 'rug_risk', value: 0.1, confidence: 1 },
        { id: 'setup_quality', value: 3, confidence: 1 },
      ],
    };
  }
}

function seed(n: number) {
  const db = openDb({ path: ':memory:', memory: true });
  const repos = new Repositories(db);
  for (let i = 0; i < n; i++) {
    const mint = `M${i}`;
    const winner = i % 2 === 0;
    const v: CandidateVerdict = {
      mint,
      verdict: 'accept',
      hardChecks: [{ id: 'H12', status: 'pass', detail: '' }] as CandidateVerdict['hardChecks'],
      softScore: 85,
      vetoReasons: [],
      highVolatility: false,
      sizeMultiplier: 1,
    };
    repos.recordVerdict(v, null, {
      populationOk: true,
      earlyFlowNetSol: winner ? 2 : -2,
      modelProb: winner ? 0.6 : 0.4,
      featuresJson: JSON.stringify({ earlyFlow: { netInflowSol: winner ? 2 : -2, tx: { buyCount: 5 }, txFlowVersion: 2 } }),
    });
    // Label path: winners hit +20 %, losers hit -20 %.
    const end = winner ? 1.2 : 0.8;
    repos.insertPathTicks([0, 1000, 2000, 3000, 4000, 5000].map((t, k) => ({ mint, arm: 'veto', tMs: t, price: k < 5 ? 1 : end })));
  }
  // A v1 row (phantom migration sell) is excluded from replay by default.
  repos.recordVerdict(
    { mint: 'OLD', verdict: 'accept', hardChecks: [], softScore: 85, vetoReasons: [], highVolatility: false, sizeMultiplier: 1 },
    null,
    { populationOk: true, featuresJson: JSON.stringify({ earlyFlow: { tx: { maxSellSol: 85 } } }) },
  );
  return { db, repos };
}

describe('replay -> report', () => {
  it('replays only clean canonical rows, resumes, and never double-scores', async () => {
    const { db, repos } = seed(6);
    const config = ConfigSchema.parse({ mode: 'paper', decision: { provider: 'stub' } });
    const inputs = loadReplayInputs(db, { provider: 'stub', modelVersion: 'stub-v1' });
    expect(inputs.map((x) => x.mint).sort()).toEqual(['M0', 'M1', 'M2', 'M3', 'M4', 'M5']);
    expect(inputs[0]!.input.checks).toEqual({ H12: 'pass' });
    expect(loadReplayInputs(db, { provider: 'stub', modelVersion: 'stub-v1', includeV1: true })).toHaveLength(7);

    const d = new EntryDecider({ config, repos, client: new StubDecisionClient() });
    for (const x of inputs.slice(0, 4)) d.persist(x.mint, await d.score(x.input, 1000), 'replay');
    expect(loadReplayInputs(db, { provider: 'stub', modelVersion: 'stub-v1' }).map((x) => x.mint).sort()).toEqual(['M4', 'M5']);
  });

  it('measures calibration against triple-barrier labels and the gate lift', async () => {
    const { db, repos } = seed(40);
    const config = ConfigSchema.parse({ mode: 'paper' });
    // Informative model: 0.8 on winners (net inflow +2), 0.2 on losers.
    const client = new ScriptedClient((s) => (((s as { flow?: { netInflowSol?: number } }).flow?.netInflowSol ?? 0) > 0 ? 0.8 : 0.2));
    const d = new EntryDecider({ config, repos, client });
    for (const x of loadReplayInputs(db, { provider: 'scripted', modelVersion: 's-1' })) d.persist(x.mint, await d.score(x.input, 1000), 'replay');

    const rows = loadDecisionRows(db);
    expect(rows).toHaveLength(40);
    const labels = labelsByMint(db, { barrier: BARRIER, cost: COST, arms: ['veto'] });
    expect(labels.get('M0')?.label).toBe(1);
    expect(labels.get('M1')?.label).toBe(0);

    const r = buildDecisionReport(rows, labels, { gate: config.decision.gate, seed: 1 });
    expect(r.groups).toHaveLength(1);
    const g = r.groups[0]!;
    expect(g.key).toBe('scripted · s-1 · q1');
    expect(g.continuation).toMatchObject({ n: 40, baseRate: 0.5 });
    expect(g.continuation!.brier).toBeCloseTo(0.04, 9); // (0.2)^2 everywhere
    expect(g.learnedFilter!.brier).toBeCloseTo(0.16, 9); // model_prob 0.6 / 0.4 baseline
    expect(g.policy!.taken).toBe(20); // continuation 0.8 >= 0.55 and toxic 0.2 <= 0.6
    expect(g.policy!.lift.lo).toBeGreaterThan(0);

    const md = renderDecisionReport(r, { db: ':memory:', barrier: BARRIER, arms: ['veto'] });
    expect(md).toContain('Gate lift CI lower bound > 0');
    expect(md).toContain('| continuation | profitable after costs | 40 |');
  });

  it('prefers the live shadow row over a replay for the same mint', () => {
    const { db, repos } = seed(2);
    const base = { phase: 'entry' as const, provider: 'jev', modelVersion: 'j', stateVersion: 1, questionSetVersion: 1, latencyMs: 1, ok: true, stateJson: '{}' };
    repos.recordDecisionCall({ ...base, mint: 'M0', mode: 'shadow', answersJson: JSON.stringify([{ id: 'continuation', value: 0.61, confidence: 1 }]) });
    repos.recordDecisionCall({ ...base, mint: 'M0', mode: 'replay', answersJson: JSON.stringify([{ id: 'continuation', value: 0.99, confidence: 1 }]) });
    const rows = loadDecisionRows(db);
    expect(rows.find((r) => r.mint === 'M0')).toMatchObject({ mode: 'shadow', answers: [{ id: 'continuation', value: 0.61 }] });
  });
});
