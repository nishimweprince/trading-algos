/**
 * Send-ready probe: how long from the landed migrate reaching this process to
 * a SIGNED post-migration PumpSwap buy, built the way the trader builds one.
 * Replaces the old trade-local probe, which timed a remote HTTPS call that
 * built a bonding-curve completion buy we never intend to send (~300 ms).
 *
 * Hot path, zero RPC:
 *   state — pool account (pushed on the racer stream), mint account and the
 *           user's ATAs (prepared at admission), PumpSwap configs (kept warm),
 *           reserves from the migrate tx's own token balances;
 *   build — the SDK's offline `buyQuoteInput` (remaining accounts, fees);
 *   sign  — v0 assembly with a cached blockhash, compute budget and tip,
 *           signed by an ephemeral never-funded key.
 * The bytes are dropped. Nothing here can send.
 *
 * Keep-warm: a probe runs once every few minutes, so V8 never keeps the
 * build / sign code hot (live p50 was ~17 ms against ~2 ms warm). `warm()`
 * re-runs build → sign on the last real inputs and discards the result; the
 * worker calls it on a short interval, like the trader's blockhash keep-warm.
 */
import BN from 'bn.js';
import { PumpAmmSdk } from '@pump-fun/pump-swap-sdk';
import type { Connection } from '@solana/web3.js';
import { assembleSignedSwapTx } from '../executor/assemble.ts';
import type { FeePlan } from '../executor/fees.ts';
import { buildSwapState, type AmmConfigs, type RawAccountRead } from '../executor/swapState.ts';
import type { Wallet } from '../executor/wallet.ts';

export interface SendReadyPrep {
  poolKey: string;
  baseMint: RawAccountRead;
  userBaseAta: string;
  userQuoteAta: string;
}

export interface SendReadyInput {
  mint: string;
  /** process.hrtime.bigint() when the frame carrying the landed migrate arrived. */
  receivedAtNs: bigint;
  baseReserve: bigint;
  quoteReserveLamports: bigint;
}

export type SendReadyResult =
  | { ok: true; totalUs: number; waitPoolUs: number; stateUs: number; buildUs: number; signUs: number; bytes: number }
  | { ok: false; reason: 'not-prepared' | 'no-configs' | 'no-pool' | 'error'; totalUs: number; detail?: string };

export interface SendReadyDeps {
  configs: () => AmmConfigs | null;
  blockhash: () => Promise<string>;
  wallet: Wallet;
  feePlan: FeePlan;
  tipAccount: () => string | undefined;
  quoteLamports: bigint;
  slippagePct: number;
  /** Longest wait for the pool account push when the migrate tx arrived first. */
  poolWaitMs?: number;
  nowNs?: () => bigint;
}

const usSince = (startNs: bigint, nowNs: bigint): number => Number(nowNs - startNs) / 1_000;

export class SendReadyProbe {
  private readonly deps: SendReadyDeps;
  private readonly offline = new PumpAmmSdk();
  private readonly prep = new Map<string, SendReadyPrep>();
  private readonly pools = new Map<string, RawAccountRead>();
  private readonly waiters = new Map<string, Array<() => void>>();
  private readonly nowNs: () => bigint;
  /** Unused by assembly when a blockhash provider is supplied; satisfies the type. */
  private readonly connection = null as unknown as Connection;
  /** Last real build inputs, replayed by warm(). */
  private warmInputs: { prep: SendReadyPrep; pool: RawAccountRead; baseReserve: bigint; quoteReserveLamports: bigint } | null = null;
  warmRuns = 0;

  constructor(deps: SendReadyDeps) {
    this.deps = deps;
    this.nowNs = deps.nowNs ?? (() => process.hrtime.bigint());
  }

  /** Seed the keep-warm inputs (e.g. from the latest recorded pool at boot). */
  seedWarm(prep: SendReadyPrep, pool: RawAccountRead, baseReserve: bigint, quoteReserveLamports: bigint): void {
    this.warmInputs = { prep, pool, baseReserve, quoteReserveLamports };
  }

  /** Re-run state → build → sign on the last real inputs; result discarded. */
  async warm(): Promise<boolean> {
    const w = this.warmInputs;
    const configs = this.deps.configs();
    if (!w || !configs) return false;
    try {
      await this.buildAndSign(w.prep, w.pool, w.baseReserve, w.quoteReserveLamports, configs);
      this.warmRuns++;
      return true;
    } catch {
      return false;
    }
  }

  private async buildAndSign(
    prep: SendReadyPrep,
    pool: RawAccountRead,
    baseReserve: bigint,
    quoteReserveLamports: bigint,
    configs: AmmConfigs,
  ): Promise<{ bytes: Uint8Array; t2: bigint; t3: bigint; t4: bigint }> {
    const state = buildSwapState(
      {
        poolKey: prep.poolKey,
        pool,
        baseMint: prep.baseMint,
        baseReserve,
        quoteReserveLamports,
        user: this.deps.wallet.publicKey,
        userBaseAta: prep.userBaseAta,
        userBaseAccount: null,
        userQuoteAta: prep.userQuoteAta,
        userQuoteAccount: null,
      },
      configs,
    );
    const t2 = this.nowNs();
    const ixs = await this.offline.buyQuoteInput(state, new BN(this.deps.quoteLamports.toString()), this.deps.slippagePct);
    const t3 = this.nowNs();
    const tipAccount = this.deps.tipAccount();
    const bytes = await assembleSignedSwapTx(ixs, {
      connection: this.connection,
      wallet: this.deps.wallet,
      feePlan: this.deps.feePlan,
      ...(tipAccount ? { jitoTipAccount: tipAccount } : {}),
      blockhashProvider: this.deps.blockhash,
    });
    return { bytes, t2, t3, t4: this.nowNs() };
  }

  /** Off the hot path (admission): everything that exists before the migrate. */
  prepare(mint: string, prep: SendReadyPrep): void {
    this.prep.set(mint, prep);
  }

  isPrepared(mint: string): boolean {
    return this.prep.has(mint);
  }

  /** The pool account was pushed (created by the migrate tx, then rewritten by swaps). */
  onPool(mint: string, account: RawAccountRead): void {
    this.pools.set(mint, account);
    const ws = this.waiters.get(mint);
    if (ws) {
      this.waiters.delete(mint);
      for (const w of ws) w();
    }
  }

  release(mint: string): void {
    this.prep.delete(mint);
    this.pools.delete(mint);
    const ws = this.waiters.get(mint);
    this.waiters.delete(mint);
    for (const w of ws ?? []) w();
  }

  private waitForPool(mint: string): Promise<void> {
    if (this.pools.has(mint)) return Promise.resolve();
    return new Promise((resolve) => {
      const timer = setTimeout(resolve, this.deps.poolWaitMs ?? 100);
      timer.unref?.();
      const list = this.waiters.get(mint) ?? [];
      list.push(() => {
        clearTimeout(timer);
        resolve();
      });
      this.waiters.set(mint, list);
    });
  }

  async probe(input: SendReadyInput): Promise<SendReadyResult> {
    const start = input.receivedAtNs;
    const prep = this.prep.get(input.mint);
    if (!prep) return { ok: false, reason: 'not-prepared', totalUs: usSince(start, this.nowNs()) };
    const configs = this.deps.configs();
    if (!configs) return { ok: false, reason: 'no-configs', totalUs: usSince(start, this.nowNs()) };
    try {
      const t0 = this.nowNs();
      await this.waitForPool(input.mint);
      const pool = this.pools.get(input.mint);
      if (!pool) return { ok: false, reason: 'no-pool', totalUs: usSince(start, this.nowNs()) };
      const t1 = this.nowNs();
      const { bytes, t2, t3, t4 } = await this.buildAndSign(prep, pool, input.baseReserve, input.quoteReserveLamports, configs);
      this.warmInputs = { prep, pool, baseReserve: input.baseReserve, quoteReserveLamports: input.quoteReserveLamports };
      return {
        ok: true,
        totalUs: usSince(start, t4),
        waitPoolUs: usSince(t0, t1),
        stateUs: usSince(t1, t2),
        buildUs: usSince(t2, t3),
        signUs: usSince(t3, t4),
        bytes: bytes.length,
      };
    } catch (err) {
      return { ok: false, reason: 'error', totalUs: usSince(start, this.nowNs()), detail: err instanceof Error ? err.message : String(err) };
    }
  }
}
