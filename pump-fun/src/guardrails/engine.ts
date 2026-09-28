import type { Config } from '../config/schema.ts';
import type { RunMode } from '../config/schema.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { CandidateVerdict, CheckResult } from '../core/types.ts';
import type { Candidate } from '../enrichment/types.ts';
import type { EntryDecision } from '../risk/manager.ts';
import { scoreCandidate, type MomentumScoringOpts } from './scoring.ts';
import { checkProvenance } from './checks/provenance.ts';
import { checkSerialRugger, checkBreakers } from './checks/blacklist.ts';
import { checkPopulation } from './checks/population.ts';
import { checkManipulation } from './checks/manipulation.ts';
import { checkCreatorHoldings, checkLiquidityFloor } from './checks/pool.ts';

/**
 * Guardrail engine — the fast path (2026-09-28). Every check reads the one
 * batched account read (fastRead.ts) or local state, so the verdict costs
 * well under a millisecond after the read returns. A single FAIL vetoes;
 * nothing else does.
 *
 * What a buy at graduation can actually lose money to, and what covers it:
 *   - unsellable / inflatable / rug-pullable token → P0 (canonical pump.fun
 *     migration: the program guarantees revoked authorities, no Token-2022
 *     traps, burned LP);
 *   - a bad fill → H7 (pool floor, impact) and the tx's own slippage bound;
 *   - our own limits → H8 (blacklist), H10 (breakers);
 *   - known losing populations → H12 (canonical graduation band, mint age),
 *     H6 (creator bag), H13 (serial-launcher cluster, precomputed).
 *
 * Removed, and why:
 *   - H1 / H2 / H3 / H9 / H11 → folded into P0.
 *   - H4 sellability probe (~1.5 s): guaranteed by the program for canonical
 *     mints; its `price_moved` reason was a sniping signal, not a safety one.
 *   - H5 holder concentration: getTokenLargestAccounts cannot index a fresh
 *     mint in time (99/174 unknown on 2026-09-28).
 *   - The unknowns policy and relaxed-risk accepts: with no check that can
 *     come back "could not read", there is nothing left to tolerate.
 *   - The LOW_SCORE gate: the score was flat (421/524 trades at exactly 85).
 *     It is still computed and recorded.
 */

export interface GuardrailRisk {
  canEnter(): EntryDecision;
  getSnapshot?: () => { walletBalanceSol: number | null };
}

export interface CheckContext {
  candidate: Candidate;
  config: Config;
  repos: Repositories;
  mode: RunMode;
  /** Optional risk-manager consult for H10 (absent → check passes). */
  risk?: GuardrailRisk;
  /** Live wallet SOL for H7 buy-impact; 0 when unknown (uses minAbsoluteSol). */
  walletSol: number;
  /** Start of the launch stream's unbroken coverage window (H12); null when none. */
  launchCoverageSinceMs?: number | null;
}

type CheckFn = (ctx: CheckContext) => CheckResult | CheckResult[];

const CHECKS: CheckFn[] = [
  checkProvenance, // P0
  checkCreatorHoldings, // H6
  checkLiquidityFloor, // H7
  checkSerialRugger, // H8
  checkBreakers, // H10
  checkPopulation, // H12
  checkManipulation, // H13
];

export class GuardrailEngine {
  private readonly config: Config;
  private readonly repos: Repositories;
  private readonly momentumOpts: MomentumScoringOpts;
  private readonly risk: GuardrailRisk | undefined;
  private readonly launchCoverageSinceMs: () => number | null;

  constructor(config: Config, repos: Repositories, risk?: GuardrailRisk, launchCoverageSinceMs?: () => number | null) {
    this.config = config;
    this.repos = repos;
    this.risk = risk;
    this.launchCoverageSinceMs = launchCoverageSinceMs ?? (() => null);
    this.momentumOpts = {
      strongInflowSol: config.guardrails.momentumStrongInflowSol,
      maxScoreBonus: config.guardrails.momentumMaxScoreBonus,
      highVolInflowRateSolPerSec: config.guardrails.highVolInflowRateSolPerSec,
    };
  }

  evaluate(candidate: Candidate): CandidateVerdict {
    const walletSol = this.risk?.getSnapshot?.()?.walletBalanceSol ?? 0;
    const ctx: CheckContext = {
      candidate,
      config: this.config,
      repos: this.repos,
      mode: this.config.mode,
      walletSol,
      launchCoverageSinceMs: this.launchCoverageSinceMs(),
      ...(this.risk ? { risk: this.risk } : {}),
    };

    const hardChecks: CheckResult[] = [];
    for (const check of CHECKS) {
      const out = check(ctx);
      if (Array.isArray(out)) hardChecks.push(...out);
      else hardChecks.push(out);
    }

    const vetoReasons = hardChecks.filter((r) => r.status === 'fail').map((r) => r.id);
    const soft = scoreCandidate(candidate, this.momentumOpts);
    const accepted = vetoReasons.length === 0;
    return {
      mint: candidate.graduation.mint,
      verdict: accepted ? 'accept' : 'veto',
      hardChecks,
      softScore: soft.score,
      vetoReasons,
      highVolatility: soft.highVolatility,
      sizeMultiplier: accepted ? soft.sizeMultiplier : 0,
      relaxedRisk: false,
      relaxedReasons: [],
      scoreComponents: soft.components,
    };
  }
}
