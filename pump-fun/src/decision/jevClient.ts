import { logger, registerSecret } from '../core/logger.ts';
import { estimateTokens, type Answer, type DecisionClient, type DecisionResult, type Question } from './types.ts';

/**
 * TypeSafe Jev client (System One decision model). POSTs state + typed
 * questions to the direct API — never through a gateway, each hop is latency
 * that cannot be recovered at block cadence.
 *
 * Wire format per docs.typesafe.ai/api (checked 2026-09-28, jev-1.13.0):
 * `questions` and `answers` are maps keyed by our question id; each question
 * is `{ type, instructions, criteria? }`. Only toJevRequest / fromJevResponse
 * know it; nothing else changes if it moves.
 *
 * No retries: a 429 / 529 counts as a failure and the DecisionBreaker backs
 * off. SDK-style retry-with-backoff would blow the gate's timeout budget.
 *
 * Latency (2026-09-28): ~350 ms of a ~410 ms warm call is network RTT; a new
 * TLS connection adds 250–1,100 ms, and fetch drops idle sockets after ~4 s.
 * `warm()` opens the connection while a candidate is still being screened.
 */

/** A socket used this recently is still in fetch's ~4 s idle keep-alive window. */
export const WARM_FRESH_MS = 2500;

export interface JevClientOpts {
  url: string;
  apiKey: string;
  /** Pinned model id (e.g. `jev-1.13.0`, not the moving `jev-latest` alias, once gates are tuned). */
  model: string;
  fetchImpl?: typeof fetch;
  now?: () => number;
}

/** Score questions take 2..10 ordered levels (API limit). */
const SCORE_MIN_LEVELS = 2;
const SCORE_MAX_LEVELS = 10;
/** Choice questions take at most 255 options (API limit). */
const CHOICE_MAX_OPTIONS = 255;

type JevQuestion =
  | { type: 'noul'; instructions: string }
  | { type: 'choice'; instructions: string; criteria: Record<string, string | null> }
  | { type: 'score'; instructions: string; criteria: string[] };

export interface JevRequest {
  state: object;
  model: string;
  questions: Record<string, JevQuestion>;
}

/** Score levels as sent: the rubric, else the integer points min..max. */
function scoreLevels(q: Extract<Question, { kind: 'score' }>): string[] {
  const levels = q.rubric ?? Array.from({ length: q.max - q.min + 1 }, (_, i) => String(q.min + i));
  if (levels.length < SCORE_MIN_LEVELS || levels.length > SCORE_MAX_LEVELS) {
    throw new Error(`Jev score question ${q.id}: ${levels.length} levels, API accepts ${SCORE_MIN_LEVELS}-${SCORE_MAX_LEVELS}`);
  }
  return levels;
}

export function toJevRequest(model: string, state: object, questions: readonly Question[]): JevRequest {
  const out: Record<string, JevQuestion> = {};
  for (const q of questions) {
    if (q.kind === 'probability') {
      out[q.id] = { type: 'noul', instructions: q.prompt };
    } else if (q.kind === 'choice') {
      if (q.options.length > CHOICE_MAX_OPTIONS) {
        throw new Error(`Jev choice question ${q.id}: ${q.options.length} options, API accepts ${CHOICE_MAX_OPTIONS}`);
      }
      out[q.id] = { type: 'choice', instructions: q.prompt, criteria: Object.fromEntries(q.options.map((o) => [o, null])) };
    } else {
      out[q.id] = { type: 'score', instructions: q.prompt, criteria: scoreLevels(q) };
    }
  }
  return { state, model, questions: out };
}

type JevAnswer =
  | { type: 'noul'; noul?: number }
  | { type: 'choice'; choice?: string; probabilities?: Record<string, number>; confidence?: number }
  | { type: 'score'; score?: number; legend?: Record<string, string>; probabilities?: Record<string, number>; confidence?: number };

export interface JevResponseBody {
  model?: string;
  answers?: Record<string, JevAnswer>;
  usage?: { input_tokens?: number; output_tokens?: number };
}

const KIND_TO_TYPE = { probability: 'noul', choice: 'choice', score: 'score' } as const;

export function fromJevResponse(
  body: JevResponseBody,
  requested: string,
  questions: readonly Question[],
): { modelVersion: string; answers: Answer[]; inputTokens?: number } {
  const answers: Answer[] = [];
  for (const q of questions) {
    const a = body.answers?.[q.id];
    // A missing answer or one of the wrong type is dropped, never guessed:
    // the gate fails closed on a question it was asked to hold.
    if (!a || a.type !== KIND_TO_TYPE[q.kind]) continue;
    if (a.type === 'noul') {
      // A noul carries no separate confidence — the value is the whole answer.
      if (typeof a.noul === 'number' && Number.isFinite(a.noul)) answers.push({ id: q.id, value: a.noul, confidence: 1 });
    } else if (a.type === 'choice') {
      if (typeof a.choice !== 'string') continue;
      answers.push({
        id: q.id,
        value: a.choice,
        ...(a.probabilities ? { probs: a.probabilities } : {}),
        confidence: typeof a.confidence === 'number' ? a.confidence : 1,
      });
    } else if (q.kind === 'score') {
      if (typeof a.score !== 'number' || !Number.isFinite(a.score)) continue;
      // `score` is a probability-weighted level INDEX (0-based); map it back
      // onto the question's own scale.
      answers.push({
        id: q.id,
        value: q.min + a.score,
        ...(a.probabilities ? { probs: a.probabilities } : {}),
        confidence: typeof a.confidence === 'number' ? a.confidence : 1,
      });
    }
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
  private readonly log = logger.child({ mod: 'jev' });
  /** When the last request (warm or decide) finished; -Infinity = never. */
  private lastActivityMs = Number.NEGATIVE_INFINITY;
  private warming = false;

  constructor(opts: JevClientOpts) {
    this.url = opts.url;
    this.apiKey = opts.apiKey;
    this.model = opts.model;
    registerSecret(opts.apiKey);
    this.fetchImpl = opts.fetchImpl ?? fetch;
    this.now = opts.now ?? Date.now;
  }

  /**
   * Open (or refresh) the pooled TLS connection with the cheapest authenticated
   * request — `GET /v1/models`, unbilled and outside the systemone budget.
   * Fire-and-forget; a failure only means the next call starts cold, so it is
   * logged at debug and never reaches the breaker.
   */
  warm(): void {
    if (this.warming || this.now() - this.lastActivityMs < WARM_FRESH_MS) return;
    this.warming = true;
    void this.fetchImpl(new URL('/v1/models', this.url), { headers: { authorization: `Bearer ${this.apiKey}` } })
      .then((res) => res.arrayBuffer()) // drain so the socket returns to the pool
      .then(
        () => {
          this.lastActivityMs = this.now();
        },
        (err) => this.log.debug('jev warm-up failed', { err }),
      )
      .finally(() => {
        this.warming = false;
      });
  }

  async decide(state: object, questions: readonly Question[], signal: AbortSignal): Promise<DecisionResult> {
    const started = this.now();
    const payload = toJevRequest(this.model, state, questions);
    let body: JevResponseBody;
    try {
      const res = await this.fetchImpl(this.url, {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${this.apiKey}` },
        body: JSON.stringify(payload),
        signal,
      });
      if (!res.ok) {
        // 422 names the offending field; keep it for decision_calls.error.
        const detail = (await res.text().catch(() => '')).replace(/\s+/g, ' ').slice(0, 300);
        throw new Error(`Jev HTTP ${res.status}${detail ? `: ${detail}` : ''}`);
      }
      body = (await res.json()) as JevResponseBody;
    } catch (err) {
      this.lastActivityMs = this.now();
      throw err;
    }
    const finished = this.now();
    this.lastActivityMs = finished;
    const parsed = fromJevResponse(body, this.model, questions);
    if (parsed.answers.length === 0) throw new Error('Jev: response carried no answers');
    return {
      provider: this.name,
      modelVersion: parsed.modelVersion,
      latencyMs: finished - started,
      answers: parsed.answers,
      inputTokens: parsed.inputTokens ?? estimateTokens(payload),
    };
  }
}
