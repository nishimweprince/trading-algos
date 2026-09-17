import type { Config } from '../config/schema.ts';
import type { RunMode } from '../config/schema.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { CandidateVerdict, CheckResult } from '../core/types.ts';
import type { Candidate } from '../enrichment/types.ts';
import type { EntryDecision } from '../risk/manager.ts';
import { scoreCandidate, type MomentumScoringOpts, type TokenAgeScoringOpts } from './scoring.ts';
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
  private readonly tokenAgeOpts: TokenAgeScoringOpts;
  private readonly risk: GuardrailRisk | undefined;

  constructor(config: Config, repos: Repositories, risk?: GuardrailRisk) {
    this.config = config;
    this.repos = repos;
    this.risk = risk;
    this.momentumOpts = {
      strongInflowSol: config.guardrails.momentumStrongInflowSol,
      maxScoreBonus: config.guardrails.momentumMaxScoreBonus,
      highVolInflowRateSolPerSec: config.guardrails.highVolInflowRateSolPerSec,
    };
    this.tokenAgeOpts = {
      freshMs: config.guardrails.tokenAgeFreshMinutes * 60_000,
      staleMs: config.guardrails.tokenAgeStaleMinutes * 60_000,
      maxPenalty: config.guardrails.tokenAgeMaxPenalty,
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
    let toleratedH4Unknown = false;
    let toleratedDataGapUnknown = false;
    for (const r of hardChecks) {
      if (r.status === 'fail') {
        vetoReasons.push(r.id);
      } else if (r.status === 'unknown' && liveMode) {
        if (this.canTolerateUnknown(r, hardChecks)) {
          if (r.id === 'H4') toleratedH4Unknown = true;
          else toleratedDataGapUnknown = true;
        } else {
          vetoReasons.push(`UNKNOWN:${r.id}`);
        }
      }
    }

    const soft = scoreCandidate(candidate, this.momentumOpts, this.tokenAgeOpts);
    const relaxedReasons = computeRelaxedReasons(candidate, this.config);
    if (toleratedH4Unknown) relaxedReasons.push('relaxed_unknown_h4');
    if (toleratedDataGapUnknown) relaxedReasons.push('relaxed_unknown_data_gap');
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
      this.config.entry.baseSizeWalletPct > 0
        ? this.config.guardrails.relaxedRiskMaxSizeWalletPct / this.config.entry.baseSizeWalletPct
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
    if (r.id === 'H4') {
      // tx_too_large/buy_only_ok/account_setup_unavailable mean "we got SOME
      // signal, just not a full atomic sell proof". rpc_unavailable/not_run
      // mean the probe never ran at all — behind tolerateUnprobedSellability
      // this falls back to trusting H2 (freeze) + H9 (Token-2022) alone, i.e.
      // the static honeypot vectors, with NO dynamic sell confirmation. That
      // is a real risk trade, not an infra fix — off by default, opt-in only.
      // price_moved is excluded from every flag, unconditionally: it means
      // the pool is being sniped right now, never a data gap, and tolerating
      // it produced the 3–15s stop-loss pattern on 2026-09-16 (see tests).
      const allowed =
        (r.reason === 'tx_too_large' && this.config.guardrails.tolerateTxTooLargeSellability) ||
        (r.reason === 'buy_only_ok' && this.config.guardrails.sellabilityBuyOnlyBackstop) ||
        (r.reason === 'account_setup_unavailable' && this.config.guardrails.tolerateInconclusiveSellability) ||
        ((r.reason === 'rpc_unavailable' || r.reason === 'not_run') &&
          this.config.guardrails.tolerateUnprobedSellability);
      if (!allowed) return false;
      return hardChecks.every((check) => check.id === 'H4' || check.status === 'pass');
    }
    // General relief valve for the remaining checks (H1/H2/H3/H5/H6/H9), all of
    // which only ever go `unknown` on a plain "could not read the account/pool/
    // holders" data gap — never a signal in themselves (H8/H10 never report
    // `unknown`; they only pass/fail off local data). 2026-09-17: 98.1% of live
    // vetoes had >=1 unknown check and only ~6% were a genuine hard fail, so an
    // RPC data gap — not real risk — was the dominant blocker. Still refuses
    // outright the moment ANYTHING is an explicit fail (a real risk signal is
    // never rescued), and an accepted candidate is sized down via relaxedRisk
    // exactly like every other relaxed-entry path.
    if (!this.config.guardrails.tolerateUnknownWhenNoHardFail) return false;
    return !hardChecks.some((check) => check.status === 'fail');
  }
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
