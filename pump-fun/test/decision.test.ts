import { describe, it, expect, afterEach } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { computeEntrySizeSol } from '../src/config/sizing.ts';
import { JevHttpClient, fromJevResponse, toJevRequest } from '../src/decision/jevClient.ts';
import { StubDecisionClient } from '../src/decision/stubClient.ts';
import { DecisionBreaker } from '../src/decision/breaker.ts';
import { buildEntryState } from '../src/decision/entryState.ts';
import { buildEntryQuestions } from '../src/decision/entryQuestions.ts';
import { evaluateGate, recalibrate } from '../src/decision/policy.ts';
import { EntryDecider } from '../src/decision/entryDecider.ts';
import { createDecisionClient } from '../src/decision/index.ts';
import { candidateFeatureInput } from '../src/research/dataset.ts';
import type { CandidateVerdict } from '../src/core/types.ts';
import type { DecisionClient, DecisionResult, Question } from '../src/decision/types.ts';

const config = (decision: Record<string, unknown> = {}) => ConfigSchema.parse({ mode: 'paper', decision });

const FEATURES_JSON = JSON.stringify({
  earlyFlow: {
    netInflowSol: 1.035461368,
    inflowRateSolPerSec: 1.964822330170778,
    windowMs: 527,
    tx: { buyCount: 23, sellCount: 23, uniqueBuyers: 13, uniqueSellers: 12, buySol: 10.736644433, sellSol: 95.18575408499994, maxSellSol: 85.005360973 },
  },
  manipulation: {
    copycat: { nameMatches: 0, imageMatches: 0, isCopycat: false },
    cluster: { funder: '8chL…', root: '6vog…', launches7d: 1, wallets: 1, hubFunder: false },
    curve: { creationSlot: null, txScanned: 3000, bundleSharePct: null, creationSlotBuyers: null, washRatio: 0.04692771550232924 },
    timeToGraduateMs: 91782613.9350586,
    snipers: { earlyBuyers: 2, knownSnipers: 0, sniperBuyShare: 0 },
  },
});

/** A candidates row as persisted (the 2026-09-27 G4zJ… trade). */
const ROW = {
  created_at: '2026-09-27 19:30:46',
  early_flow_net_sol: 1.035461368,
  early_flow_rate: 1.964822330170778,
  pool_sol_at_entry: 67.405855287,
  top10_share: 0.362615,
  max_holder_share: 0.08,
  creator_share: 0,
  rugcheck_score: 85,
  has_socials: 0,
  mint_age_ms: 91_800_000,
  mcap_sol_at_entry: 325.7895373948474,
  pool_move_pct: 0.43968778341005876,
  sellability_status: 'pass',
  momentum_window_ms: 500,
  features_json: FEATURES_JSON,
  population_ok: 1,
};

function verdict(overrides: Partial<CandidateVerdict> = {}): CandidateVerdict {
  return {
    mint: 'M',
    verdict: 'accept',
    hardChecks: [{ id: 'H12', status: 'pass', detail: '' }] as CandidateVerdict['hardChecks'],
    softScore: 87,
    vetoReasons: [],
    highVolatility: false,
    sizeMultiplier: 1,
    ...overrides,
  };
}

function answers(a: Record<string, number | string>): Pick<DecisionResult, 'answers'> {
  return { answers: Object.entries(a).map(([id, value]) => ({ id, value, confidence: 1 })) };
}

class FakeClient implements DecisionClient {
  readonly name = 'fake';
  calls = 0;
  private readonly reply: () => Promise<DecisionResult>;
  constructor(reply: () => Promise<DecisionResult>) {
    this.reply = reply;
  }
  decide(): Promise<DecisionResult> {
    this.calls++;
    return this.reply();
  }
}

const passing = (): Promise<DecisionResult> =>
  Promise.resolve({
    provider: 'fake',
    modelVersion: 'fake-1',
    latencyMs: 80,
    inputTokens: 300,
    answers: answers({ continuation: 0.7, toxic_flow: 0.2, rug_risk: 0.1, manipulation: 0.3, flow_regime: 'accumulation', setup_quality: 2.5 }).answers,
  });

describe('Jev client (placeholder wire format)', () => {
  it('maps questions to the three primitives', () => {
    const qs: Question[] = [
      { id: 'p', kind: 'probability', prompt: 'P?' },
      { id: 'c', kind: 'choice', prompt: 'C?', options: ['a', 'b'] },
      { id: 's', kind: 'score', prompt: 'S?', min: 0, max: 3 },
    ];
    const req = toJevRequest('jev-x', { a: 1 }, qs);
    expect(req.questions.map((q) => q.type)).toEqual(['noul', 'choice', 'score']);
    expect(req.questions[1]!.options).toEqual(['a', 'b']);
    expect(req.questions[2]!.scale).toEqual({ min: 0, max: 3 });
  });

  it('parses answers and falls back to the requested model id', () => {
    const r = fromJevResponse({ answers: [{ id: 'p', value: 0.8, confidence: 0.9 }, { id: 'c', answer: 'a', probabilities: { a: 0.7, b: 0.3 } }, { value: 1 }] }, 'jev-x');
    expect(r.modelVersion).toBe('jev-x');
    expect(r.answers).toEqual([
      { id: 'p', value: 0.8, confidence: 0.9 },
      { id: 'c', value: 'a', probs: { a: 0.7, b: 0.3 }, confidence: 1 },
    ]);
  });

  it('posts to the direct API with the key and reports latency + tokens', async () => {
    let seen: { url: string; init: RequestInit } | null = null;
    const fetchImpl = (async (url: string, init: RequestInit) => {
      seen = { url, init };
      return new Response(JSON.stringify({ model: 'jev-2026-09-15', answers: [{ id: 'p', value: 0.6 }], usage: { input_tokens: 321 } }), { status: 200 });
    }) as unknown as typeof fetch;
    let t = 0;
    const client = new JevHttpClient({ url: 'https://api.typesafe.ai/v1/systemone', apiKey: 'k', model: 'jev-latest', fetchImpl, now: () => (t += 90) });
    const r = await client.decide({ s: 1 }, [{ id: 'p', kind: 'probability', prompt: 'P?' }], new AbortController().signal);
    expect(seen!.url).toBe('https://api.typesafe.ai/v1/systemone');
    expect((seen!.init.headers as Record<string, string>).authorization).toBe('Bearer k');
    expect(r).toMatchObject({ provider: 'jev', modelVersion: 'jev-2026-09-15', latencyMs: 90, inputTokens: 321 });
  });

  it('throws on HTTP errors and empty answers', async () => {
    const bad = (async () => new Response('{}', { status: 503 })) as unknown as typeof fetch;
    const empty = (async () => new Response('{"answers":[]}', { status: 200 })) as unknown as typeof fetch;
    const q: Question[] = [{ id: 'p', kind: 'probability', prompt: 'P?' }];
    await expect(new JevHttpClient({ url: 'https://x.test', apiKey: 'k', model: 'm', fetchImpl: bad }).decide({}, q, new AbortController().signal)).rejects.toThrow('503');
    await expect(new JevHttpClient({ url: 'https://x.test', apiKey: 'k', model: 'm', fetchImpl: empty }).decide({}, q, new AbortController().signal)).rejects.toThrow('no answers');
  });
});

describe('createDecisionClient', () => {
  const prev = process.env.TYPESAFE_API_KEY;
  afterEach(() => {
    if (prev === undefined) delete process.env.TYPESAFE_API_KEY;
    else process.env.TYPESAFE_API_KEY = prev;
  });

  it('builds nothing for none, the stub for stub, and refuses jev without a key', () => {
    delete process.env.TYPESAFE_API_KEY;
    expect(createDecisionClient(config())).toBeNull();
    expect(createDecisionClient(config({ provider: 'stub' }))?.name).toBe('stub');
    expect(() => createDecisionClient(config({ provider: 'jev' }))).toThrow('TYPESAFE_API_KEY');
    process.env.TYPESAFE_API_KEY = 'k';
    expect(createDecisionClient(config({ provider: 'jev' }))?.name).toBe('jev');
  });
});

describe('stub client', () => {
  it('is deterministic per state and answers every question in range', async () => {
    const qs = buildEntryQuestions(config());
    const stub = new StubDecisionClient();
    const a = await stub.decide({ flow: { netInflowSol: 1 } }, qs);
    const b = await stub.decide({ flow: { netInflowSol: 1 } }, qs);
    expect(a.answers).toEqual(b.answers);
    expect(a.modelVersion).toBe('stub-v1');
    for (const ans of a.answers) {
      const q = qs.find((x) => x.id === ans.id)!;
      if (q.kind === 'probability') expect(ans.value).toBeGreaterThanOrEqual(0), expect(ans.value).toBeLessThanOrEqual(1);
      if (q.kind === 'choice') expect(q.options).toContain(ans.value);
      if (q.kind === 'score') expect(ans.value).toBeGreaterThanOrEqual(q.min), expect(ans.value).toBeLessThanOrEqual(q.max);
    }
  });
});

describe('decision breaker', () => {
  it('opens after N consecutive failures and probes again after the cooldown', () => {
    let t = 0;
    const br = new DecisionBreaker({ failures: 3, cooldownMs: 1000 }, () => t);
    br.failure();
    br.failure();
    br.success(10); // resets the streak
    br.failure();
    br.failure();
    expect(br.allow()).toBe(true);
    br.failure();
    expect(br.allow()).toBe(false);
    expect(br.snapshot()).toMatchObject({ open: true, skipped: 1, failures: 5, calls: 6, inputTokens: 10 });
    t = 1000;
    expect(br.allow()).toBe(true);
  });
});

describe('entry state', () => {
  it('is compact, numeric, address-free, and drops v1 sell stats', () => {
    const s = buildEntryState({ ...candidateFeatureInput(ROW), softScore: 87, checks: { H4: 'pass', H12: 'pass' } });
    const json = JSON.stringify(s);
    expect(json.length / 4).toBeLessThan(400); // token budget, chars/4 proxy
    expect(json).not.toContain('8chL'); // funder / root addresses never sent
    const flow = s.flow as Record<string, unknown>;
    expect(flow.netInflowSol).toBe(1.04);
    expect(flow.maxSellSol).toBeUndefined(); // v1 row: phantom 85 SOL migration "sell"
    expect(flow.buys).toBe(23);
  });

  it('keeps sell stats on v2 rows', () => {
    const v2 = JSON.parse(FEATURES_JSON);
    v2.earlyFlow.txFlowVersion = 2;
    v2.earlyFlow.tx.maxSellSol = 1.23456;
    const s = buildEntryState(candidateFeatureInput({ ...ROW, features_json: JSON.stringify(v2) }));
    expect((s.flow as Record<string, unknown>).maxSellSol).toBe(1.23);
  });

  it('is identical whether built live or replayed from the persisted row', () => {
    const live = buildEntryState({
      earlyFlowNetSol: ROW.early_flow_net_sol,
      earlyFlowRate: ROW.early_flow_rate,
      poolSolAtEntry: ROW.pool_sol_at_entry,
      top10Share: ROW.top10_share,
      maxHolderShare: ROW.max_holder_share,
      creatorShare: ROW.creator_share,
      rugcheckScore: ROW.rugcheck_score,
      hasSocials: false,
      mintAgeMs: ROW.mint_age_ms,
      mcapSolAtEntry: ROW.mcap_sol_at_entry,
      poolMovePct: ROW.pool_move_pct,
      sellabilityStatus: 'pass',
      momentumWindowMs: 500,
      featuresJson: FEATURES_JSON,
    });
    expect(JSON.stringify(buildEntryState(candidateFeatureInput(ROW)))).toBe(JSON.stringify(live));
  });

  it('asks continuation at the configured exit barriers', () => {
    const q = buildEntryQuestions(config()).find((x) => x.id === 'continuation')!;
    const c = config();
    expect(q.prompt).toContain(`+${c.exits.tp1Pct}%`);
    expect(q.prompt).toContain(`-${c.exits.hardStopPct}%`);
  });
});

describe('gate policy', () => {
  const gate = config().decision.gate;

  it('passes a clean battery and vetoes each threshold breach by question', () => {
    const ok = answers({ continuation: 0.7, toxic_flow: 0.2, rug_risk: 0.1, setup_quality: 2.5 });
    expect(evaluateGate(ok, gate)).toEqual({ veto: null, sizeFactor: 1, continuation: 0.7 });
    expect(evaluateGate(answers({ continuation: 0.5, toxic_flow: 0.2, rug_risk: 0.1, setup_quality: 3 }), gate).veto).toBe('JEV_SKIP:continuation');
    expect(evaluateGate(answers({ continuation: 0.7, toxic_flow: 0.8, rug_risk: 0.1, setup_quality: 3 }), gate).veto).toBe('JEV_SKIP:toxic_flow');
    expect(evaluateGate(answers({ continuation: 0.7, toxic_flow: 0.2, rug_risk: 0.1, setup_quality: 1 }), gate).veto).toBe('JEV_SKIP:setup_quality');
  });

  it('fails closed on a configured question the model did not answer', () => {
    expect(evaluateGate(answers({ continuation: 0.7, toxic_flow: 0.2, setup_quality: 3 }), gate).veto).toBe('JEV_SKIP:rug_risk');
  });

  it('sizes by continuation / floor, clamped, only when enabled', () => {
    const g = { ...gate, sizeByProb: true };
    expect(evaluateGate(answers({ continuation: 0.66, toxic_flow: 0, rug_risk: 0, setup_quality: 3 }), g).sizeFactor).toBeCloseTo(1.2, 9);
    expect(evaluateGate(answers({ continuation: 0.99, toxic_flow: 0, rug_risk: 0, setup_quality: 3 }), g).sizeFactor).toBe(1.25);
  });

  it('applies Platt recalibration before thresholds', () => {
    expect(recalibrate(0.7, undefined)).toBe(0.7);
    expect(recalibrate(0.5, { a: 1, b: 0 })).toBeCloseTo(0.5, 9);
    const g = { ...gate, calibration: { continuation: { a: 1, b: -2 } } }; // shrinks 0.7 -> ~0.24
    expect(evaluateGate(answers({ continuation: 0.7, toxic_flow: 0, rug_risk: 0, setup_quality: 3 }), g).veto).toBe('JEV_SKIP:continuation');
  });

  it('feeds its size factor into the entry size as a separate knob', () => {
    const c = config();
    const base = computeEntrySizeSol(c, 1, 1, 1, false);
    expect(computeEntrySizeSol(c, 1, 1, 1, false, 1.2)).toBeCloseTo(Math.min(base * 1.2, computeEntrySizeSol(c, 1, 100, 1, false)), 9);
  });
});

describe('EntryDecider', () => {
  function harness(decision: Record<string, unknown>, client: DecisionClient) {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const d = new EntryDecider({ config: config(decision), repos, client });
    return { db, repos, d };
  }
  const input = () => ({ ...candidateFeatureInput(ROW), softScore: 87, checks: { H12: 'pass' } });
  const flush = () => new Promise((r) => setTimeout(r, 0));

  it('shadow never mutates the verdict and stamps decision_prob on the candidate row', async () => {
    const h = harness({ provider: 'stub' }, new FakeClient(passing));
    const v = verdict();
    h.repos.recordVerdict(v, null, {});
    h.d.shadow('M', input());
    expect(v).toEqual(verdict()); // untouched
    await flush();
    const call = h.db.prepare(`SELECT mode, provider, model_version, ok, decision_prob, gate_veto, input_tokens FROM decision_calls`).get();
    expect(call).toEqual({ mode: 'shadow', provider: 'fake', model_version: 'fake-1', ok: 1, decision_prob: 0.7, gate_veto: null, input_tokens: 300 });
    expect(h.db.prepare(`SELECT decision_prob FROM candidates WHERE mint='M'`).get()).toEqual({ decision_prob: 0.7 });
  });

  it('only scores the canonical population', () => {
    const h = harness({ provider: 'stub' }, new FakeClient(passing));
    expect(h.d.eligible(verdict())).toBe(true);
    expect(h.d.eligible(verdict({ hardChecks: [{ id: 'H12', status: 'fail', detail: '' }] as CandidateVerdict['hardChecks'] }))).toBe(false);
  });

  it('gate: a late answer vetoes as DECISION_TIMEOUT (fail closed) and records the miss', async () => {
    const slow = new FakeClient(() => new Promise((r) => setTimeout(() => void passing().then(r), 200)));
    const h = harness({ provider: 'stub', mode: 'gate', timeoutMs: 20 }, slow);
    const v = verdict();
    const d = await h.d.applyGate(input(), v);
    expect(v.verdict).toBe('veto');
    expect(v.vetoReasons).toEqual(['DECISION_TIMEOUT']);
    expect(d).toMatchObject({ ok: false, error: 'timeout' });
  });

  it('gate: fail-open passes a late answer through untouched', async () => {
    const slow = new FakeClient(() => new Promise((r) => setTimeout(() => void passing().then(r), 200)));
    const h = harness({ provider: 'stub', mode: 'gate', timeoutMs: 20, failClosed: false }, slow);
    const v = verdict();
    await h.d.applyGate(input(), v);
    expect(v.verdict).toBe('accept');
  });

  it('gate: a threshold miss vetoes with the question id; a pass sets the size factor', async () => {
    const low = new FakeClient(async () => ({ ...(await passing()), answers: answers({ continuation: 0.3, toxic_flow: 0.2, rug_risk: 0.1, setup_quality: 3 }).answers }));
    const h1 = harness({ provider: 'stub', mode: 'gate' }, low);
    const v1 = verdict();
    await h1.d.applyGate(input(), v1);
    expect(v1).toMatchObject({ verdict: 'veto', vetoReasons: ['JEV_SKIP:continuation'], sizeMultiplier: 0 });

    const h2 = harness({ provider: 'stub', mode: 'gate', gate: { sizeByProb: true } }, new FakeClient(passing));
    const v2 = verdict();
    await h2.d.applyGate(input(), v2);
    expect(v2.verdict).toBe('accept');
    expect(v2.decisionSizeFactor).toBe(1.25); // 0.7 / 0.55 = 1.27, capped
  });

  it('skips calls while the breaker is open', async () => {
    const failing = new FakeClient(() => Promise.reject(new Error('HTTP 503')));
    const h = harness({ provider: 'stub', breaker: { failures: 2, cooldownMs: 60_000 } }, failing);
    await h.d.score(input(), 100);
    await h.d.score(input(), 100);
    const d = await h.d.score(input(), 100);
    expect(d.error).toBe('breaker_open');
    expect(failing.calls).toBe(2);
  });

  it('replay rows never overwrite what the live bot recorded', () => {
    const h = harness({ provider: 'stub' }, new FakeClient(passing));
    h.repos.recordVerdict(verdict(), null, { decisionProb: 0.4 });
    h.d.persist('M', { ok: true, state: {}, latencyMs: 1, gate: { veto: null, sizeFactor: 1, continuation: 0.9 } }, 'replay');
    expect(h.db.prepare(`SELECT decision_prob FROM candidates WHERE mint='M'`).get()).toEqual({ decision_prob: 0.4 });
    expect(h.db.prepare(`SELECT mode, decision_prob FROM decision_calls`).get()).toEqual({ mode: 'replay', decision_prob: 0.9 });
  });
});
