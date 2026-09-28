import type { Config } from '../config/schema.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { CandidateVerdict } from '../core/types.ts';
import { getActiveRunSession } from '../core/session.ts';
import { logger } from '../core/logger.ts';
import { DecisionBreaker } from './breaker.ts';
import { buildEntryState, ENTRY_STATE_VERSION, type EntryStateInput } from './entryState.ts';
import { buildEntryQuestions, ENTRY_QUESTION_SET_VERSION } from './entryQuestions.ts';
import { evaluateGate, type GateOutcome } from './policy.ts';
import { buildMetadataState, METADATA_STATE_VERSION } from './metadataState.ts';
import { buildMetadataQuestions, METADATA_QUESTION_SET_VERSION } from './metadataQuestions.ts';
import type { DecisionClient, DecisionResult, Question } from './types.ts';
import type { TokenMetadata } from '../enrichment/types.ts';

/**
 * Shadow calls are off the entry path; give them room so latency is measured,
 * not truncated. 1500 -> 5000 (2026-09-28): jev-1.13.0 measured ~410 ms warm
 * but 840–2100 ms on a cold connection, and graduations arrive minutes apart
 * (past fetch's idle keep-alive), so most shadow calls start cold.
 */
const SHADOW_MIN_TIMEOUT_MS = 5000;

export interface EntryDecision {
  ok: boolean;
  state: Record<string, unknown>;
  result?: DecisionResult;
  error?: string;
  latencyMs: number;
  /** What the gate says about this answer (null when the call failed). */
  gate: GateOutcome | null;
}

/**
 * Runs the entry battery against the decision provider and records it.
 * Shadow (`score` without awaiting, then `persist`) never touches a verdict;
 * gate (`applyGate`) can only veto an accept or scale its size.
 */
export class EntryDecider {
  private readonly config: Config;
  private readonly repos: Repositories;
  private readonly client: DecisionClient;
  private readonly breaker: DecisionBreaker;
  private readonly questions: Question[];
  private readonly metadataQuestions: Question[];
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'decision' });

  constructor(deps: { config: Config; repos: Repositories; client: DecisionClient; now?: () => number }) {
    this.config = deps.config;
    this.repos = deps.repos;
    this.client = deps.client;
    this.now = deps.now ?? Date.now;
    this.breaker = new DecisionBreaker(deps.config.decision.breaker, this.now);
    this.questions = buildEntryQuestions(deps.config);
    this.metadataQuestions = buildMetadataQuestions();
  }

  get mode(): 'shadow' | 'gate' {
    return this.config.decision.mode;
  }

  get provider(): string {
    return this.client.name;
  }

  stats() {
    return this.breaker.snapshot();
  }

  /**
   * Candidates worth a call: the canonical population (H12 pass) — what the
   * bot actually trades, and what the labels cover. Keeps cost and noise down.
   */
  eligible(verdict: CandidateVerdict): boolean {
    const h12 = verdict.hardChecks.find((c) => c.id === 'H12');
    return !h12 || h12.status === 'pass';
  }

  /**
   * Pre-open the provider connection while a candidate is screened (see
   * JevHttpClient.warm). Skipped while the breaker is open: a failing provider
   * gets no traffic at all.
   */
  warm(): void {
    if (this.breaker.allow()) this.client.warm?.();
  }

  async score(input: EntryStateInput, timeoutMs: number): Promise<EntryDecision> {
    const d = await this.ask(buildEntryState(input), this.questions, timeoutMs);
    return d.result ? { ...d, gate: evaluateGate(d.result, this.config.decision.gate) } : d;
  }

  /** One battery call through the shared breaker + timeout path; `gate` is filled by the caller. */
  private async ask(state: Record<string, unknown>, questions: readonly Question[], timeoutMs: number): Promise<EntryDecision> {
    const started = this.now();
    if (!this.breaker.allow()) return { ok: false, state, error: 'breaker_open', latencyMs: 0, gate: null };
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const result = await Promise.race([
        this.client.decide(state, questions, controller.signal),
        new Promise<never>((_, reject) => {
          controller.signal.addEventListener('abort', () => reject(new Error('timeout')), { once: true });
        }),
      ]);
      this.breaker.success(result.inputTokens);
      return { ok: true, state, result, latencyMs: this.now() - started, gate: null };
    } catch (err) {
      this.breaker.failure();
      const error = controller.signal.aborted ? 'timeout' : ((err as Error).message ?? String(err));
      return { ok: false, state, error, latencyMs: this.now() - started, gate: null };
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Shadow-only metadata battery (text judgments: impersonation, low effort,
   * socials, narrative). Fired as soon as enrichment has the metadata, so it
   * runs beside the probe / features phase and adds no screening time. Never
   * touches a verdict or candidates.decision_prob; scored by metadata-report.
   */
  shadowMetadata(mint: string, meta: TokenMetadata | undefined): void {
    const state = buildMetadataState(meta);
    if (!state) return;
    const timeoutMs = Math.max(this.config.decision.timeoutMs, SHADOW_MIN_TIMEOUT_MS);
    void this.ask(state, this.metadataQuestions, timeoutMs)
      .then((d) => this.persist(mint, d, 'shadow', 'metadata'))
      .catch((err) => this.log.debug('metadata shadow decision failed', { mint, err }));
  }

  /** Fire-and-forget shadow call; the verdict is never read or written. */
  shadow(mint: string, input: EntryStateInput): void {
    const timeoutMs = Math.max(this.config.decision.timeoutMs, SHADOW_MIN_TIMEOUT_MS);
    void this.score(input, timeoutMs)
      .then((d) => this.persist(mint, d, 'shadow'))
      .catch((err) => this.log.debug('shadow decision failed', { mint, err }));
  }

  /**
   * Gate mode, accepts only: await the battery within decision.timeoutMs.
   * Mutates `verdict` exactly like the learned filter's MODEL_SKIP. Returns the
   * decision so the caller can persist it AFTER recordVerdict (the candidates
   * row must exist for decision_prob to land on it).
   */
  async applyGate(input: EntryStateInput, verdict: CandidateVerdict): Promise<EntryDecision> {
    const d = await this.score(input, this.config.decision.timeoutMs);
    const veto = d.gate ? d.gate.veto : this.config.decision.failClosed ? 'DECISION_TIMEOUT' : null;
    if (veto) {
      verdict.verdict = 'veto';
      verdict.vetoReasons.push(veto);
      verdict.sizeMultiplier = 0;
    } else if (d.gate) {
      verdict.decisionSizeFactor = d.gate.sizeFactor;
    }
    return d;
  }

  persist(mint: string, d: EntryDecision, mode: 'shadow' | 'gate' | 'replay', phase: 'entry' | 'metadata' = 'entry'): void {
    const session = getActiveRunSession();
    const entry = phase === 'entry';
    try {
      this.repos.recordDecisionCall({
        mint,
        phase,
        mode,
        provider: d.result?.provider ?? this.client.name,
        modelVersion: d.result?.modelVersion ?? null,
        stateVersion: entry ? ENTRY_STATE_VERSION : METADATA_STATE_VERSION,
        questionSetVersion: entry ? ENTRY_QUESTION_SET_VERSION : METADATA_QUESTION_SET_VERSION,
        latencyMs: d.ok ? (d.result?.latencyMs || d.latencyMs) : d.latencyMs,
        ok: d.ok,
        error: d.error ?? null,
        stateJson: JSON.stringify(d.state),
        answersJson: d.result ? JSON.stringify(d.result.answers) : null,
        inputTokens: d.result?.inputTokens ?? null,
        // Only the entry battery speaks for candidates.decision_prob / the gate.
        decisionProb: entry ? (d.gate?.continuation ?? null) : null,
        gateVeto: entry && d.gate ? d.gate.veto : null,
        sessionId: session?.id ?? null,
        configHash: session?.configHash ?? null,
      });
    } catch (err) {
      this.log.error('failed to persist decision call', { mint, err });
    }
  }
}
