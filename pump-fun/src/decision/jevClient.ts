import { registerSecret } from '../core/logger.ts';
import { estimateTokens, type Answer, type DecisionClient, type DecisionResult, type Question } from './types.ts';

/**
 * TypeSafe Jev client (System One decision model). POSTs state + typed
 * questions to the direct API — never through a gateway, each hop is latency
 * that cannot be recovered at block cadence.
 *
 * PLACEHOLDER — written before platform admission. The request/response
 * shapes below follow the public launch material (three primitives, typed
 * answers with probabilities + confidence) and MUST be confirmed against
 * docs.typesafe.ai before `decision.provider: jev` is used. Only
 * toJevRequest / fromJevResponse know the wire format; nothing else changes.
 */

export interface JevClientOpts {
  url: string;
  apiKey: string;
  /** Pinned model id (e.g. a dated version, not `jev-latest`, once gates are tuned). */
  model: string;
  fetchImpl?: typeof fetch;
  now?: () => number;
}

// PLACEHOLDER: confirm field names and primitive names against docs.typesafe.ai.
interface JevQuestion {
  id: string;
  type: 'noul' | 'choice' | 'score';
  question: string;
  options?: string[];
  scale?: { min: number; max: number; rubric?: string[] };
}

// PLACEHOLDER: confirm against docs.typesafe.ai after admission.
export function toJevRequest(model: string, state: object, questions: readonly Question[]): {
  model: string;
  state: object;
  questions: JevQuestion[];
} {
  return {
    model,
    state,
    questions: questions.map((q): JevQuestion => {
      if (q.kind === 'probability') return { id: q.id, type: 'noul', question: q.prompt };
      if (q.kind === 'choice') return { id: q.id, type: 'choice', question: q.prompt, options: q.options };
      return {
        id: q.id,
        type: 'score',
        question: q.prompt,
        scale: { min: q.min, max: q.max, ...(q.rubric ? { rubric: q.rubric } : {}) },
      };
    }),
  };
}

interface JevResponseBody {
  model?: string;
  answers?: Array<{
    id?: string;
    value?: number | string;
    answer?: number | string;
    probabilities?: Record<string, number>;
    confidence?: number;
  }>;
  usage?: { input_tokens?: number };
}

// PLACEHOLDER: confirm against docs.typesafe.ai after admission.
export function fromJevResponse(body: JevResponseBody, requested: string): { modelVersion: string; answers: Answer[]; inputTokens?: number } {
  const answers: Answer[] = [];
  for (const a of body.answers ?? []) {
    const value = a.value ?? a.answer;
    if (!a.id || value === undefined) continue;
    answers.push({
      id: a.id,
      value,
      ...(a.probabilities ? { probs: a.probabilities } : {}),
      confidence: typeof a.confidence === 'number' ? a.confidence : 1,
    });
  }
  return {
    modelVersion: body.model ?? requested,
    answers,
    ...(typeof body.usage?.input_tokens === 'number' ? { inputTokens: body.usage.input_tokens } : {}),
  };
}

export class JevHttpClient implements DecisionClient {
  readonly name = 'jev';
  private readonly url: string;
  private readonly apiKey: string;
  private readonly model: string;
  private readonly fetchImpl: typeof fetch;
  private readonly now: () => number;

  constructor(opts: JevClientOpts) {
    this.url = opts.url;
    this.apiKey = opts.apiKey;
    this.model = opts.model;
    registerSecret(opts.apiKey);
    this.fetchImpl = opts.fetchImpl ?? fetch;
    this.now = opts.now ?? Date.now;
  }

  async decide(state: object, questions: readonly Question[], signal: AbortSignal): Promise<DecisionResult> {
    const started = this.now();
    const payload = toJevRequest(this.model, state, questions);
    const res = await this.fetchImpl(this.url, {
      method: 'POST',
      // PLACEHOLDER: confirm the auth header scheme.
      headers: { 'content-type': 'application/json', authorization: `Bearer ${this.apiKey}` },
      body: JSON.stringify(payload),
      signal,
    });
    if (!res.ok) throw new Error(`Jev HTTP ${res.status}`);
    const parsed = fromJevResponse((await res.json()) as JevResponseBody, this.model);
    if (parsed.answers.length === 0) throw new Error('Jev: response carried no answers');
    return {
      provider: this.name,
      modelVersion: parsed.modelVersion,
      latencyMs: this.now() - started,
      answers: parsed.answers,
      inputTokens: parsed.inputTokens ?? estimateTokens(payload),
    };
  }
}
