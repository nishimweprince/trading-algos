/**
 * Decision-model layer (Jev / System One). Provider-neutral types: the
 * pipeline, persistence and research code speak these, and only the provider
 * client (jevClient.ts) knows a wire format.
 *
 * Split, as in the design notes: code computes the state, the decision model
 * interprets it through narrow typed questions, code applies policy, and the
 * risk layer keeps its veto. The model never places orders and can only
 * tighten an entry decision, never loosen one.
 *
 * Question kinds mirror Jev's three primitives:
 *  - probability: a calibrated 0..1 value ("Noul")
 *  - choice: one of up to 255 options, with a distribution
 *  - score: a point on an ordered rubric
 */
export type Question =
  | { id: string; kind: 'probability'; prompt: string }
  | { id: string; kind: 'choice'; prompt: string; options: string[] }
  | { id: string; kind: 'score'; prompt: string; min: number; max: number; rubric?: string[] };

export interface Answer {
  id: string;
  /** probability: 0..1; choice: the chosen option; score: the rubric value. */
  value: number | string;
  /** choice only: option -> probability. */
  probs?: Record<string, number>;
  /** Model-reported confidence 0..1 (1 when the provider does not report one). */
  confidence: number;
}

export interface DecisionResult {
  provider: string;
  /** Version the provider reports it answered with — pin thresholds to this. */
  modelVersion: string;
  latencyMs: number;
  answers: Answer[];
  /** Billed input tokens when reported, else an estimate (chars / 4). */
  inputTokens: number;
}

export interface DecisionClient {
  readonly name: string;
  decide(state: object, questions: readonly Question[], signal: AbortSignal): Promise<DecisionResult>;
  /** Optional: pre-open the provider connection while a candidate is screened. Never throws. */
  warm?(): void;
}

/** Look up a probability answer; null when missing or not numeric. */
export function probOf(result: Pick<DecisionResult, 'answers'>, id: string): number | null {
  const a = result.answers.find((x) => x.id === id);
  return a && typeof a.value === 'number' && Number.isFinite(a.value) ? a.value : null;
}

/** Rough input-token estimate for providers that do not report usage. */
export function estimateTokens(payload: unknown): number {
  return Math.ceil(JSON.stringify(payload).length / 4);
}
