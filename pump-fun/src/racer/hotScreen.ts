/**
 * Decision-time guardrail screen on the racer's send path.
 *
 * The live GuardrailEngine is synchronous; its only I/O is three SQLite reads
 * (H8 mint / creator blacklist, H12 launch row). Everything it needs is known
 * before the migrate, so it is precomputed at ADMISSION (`prepare`): the T22
 * transfer fee decoded from the mint account the send-ready probe already
 * reads, the coin creator from the curve pushes, and the three DB answers.
 * At the migrate the screen is pure CPU: synthesize the candidate from the
 * migrate tx's vault balance + the pool's virtual quote (racer/screening.ts
 * `candidateFromRacer`), run ONE persistent engine whose repos facade answers
 * from the prep (same engine code as live — parity), apply the P0 premise
 * break, done. A mint that was never prepared falls back to the real repos
 * for those three reads (counted) and, being unverified T22, fails closed.
 */
import type { Config } from '../config/schema.ts';
import type { Repositories } from '../persistence/repositories.ts';
import { GuardrailEngine } from '../guardrails/engine.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import {
  SCREENING_ENGINE_HOT,
  applyPremiseBreak,
  candidateFromRacer,
  screenWithEngine,
  type RacerScreeningInput,
  type ScreeningVerdict,
} from './screening.ts';

type TransferFee = RacerScreeningInput['transferFee'];
type LaunchRow = ReturnType<Repositories['launchByMint']>;

export interface HotScreenPrep {
  creator: string | null;
  isToken2022: boolean;
  transferFee: TransferFee;
  mintBlacklisted: boolean;
  creatorBlacklisted: boolean;
  launch: LaunchRow;
}

export interface HotScreenInput {
  mint: string;
  poolAddress: string;
  migrateSlot: number | null;
  /** WSOL in the pool's quote vault after the migrate tx (its own post balance). */
  vaultQuoteLamports: bigint;
  /** The pool's virtualQuoteReserves (pushed or synthesized pool). */
  virtualQuoteLamports: bigint;
  /** Used only when the mint was never prepared. */
  isToken2022Fallback?: boolean;
}

export interface HotScreenResult {
  verdict: ScreeningVerdict | null;
  accept: boolean;
  prepared: boolean;
}

export class HotScreen {
  private readonly repos: Repositories;
  private readonly prep = new Map<string, HotScreenPrep>();
  private readonly engine: GuardrailEngine;
  /** The mint currently being screened (the facade answers for it). */
  private current: string | null = null;
  readonly stats = { screened: 0, accepted: 0, vetoed: 0, unprepared: 0, dbFallbacks: 0, noInputs: 0 };

  constructor(config: Config, repos: Repositories) {
    this.repos = repos;
    // Same engine as live; the facade serves the three DB reads from the prep.
    const facade = Object.create(repos) as Repositories;
    const fromPrep = <K extends 'mintBlacklisted' | 'creatorBlacklisted' | 'launch'>(key: K) => {
      const p = this.current ? this.prep.get(this.current) : undefined;
      return p ? p[key] : undefined;
    };
    facade.isMintBlacklisted = (mint: string) => {
      const v = mint === this.current ? fromPrep('mintBlacklisted') : undefined;
      if (v !== undefined) return v;
      this.stats.dbFallbacks++;
      return repos.isMintBlacklisted(mint);
    };
    facade.isCreatorBlacklisted = (address: string) => {
      const p = this.current ? this.prep.get(this.current) : undefined;
      if (p && p.creator === address) return p.creatorBlacklisted;
      this.stats.dbFallbacks++;
      return repos.isCreatorBlacklisted(address);
    };
    facade.launchByMint = (mint: string) => {
      const v = mint === this.current ? fromPrep('launch') : undefined;
      if (v !== undefined) return v;
      this.stats.dbFallbacks++;
      return repos.launchByMint(mint);
    };
    this.engine = new GuardrailEngine(config, facade);
  }

  /** Off the hot path (admission): precompute every input the screen needs. */
  prepare(mint: string, p: { creator: string | null; isToken2022: boolean; transferFee: TransferFee }): void {
    this.prep.set(mint, {
      ...p,
      mintBlacklisted: this.repos.isMintBlacklisted(mint),
      creatorBlacklisted: p.creator ? this.repos.isCreatorBlacklisted(p.creator) : false,
      launch: this.repos.launchByMint(mint),
    });
  }

  /** Re-read the DB answers (e.g. at curve completion) — blacklists can change. */
  refresh(mint: string): void {
    const p = this.prep.get(mint);
    if (p) this.prepare(mint, { creator: p.creator, isToken2022: p.isToken2022, transferFee: p.transferFee });
  }

  /** The creator became known after admission (curve push). */
  noteCreator(mint: string, creator: string): void {
    const p = this.prep.get(mint);
    if (p && p.creator !== creator) this.prepare(mint, { creator, isToken2022: p.isToken2022, transferFee: p.transferFee });
  }

  isPrepared(mint: string): boolean {
    return this.prep.has(mint);
  }

  release(mint: string): void {
    this.prep.delete(mint);
  }

  /** Pure CPU for a prepared mint. */
  screen(input: HotScreenInput): HotScreenResult {
    const p = this.prep.get(input.mint);
    const prepared = p !== undefined;
    if (!prepared) this.stats.unprepared++;
    const screeningInput: RacerScreeningInput = {
      mint: input.mint,
      poolAddress: input.poolAddress,
      migrateSlot: input.migrateSlot,
      reachQuoteSol: Number(input.vaultQuoteLamports + input.virtualQuoteLamports) / LAMPORTS_PER_SOL,
      quoteVaultSol: Number(input.vaultQuoteLamports) / LAMPORTS_PER_SOL,
      coinCreator: p?.creator ?? '',
      isToken2022: p?.isToken2022 ?? input.isToken2022Fallback ?? false,
      // Never prepared → the fee was never read → T22 fails closed.
      transferFee: p ? p.transferFee : 'unknown',
    };
    const candidate = candidateFromRacer(screeningInput);
    if (!candidate) {
      this.stats.noInputs++;
      return { verdict: null, accept: false, prepared };
    }
    this.current = input.mint;
    let verdict: ScreeningVerdict;
    try {
      verdict = applyPremiseBreak(screenWithEngine(this.engine, candidate, SCREENING_ENGINE_HOT), screeningInput);
    } finally {
      this.current = null;
    }
    this.stats.screened++;
    const accept = verdict.verdict === 'accept';
    if (accept) this.stats.accepted++;
    else this.stats.vetoed++;
    return { verdict, accept, prepared };
  }
}
