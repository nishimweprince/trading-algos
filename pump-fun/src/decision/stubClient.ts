import { estimateTokens, type Answer, type DecisionClient, type DecisionResult, type Question } from './types.ts';

/**
 * Deterministic stand-in for the decision model, so the whole path —
 * persistence, exports, replay, reports, gating — runs in dry-run before
 * platform admission. It claims NO edge: answers are a fixed function of a
 * few state fields plus a hash of the state, so the same state always gets
 * the same answers (replay == live) and nothing downstream can mistake its
 * numbers for signal. model_version is `stub-v1`.
 */
export class StubDecisionClient implements DecisionClient {
  readonly name = 'stub';

  async decide(state: object, questions: readonly Question[]): Promise<DecisionResult> {
    const h = hashUnit(JSON.stringify(state));
    const flow = numAt(state, ['flow', 'netInflowSol']) ?? 0;
    const tilt = 1 / (1 + Math.exp(-flow)); // more net inflow -> higher
    const answers = questions.map((q, i): Answer => {
      const u = (h + i * 0.618_033_988_75) % 1;
      if (q.kind === 'probability') {
        const p = clamp01(0.5 * tilt + 0.5 * u);
        return { id: q.id, value: round3(p), confidence: 0.5 };
      }
      if (q.kind === 'choice') {
        const idx = Math.floor(u * q.options.length) % q.options.length;
        const probs = Object.fromEntries(q.options.map((o, j) => [o, j === idx ? 0.6 : 0.4 / Math.max(1, q.options.length - 1)]));
        return { id: q.id, value: q.options[idx]!, probs, confidence: 0.5 };
      }
      return { id: q.id, value: round3(q.min + u * (q.max - q.min)), confidence: 0.5 };
    });
    return { provider: this.name, modelVersion: 'stub-v1', latencyMs: 0, answers, inputTokens: estimateTokens({ state, questions }) };
  }
}

/** FNV-1a of a string mapped to [0, 1). */
function hashUnit(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h / 0x1_0000_0000;
}

function numAt(obj: object, path: string[]): number | null {
  let cur: unknown = obj;
  for (const k of path) {
    if (!cur || typeof cur !== 'object') return null;
    cur = (cur as Record<string, unknown>)[k];
  }
  return typeof cur === 'number' && Number.isFinite(cur) ? cur : null;
}

const clamp01 = (x: number) => Math.min(1, Math.max(0, x));
const round3 = (x: number) => Math.round(x * 1000) / 1000;
