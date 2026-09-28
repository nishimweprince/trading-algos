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
import { buildMetadataState } from '../src/decision/metadataState.ts';
import { buildMetadataQuestions } from '../src/decision/metadataQuestions.ts';
import { buildMetadataReport } from '../src/research/metadataReport.ts';
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

describe('Jev client (docs.typesafe.ai/api)', () => {
  const qs: Question[] = [
    { id: 'p', kind: 'probability', prompt: 'P?' },
    { id: 'c', kind: 'choice', prompt: 'C?', options: ['billing', 'technical', 'sales'] },
    { id: 's', kind: 'score', prompt: 'S?', min: 0, max: 2, rubric: ['Calm', 'Frustrated', 'Very angry'] },
  ];
  // The API reference's own example answers.
  const DOC_ANSWERS = {
    p: { type: 'noul', noul: 0.95 },
    c: { type: 'choice', choice: 'billing', probabilities: { billing: 0.88, technical: 0.12, sales: 0.0 }, confidence: 0.81 },
    s: { type: 'score', score: 1.05, legend: { '0': 'Calm', '1': 'Frustrated', '2': 'Very angry' }, probabilities: { '0': 0.0, '1': 0.95, '2': 0.05 }, confidence: 0.92 },
  } as const;

  it('sends questions as a map keyed by id, in the three primitive shapes', () => {
    const req = toJevRequest('jev-1.13.0', { a: 1 }, qs);
    expect(req).toEqual({
      state: { a: 1 },
      model: 'jev-1.13.0',
      questions: {
        p: { type: 'noul', instructions: 'P?' },
        c: { type: 'choice', instructions: 'C?', criteria: { billing: null, technical: null, sales: null } },
        s: { type: 'score', instructions: 'S?', criteria: ['Calm', 'Frustrated', 'Very angry'] },
      },
    });
  });

  it('derives score levels from min..max without a rubric, and enforces the 2-10 level limit', () => {
    const req = toJevRequest('m', {}, [{ id: 's', kind: 'score', prompt: 'S?', min: 1, max: 3 }]);
    expect(req.questions.s).toEqual({ type: 'score', instructions: 'S?', criteria: ['1', '2', '3'] });
    expect(() => toJevRequest('m', {}, [{ id: 's', kind: 'score', prompt: 'S?', min: 0, max: 10 }])).toThrow('11 levels');
  });

  it('the live entry battery fits the API limits', () => {
    const req = toJevRequest('m', {}, buildEntryQuestions(config()));
    expect(Object.keys(req.questions)).toEqual(['continuation', 'toxic_flow', 'rug_risk', 'manipulation', 'flow_regime', 'setup_quality']);
    expect(req.questions.setup_quality).toMatchObject({ type: 'score', criteria: ['avoid', 'weak', 'acceptable', 'strong'] });
  });

  it('parses the documented answers onto the provider-neutral shape', () => {
    const r = fromJevResponse({ model: 'jev-1.13.0', answers: DOC_ANSWERS, usage: { input_tokens: 318, output_tokens: 34 } }, 'jev-latest', qs);
    expect(r.modelVersion).toBe('jev-1.13.0');
    expect(r.inputTokens).toBe(318);
    expect(r.answers).toEqual([
      { id: 'p', value: 0.95, confidence: 1 }, // noul: no separate confidence
      { id: 'c', value: 'billing', probs: { billing: 0.88, technical: 0.12, sales: 0.0 }, confidence: 0.81 },
      { id: 's', value: 1.05, probs: { '0': 0.0, '1': 0.95, '2': 0.05 }, confidence: 0.92 },
    ]);
  });

  it('maps a score level index back onto the question scale', () => {
    const q: Question[] = [{ id: 's', kind: 'score', prompt: 'S?', min: 1, max: 3 }];
    expect(fromJevResponse({ answers: { s: DOC_ANSWERS.s } }, 'm', q).answers[0]!.value).toBeCloseTo(2.05, 9);
  });

  it('drops missing, mistyped and non-finite answers instead of guessing', () => {
    const r = fromJevResponse(
      { answers: { p: { type: 'choice', choice: 'x' }, c: { type: 'choice' }, s: { type: 'score', score: Number.NaN }, extra: { type: 'noul', noul: 1 } } },
      'm',
      qs,
    );
    expect(r.answers).toEqual([]);
    expect(r.modelVersion).toBe('m');
  });

  it('posts the documented request with the bearer key and reports latency + tokens', async () => {
    let seen: { url: string; init: RequestInit } | null = null;
    const fetchImpl = (async (url: string, init: RequestInit) => {
      seen = { url, init };
      return new Response(JSON.stringify({ model: 'jev-1.13.0', answers: { p: { type: 'noul', noul: 0.6 } }, usage: { input_tokens: 321, output_tokens: 20 } }), { status: 200 });
    }) as unknown as typeof fetch;
    let t = 0;
    const client = new JevHttpClient({ url: 'https://api.typesafe.ai/v1/systemone', apiKey: 'k', model: 'jev-1.13.0', fetchImpl, now: () => (t += 90) });
    const r = await client.decide({ s: 1 }, [{ id: 'p', kind: 'probability', prompt: 'P?' }], new AbortController().signal);
    expect(seen!.url).toBe('https://api.typesafe.ai/v1/systemone');
    expect((seen!.init.headers as Record<string, string>).authorization).toBe('Bearer k');
    const body = JSON.parse(seen!.init.body as string);
    expect(Array.isArray(body.questions)).toBe(false);
    expect(body).toEqual({ state: { s: 1 }, model: 'jev-1.13.0', questions: { p: { type: 'noul', instructions: 'P?' } } });
    expect(r).toMatchObject({ provider: 'jev', modelVersion: 'jev-1.13.0', latencyMs: 90, inputTokens: 321 });
  });

  it('surfaces the status and validation detail on HTTP errors; throws on empty answers', async () => {
    const q: Question[] = [{ id: 'p', kind: 'probability', prompt: 'P?' }];
    const status = (code: number, text: string) => (async () => new Response(text, { status: code })) as unknown as typeof fetch;
    const call = (fetchImpl: typeof fetch) => new JevHttpClient({ url: 'https://x.test', apiKey: 'k', model: 'm', fetchImpl }).decide({}, q, new AbortController().signal);
    await expect(call(status(422, '{"detail":"questions.p.instructions: field required"}'))).rejects.toThrow(/Jev HTTP 422: .*instructions/);
    await expect(call(status(429, ''))).rejects.toThrow('Jev HTTP 429');
    await expect(call(status(529, 'overloaded'))).rejects.toThrow('Jev HTTP 529: overloaded');
    await expect(call(status(200, '{"model":"m","answers":{}}'))).rejects.toThrow('no answers');
  });

  it('end to end: a documented-shape battery response drives the gate', async () => {
    const reply = {
      model: 'jev-1.13.0',
      answers: {
        continuation: { type: 'noul', noul: 0.62 },
        toxic_flow: { type: 'noul', noul: 0.2 },
        rug_risk: { type: 'noul', noul: 0.1 },
        manipulation: { type: 'noul', noul: 0.3 },
        flow_regime: { type: 'choice', choice: 'accumulation', probabilities: { accumulation: 0.7, distribution: 0.1, churn: 0.1, thin: 0.1 }, confidence: 0.6 },
        setup_quality: { type: 'score', score: 2.4, legend: {}, probabilities: {}, confidence: 0.7 },
      },
      usage: { input_tokens: 900, output_tokens: 60 },
    };
    const fetchImpl = (async () => new Response(JSON.stringify(reply), { status: 200 })) as unknown as typeof fetch;
    const client = new JevHttpClient({ url: 'https://x.test', apiKey: 'k', model: 'jev-1.13.0', fetchImpl });
    const d = new EntryDecider({ config: config({ provider: 'stub' }), repos: new Repositories(openDb({ path: ':memory:', memory: true })), client });
    const out = await d.score({ ...candidateFeatureInput(ROW), softScore: 87, checks: { H12: 'pass' } }, 1000);
    expect(out.ok).toBe(true);
    expect(out.result?.answers).toHaveLength(6);
    expect(out.gate).toEqual({ veto: null, sizeFactor: 1, continuation: 0.62 });
    reply.answers.rug_risk.noul = 0.5;
    const vetoed = await d.score({ ...candidateFeatureInput(ROW), softScore: 87, checks: { H12: 'pass' } }, 1000);
    expect(vetoed.gate?.veto).toBe('JEV_SKIP:rug_risk');
  });
});

describe('Jev connection warm-up', () => {
  const flush = () => new Promise((r) => setTimeout(r, 0));
  function client(opts: { status?: number; throws?: boolean } = {}) {
    const calls: Array<{ url: string; method: string; auth: string }> = [];
    let t = 0;
    const fetchImpl = (async (url: string | URL, init: RequestInit = {}) => {
      calls.push({ url: String(url), method: init.method ?? 'GET', auth: (init.headers as Record<string, string>).authorization! });
      if (opts.throws) throw new Error('ECONNRESET');
      const body = String(url).endsWith('/v1/models')
        ? '{"models":[]}'
        : JSON.stringify({ model: 'jev-1.13.0', answers: { p: { type: 'noul', noul: 0.5 } } });
      return new Response(body, { status: opts.status ?? 200 });
    }) as unknown as typeof fetch;
    const c = new JevHttpClient({ url: 'https://api.typesafe.ai/v1/systemone', apiKey: 'k', model: 'jev-1.13.0', fetchImpl, now: () => t });
    return { c, calls, setTime: (ms: number) => (t = ms) };
  }

  it('opens the connection with an authenticated GET /v1/models', async () => {
    const h = client();
    h.c.warm();
    await flush();
    expect(h.calls).toEqual([{ url: 'https://api.typesafe.ai/v1/models', method: 'GET', auth: 'Bearer k' }]);
  });

  it('is throttled while a warm-up is in flight or the socket was just used', async () => {
    const h = client();
    h.setTime(10_000);
    h.c.warm();
    h.c.warm(); // in flight
    await flush();
    expect(h.calls).toHaveLength(1);
    h.setTime(11_000);
    h.c.warm(); // 1 s after the last activity: still warm
    await flush();
    expect(h.calls).toHaveLength(1);
    await h.c.decide({}, [{ id: 'p', kind: 'probability', prompt: 'P?' }], new AbortController().signal);
    h.setTime(12_000);
    h.c.warm(); // 1 s after a decide: still warm
    await flush();
    expect(h.calls).toHaveLength(2);
    h.setTime(14_000);
    h.c.warm(); // 3 s idle: re-warm
    await flush();
    expect(h.calls).toHaveLength(3);
    expect(h.calls[2]!.url).toBe('https://api.typesafe.ai/v1/models');
  });

  it('never throws and never trips the breaker when the warm-up fails', async () => {
    for (const opts of [{ throws: true }, { status: 500 }]) {
      const h = client(opts);
      const d = new EntryDecider({ config: config({ provider: 'stub' }), repos: new Repositories(openDb({ path: ':memory:', memory: true })), client: h.c });
      expect(() => d.warm()).not.toThrow();
      await flush();
      expect(d.stats()).toMatchObject({ failures: 0, open: false });
    }
  });

  it('EntryDecider.warm is a no-op with an open breaker or a client without warm()', async () => {
    const h = client();
    const d = new EntryDecider({ config: config({ provider: 'stub', breaker: { failures: 1, cooldownMs: 60_000 } }), repos: new Repositories(openDb({ path: ':memory:', memory: true })), client: h.c });
    await d.score({ ...candidateFeatureInput(ROW), softScore: 87, checks: { H12: 'pass' } }, 1); // times out -> breaker opens
    const before = h.calls.length;
    d.warm();
    await flush();
    expect(h.calls).toHaveLength(before);
    expect(() => new EntryDecider({ config: config({ provider: 'stub' }), repos: new Repositories(openDb({ path: ':memory:', memory: true })), client: new StubDecisionClient() }).warm()).not.toThrow();
  });
});

describe('metadata battery (shadow)', () => {
  const flush = () => new Promise((r) => setTimeout(r, 0));

  it('state is the token text only: image dropped, socials kept', () => {
    expect(
      buildMetadataState({ name: 'ELON', symbol: 'ELON', hasSocials: true, links: { image: 'https://ipfs.io/ipfs/x', twitter: 'https://x.com/e' } }),
    ).toEqual({ v: 1, name: 'ELON', symbol: 'ELON', links: { twitter: 'https://x.com/e' }, hasSocials: true });
    expect(buildMetadataState({ name: 'A', symbol: 'A', description: 'a coin', hasSocials: false })).toMatchObject({ description: 'a coin' });
    expect(buildMetadataState({ hasSocials: false })).toBeNull();
    expect(buildMetadataState(undefined)).toBeNull();
  });

  it('maps onto the three primitives within API limits', () => {
    const req = toJevRequest('jev-1.13.0', {}, buildMetadataQuestions());
    expect(Object.keys(req.questions)).toEqual(['impersonation', 'low_effort', 'socials_credible', 'narrative']);
    expect(req.questions.impersonation).toMatchObject({ type: 'noul' });
    expect(req.questions.narrative).toMatchObject({ type: 'choice' });
    expect(Object.values((req.questions.narrative as { criteria: Record<string, null> }).criteria).every((v) => v === null)).toBe(true);
  });

  it('persists phase=metadata, never stamps decision_prob, never touches a verdict', async () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    let asked: string[] = [];
    const client: DecisionClient = {
      name: 'fake',
      decide: async (_state, qs) => {
        asked = qs.map((q) => q.id);
        return {
          provider: 'fake',
          modelVersion: 'fake-1',
          latencyMs: 50,
          inputTokens: 120,
          answers: [
            { id: 'impersonation', value: 0.9, confidence: 1 },
            { id: 'low_effort', value: 0.2, confidence: 1 },
            { id: 'socials_credible', value: 0.1, confidence: 1 },
            { id: 'narrative', value: 'celebrity_or_politics', confidence: 0.7 },
          ],
        };
      },
    };
    const d = new EntryDecider({ config: config({ provider: 'stub' }), repos, client });
    const v = verdict();
    repos.recordVerdict(v, null, {});
    d.shadowMetadata('M', { name: 'ELON', symbol: 'ELON', hasSocials: false });
    d.shadowMetadata('N', { hasSocials: false }); // no name -> no call
    await flush();
    expect(asked).toEqual(['impersonation', 'low_effort', 'socials_credible', 'narrative']);
    expect(db.prepare(`SELECT mint, phase, mode, ok, decision_prob, gate_veto, state_version, question_set_version FROM decision_calls`).all()).toEqual([
      { mint: 'M', phase: 'metadata', mode: 'shadow', ok: 1, decision_prob: null, gate_veto: null, state_version: 1, question_set_version: 1 },
    ]);
    expect(db.prepare(`SELECT decision_prob FROM candidates WHERE mint='M'`).get()).toEqual({ decision_prob: null });
    expect(v).toEqual(verdict());
  });

  it('report buckets labelled answers per question', () => {
    const row = (mint: string, imp: number, narrative: string) => ({
      mint,
      mode: 'shadow',
      provider: 'jev',
      modelVersion: 'jev-1.13.0',
      questionSetVersion: 1,
      decisionProb: null,
      modelProb: null,
      answers: [
        { id: 'impersonation', value: imp, confidence: 1 },
        { id: 'narrative', value: narrative, confidence: 1 },
      ],
    });
    const labels = new Map([
      ['a', { label: 1 as const, netReturn: 0.1, arm: 'veto' }],
      ['b', { label: 0 as const, netReturn: -0.2, arm: 'veto' }],
      ['c', { label: 0 as const, netReturn: -0.1, arm: 'veto' }],
    ]);
    const r = buildMetadataReport([row('a', 0.1, 'meme_animal'), row('b', 0.9, 'celebrity_or_politics'), row('c', 0.8, 'celebrity_or_politics'), row('z', 0.9, 'other')], labels, { seed: 1 });
    expect(r).toMatchObject({ scored: 4, labelled: 3 });
    const imp = r.buckets.filter((b) => b.question === 'impersonation');
    expect(imp.map((b) => [b.bucket, b.n, b.winRate])).toEqual([
      ['< 0.5', 1, 1],
      ['>= 0.5', 2, 0],
    ]);
    expect(imp[1]!.meanNetReturn).toBeCloseTo(-0.15, 9);
    expect(r.buckets.find((b) => b.question === 'narrative' && b.bucket === 'celebrity_or_politics')?.n).toBe(2);
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
