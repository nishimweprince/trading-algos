/**
 * Offline screening parity (paper-honesty Unit 8): would the live guardrail
 * engine have taken these paper tracks?
 *
 * The racer funnel (curve admission → migrate tracking) is not the live
 * funnel (graduation → GuardrailEngine → open), so paper prices mints the
 * live bot would have vetoed. This module synthesizes a screening Candidate
 * per racer track from stored graduation-time data and runs the REAL engine
 * (same code as live, not a copy) over it. The verdict rides on the track
 * (screening_json) for the dashboard/CSV; the summary counts the screened
 * cohort.
 *
 * Synthesis, per check:
 * - P0 (canonical migration): by construction. Every racer track descends
 *   from a pump.fun Migrate instruction observed on the node stream
 *   (migrateTx.ts matches program + Migrate), and pump.fun mints carry no
 *   authorities or rug extensions — the same program guarantee P0 itself
 *   relies on. Premise-break detection covers TransferFeeConfig only (the one
 *   extension the racer decodes — see Unit 9): a fee vetoes here like live,
 *   an unverified T22 mint fails closed, and other rug extensions would pass
 *   here and fail live (stated residual gap).
 * - H7 (liquidity floor + impact): real graduation-time reserves — vault SOL
 *   from entry_features.quoteVaultSol, effective (vault + virtual) from the
 *   reach mark. Tracks that predate entry features cannot run H7: verdict
 *   null (unknown), never a guess.
 * - H8 (blacklist): mint clause enforced against the current blacklist;
 *   creator clause enforced when the curve creator was captured
 *   (racer_migrations.coin_creator), skipped otherwise.
 * - H10 (breakers): as-now, like screenReplay — no risk manager offline, so
 *   only the KILL sentinel can fail it.
 * - H12 (population): the mint suffix, exact.
 *
 * Verdicts stamp the engine version; when the checks change, bump it and
 * re-run racer:backfill-screening so history replays at HEAD.
 */
import type { Config } from '../config/schema.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { Candidate } from '../enrichment/types.ts';
import { WSOL_MINT, LAMPORTS_PER_SOL } from '../core/constants.ts';
import { GuardrailEngine } from '../guardrails/engine.ts';

/** Bump when the guardrail checks change; stale verdicts re-replay via backfill. */
export const SCREENING_ENGINE = 'guardrail-fast-path/1';

export interface RacerScreeningInput {
  mint: string;
  poolAddress: string;
  migrateSlot: number | null;
  /** Effective graduation depth (vault + virtual), from the reach mark. */
  reachQuoteSol: number;
  /** Vault SOL at graduation, from entry_features (null = H7 unrunnable). */
  quoteVaultSol: number | null;
  /** Curve creator when captured ('' = unknown, creator clause skipped). */
  coinCreator: string;
  isToken2022: boolean;
  /** T22 transfer fee read (Unit 9 premise-break detection). */
  transferFee?: { bps: number; maxRaw: string } | 'unknown' | null | undefined;
}

/** A synthesized screening candidate, or null when H7 inputs are missing. */
export function candidateFromRacer(input: RacerScreeningInput): Candidate | null {
  if (!(input.reachQuoteSol > 0) || !(input.quoteVaultSol !== null && input.quoteVaultSol >= 0)) return null;
  const vaultLamports = BigInt(Math.round(input.quoteVaultSol * LAMPORTS_PER_SOL));
  const effectiveLamports = BigInt(Math.round(input.reachQuoteSol * LAMPORTS_PER_SOL));
  return {
    graduation: {
      mint: input.mint,
      venue: 'pumpswap',
      poolAddress: input.poolAddress,
      slot: input.migrateSlot ?? 0,
      feedSource: 'laserstream',
      receivedAtNs: 0n,
    },
    enrichment: {
      pool: {
        poolAddress: input.poolAddress,
        baseMint: input.mint,
        quoteMint: WSOL_MINT,
        lpMint: '',
        baseVault: '',
        quoteVault: '',
        creator: input.coinCreator,
        coinCreator: input.coinCreator,
        isCanonical: true,
        baseReserve: 0n,
        quoteReserveLamports: vaultLamports,
        virtualQuoteLamports: effectiveLamports >= vaultLamports ? effectiveLamports - vaultLamports : 0n,
      },
      mintInfo: {
        isToken2022: input.isToken2022,
        mintAuthority: null,
        freezeAuthority: null,
        supply: 0n,
        decimals: 6,
        extensions: [],
      },
      unknowns: [],
      elapsedMs: 0,
    },
  };
}

export interface ScreeningVerdict {
  engine: string;
  verdict: 'accept' | 'veto';
  fails: Array<{ id: string; reason?: string; detail?: string }>;
  softScore: number;
  /** The candidate was synthesized from racer data (see module doc), not fast-read. */
  synthesized: true;
}

/** Decision-time screen on the racer send path (src/racer/hotScreen.ts). */
export const SCREENING_ENGINE_HOT = 'guardrail-hot/2';

/** Run the live engine over a synthesized candidate. */
export function screenRacerCandidate(candidate: Candidate, config: Config, repos: Repositories): ScreeningVerdict {
  return screenWithEngine(new GuardrailEngine(config, repos), candidate);
}

/** Run a given (reusable) engine over a candidate. */
export function screenWithEngine(engine: GuardrailEngine, candidate: Candidate, engineTag = SCREENING_ENGINE): ScreeningVerdict {
  const v = engine.evaluate(candidate);
  return {
    engine: engineTag,
    verdict: v.verdict,
    fails: v.hardChecks
      .filter((h) => h.status === 'fail')
      .map((h) => ({ id: h.id, ...(h.reason ? { reason: h.reason } : {}), ...(h.detail ? { detail: h.detail } : {}) })),
    softScore: v.softScore,
    synthesized: true,
  };
}

/** Synthesize and screen in one step; null when H7 inputs are missing. */
export function screenRacerTrack(input: RacerScreeningInput, config: Config, repos: Repositories): ScreeningVerdict | null {
  const c = candidateFromRacer(input);
  if (!c) return null;
  return applyPremiseBreak(screenRacerCandidate(c, config, repos), input);
}

/**
 * P0 premise-break detection (Unit 9): a transfer fee on the mint fails live
 * P0 (rug_extension), so it vetoes here too. An unverified T22 mint fails
 * closed — safety screening cannot assume the extension absent.
 */
export function applyPremiseBreak(v: ScreeningVerdict, input: Pick<RacerScreeningInput, 'transferFee' | 'isToken2022'>): ScreeningVerdict {
  const tf = input.transferFee;
  if (tf && tf !== 'unknown') {
    return { ...v, verdict: 'veto',
      fails: [{ id: 'P0', reason: 'rug_extension', detail: `TransferFee ${tf.bps} bps (premise break)` }, ...v.fails] };
  }
  if (input.isToken2022 && (tf === 'unknown' || tf === undefined)) {
    return { ...v, verdict: 'veto',
      fails: [{ id: 'P0', reason: 't22_fee_unreadable', detail: 'T22 fee never verified (fail closed)' }, ...v.fails] };
  }
  return v;
}
