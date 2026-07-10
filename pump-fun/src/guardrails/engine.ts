import type { Config } from '../config/schema.ts';
import type { RunMode } from '../config/schema.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { CandidateVerdict, CheckResult } from '../core/types.ts';
import type { Candidate } from '../enrichment/types.ts';
import type { EntryDecision } from '../risk/manager.ts';
import { scoreCandidate, type MomentumScoringOpts } from './scoring.ts';
import { quoteReserveSol, BURN_OWNERS } from '../enrichment/pool.ts';
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
  /** Optional risk-manager consult for H10 (absent → check passes). */
  risk?: { canEnter(): EntryDecision };
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
  private readonly risk: { canEnter(): EntryDecision } | undefined;

  constructor(config: Config, repos: Repositories, risk?: { canEnter(): EntryDecision }) {
    this.config = config;
    this.repos = repos;
    this.risk = risk;
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
      ...(this.risk ? { risk: this.risk } : {}),
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
      else if (r.status === 'unknown' && liveMode && !this.canTolerateUnknown(r, hardChecks)) {
        vetoReasons.push(`UNKNOWN:${r.id}`);
      }
    }

    const soft = scoreCandidate(candidate, this.momentumOpts);
    const relaxedReasons = computeRelaxedReasons(candidate, this.config);
    if (
      vetoReasons.length === 0 &&
      relaxedReasons.length > this.config.guardrails.relaxedRiskMaxReasons
    ) {
      vetoReasons.push('MULTI_RELAXED_RISK');
    }

    // Soft score gates entry but never rescues a hard fail (Section 6.2).
    if (vetoReasons.length === 0 && soft.score < this.config.entry.minEntryScore) {
      vetoReasons.push('LOW_SCORE');
    }

    const accepted = vetoReasons.length === 0;
    const relaxedRisk = accepted && relaxedReasons.length > 0;
    const relaxedSizeCap =
      this.config.entry.baseSizeSol > 0
        ? this.config.guardrails.relaxedRiskMaxSizeSol / this.config.entry.baseSizeSol
        : this.config.guardrails.relaxedRiskSizeMultiplierCap;
    const sizeMultiplier =
      accepted
        ? relaxedRisk
          ? Math.min(soft.sizeMultiplier, this.config.guardrails.relaxedRiskSizeMultiplierCap, relaxedSizeCap)
          : soft.sizeMultiplier
        : 0;

    const verdict: CandidateVerdict = {
      mint: candidate.graduation.mint,
      verdict: accepted ? 'accept' : 'veto',
      hardChecks,
      softScore: soft.score,
      vetoReasons,
      highVolatility: soft.highVolatility,
      sizeMultiplier,
      relaxedRisk,
      relaxedReasons,
      scoreComponents: soft.components,
    };
    return verdict;
  }

  private canTolerateUnknown(r: CheckResult, hardChecks: CheckResult[]): boolean {
    if (r.id !== 'H4') return false;
    if (!this.config.guardrails.tolerateTxTooLargeSellability) return false;
    if (r.reason !== 'tx_too_large') return false;
    return checkPassed(hardChecks, 'H2') && checkPassed(hardChecks, 'H9');
  }
}

function checkPassed(checks: CheckResult[], id: string): boolean {
  return checks.some((c) => c.id === id && c.status === 'pass');
}

function computeRelaxedReasons(candidate: Candidate, config: Config): string[] {
  const out: string[] = [];
  const { pool, holders } = candidate.enrichment;
  const g = config.guardrails;

  if (pool) {
    const reserveSol = quoteReserveSol(pool);
    if (g.minPoolSol < g.strictMinPoolSol && reserveSol >= g.minPoolSol && reserveSol < g.strictMinPoolSol) {
      out.push('relaxed_h7_pool_sol');
    }
  }

  if (pool && holders) {
    const excludedAccounts = new Set([pool.baseVault, pool.quoteVault]);
    const real = holders.holders.filter(
      (h) => !excludedAccounts.has(h.account) && !(h.owner && BURN_OWNERS.has(h.owner)),
    );
    const top10 = real.slice(0, 10).reduce((s, h) => s + h.share, 0);
    const maxShare = real[0]?.share ?? 0;
    const creatorShare = holders.holders
      .filter((h) => h.owner === pool.coinCreator)
      .reduce((s, h) => s + h.share, 0);

    const top10Cap = g.top10HolderCapPct / 100;
    const strictTop10Cap = g.strictTop10HolderCapPct / 100;
    if (top10Cap > strictTop10Cap && top10 > strictTop10Cap && top10 <= top10Cap) {
      out.push('relaxed_h5_top10');
    }
    // singleHolderCapPct intentionally has no relaxed path.
    if (maxShare > g.singleHolderCapPct / 100) {
      return out;
    }

    const creatorCap = g.creatorHoldingsCapPct / 100;
    const strictCreatorCap = g.strictCreatorHoldingsCapPct / 100;
    if (creatorCap > strictCreatorCap && creatorShare > strictCreatorCap && creatorShare <= creatorCap) {
      out.push('relaxed_h6_creator');
    }
  }

  return out;
}
