import type { Config } from '../config/schema.ts';
import type { RunMode } from '../config/schema.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { CandidateVerdict, CheckResult } from '../core/types.ts';
import type { Candidate } from '../enrichment/types.ts';
import { scoreCandidate, type MomentumScoringOpts } from './scoring.ts';
import { checkAuthorities } from './checks/authorities.ts';
import { checkToken2022 } from './checks/token2022.ts';
import { checkSerialRugger, checkBreakers } from './checks/blacklist.ts';
import { checkSellability } from './checks/pending.ts';
import {
  checkLpStatus,
  checkHolderConcentration,
  checkCreatorHoldings,
  checkLiquidityFloor,
} from './checks/pool.ts';

/**
 * Guardrail engine (Section 6). Runs every hard-fail check, aggregates a
 * verdict, and computes soft-signal sizing. A single hard FAIL vetoes. The
 * unknowns policy (Section 6.3) is applied here: in live mode an un-evaluable
 * check counts as FAIL; in paper/dry-run it is recorded but does not veto, so
 * safety data still accumulates.
 */

export interface CheckContext {
  candidate: Candidate;
  config: Config;
  repos: Repositories;
  mode: RunMode;
}

type CheckFn = (ctx: CheckContext) => CheckResult | CheckResult[];

const CHECKS: CheckFn[] = [
  checkAuthorities, // H1, H2
  checkLpStatus, // H3
  checkSellability, // H4
  checkHolderConcentration, // H5
  checkCreatorHoldings, // H6
  checkLiquidityFloor, // H7
  checkSerialRugger, // H8
  checkToken2022, // H9
  checkBreakers, // H10
];

export class GuardrailEngine {
  private readonly config: Config;
  private readonly repos: Repositories;
  private readonly momentumOpts: MomentumScoringOpts;

  constructor(config: Config, repos: Repositories) {
    this.config = config;
    this.repos = repos;
    this.momentumOpts = {
      strongInflowSol: config.guardrails.momentumStrongInflowSol,
      maxScoreBonus: config.guardrails.momentumMaxScoreBonus,
      highVolInflowRateSolPerSec: config.guardrails.highVolInflowRateSolPerSec,
    };
  }

  evaluate(candidate: Candidate): CandidateVerdict {
    const ctx: CheckContext = {
      candidate,
      config: this.config,
      repos: this.repos,
      mode: this.config.mode,
    };

    const hardChecks: CheckResult[] = [];
    for (const check of CHECKS) {
      const out = check(ctx);
      if (Array.isArray(out)) hardChecks.push(...out);
      else hardChecks.push(out);
    }

    const liveMode = this.config.mode === 'live';
    const vetoReasons: string[] = [];
    for (const r of hardChecks) {
      if (r.status === 'fail') vetoReasons.push(r.id);
      else if (r.status === 'unknown' && liveMode) vetoReasons.push(`UNKNOWN:${r.id}`);
    }

    const soft = scoreCandidate(candidate, this.momentumOpts);

    // Soft score gates entry but never rescues a hard fail (Section 6.2).
    if (vetoReasons.length === 0 && soft.score < this.config.entry.minEntryScore) {
      vetoReasons.push('LOW_SCORE');
    }

    const verdict: CandidateVerdict = {
      mint: candidate.graduation.mint,
      verdict: vetoReasons.length === 0 ? 'accept' : 'veto',
      hardChecks,
      softScore: soft.score,
      vetoReasons,
      highVolatility: soft.highVolatility,
      sizeMultiplier: verdictIsAccept(vetoReasons) ? soft.sizeMultiplier : 0,
    };
    return verdict;
  }
}

function verdictIsAccept(vetoReasons: string[]): boolean {
  return vetoReasons.length === 0;
}
