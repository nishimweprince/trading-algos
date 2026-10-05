/**
 * Send-ready probe: how long from the landed migrate reaching this process to
 * a SIGNED post-migration PumpSwap buy, built the way the trader builds one.
 * Replaces the old trade-local probe, which timed a remote HTTPS call that
 * built a bonding-curve completion buy we never intend to send (~300 ms).
 *
 * Hot path, zero RPC, zero waiting:
 *   state — the pool account, mint account and the user's ATAs (all prepared
 *           at admission), PumpSwap configs (kept warm), reserves from the
 *           migrate tx's own token balances;
 *   build — the SDK's offline `buyQuoteInput` (remaining accounts, fees);
 *   sign  — v0 assembly with a cached blockhash, compute budget and tip,
 *           signed by an ephemeral never-funded key.
 * The bytes are dropped. Nothing here can send.
 *
 * The pool account does not exist until the migrate creates it, and its push
 * arrived ~2.7 ms AFTER the migrate tx on Frankfurt. Waiting for it was the
 * largest stage, so the pool is SYNTHESIZED at admission instead: a recent
 * real canonical pool as the template, with every per-mint field the buy
 * reads re-derived — base mint, LP mint (lpMintPda), both vaults (pool ATAs),
 * creator (pump `pool-authority` PDA of the mint), coin creator (the curve's
 * creator) — and encoded with the SDK's own account coder, never by hand.
 * `virtualQuoteReserves` comes from the template (≈17.58 SOL on every pool
 * seen, varying by a few thousand lamports); it only moves the quote, not
 * the transaction's accounts. A pushed pool is used whenever it already
 * arrived. When the real pool lands later, `verify()` rebuilds from it off the
 * hot path and checks the instructions' accounts are identical, so every
 * synthesized build is audited rather than assumed.
 *
 * Keep-warm: a probe runs once every few minutes, so V8 never keeps the
 * build / sign code hot (live p50 was ~17 ms against ~2 ms warm). `warm()`
 * re-runs build → sign on the last real inputs and discards the result; the
 * worker calls it on a short interval, like the trader's blockhash keep-warm.
 */
import BN from 'bn.js';
import { PUMP_AMM_SDK, PumpAmmSdk, getPumpAmmProgram, lpMintPda, pumpPoolAuthorityPda } from '@pump-fun/pump-swap-sdk';
import { Connection, PublicKey, type TransactionInstruction } from '@solana/web3.js';
import { deriveAta } from '../core/ata.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../core/constants.ts';
import { assembleSignedSwapTx } from '../executor/assemble.ts';
import type { FeePlan } from '../executor/fees.ts';
import { buildSwapState, toAccountInfo, type AmmConfigs, type RawAccountRead } from '../executor/swapState.ts';
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

export type PoolSource = 'pushed' | 'synthesized';

export type SendReadyResult =
  | { ok: true; totalUs: number; stateUs: number; buildUs: number; signUs: number; bytes: number; poolSource: PoolSource }
  | { ok: false; reason: 'not-prepared' | 'no-configs' | 'no-pool' | 'error'; totalUs: number; detail?: string };

export interface SynthCheck {
  /** Every instruction's program and account metas identical to a build from the real pool. */
  match: boolean;
  /** Real − synthesized virtualQuoteReserves, lamports. */
  vDeltaLamports: bigint;
}

export interface SendReadyDeps {
  configs: () => AmmConfigs | null;
  blockhash: () => Promise<string>;
  wallet: Wallet;
  feePlan: FeePlan;
  tipAccount: () => string | undefined;
  quoteLamports: bigint;
  slippagePct: number;
  nowNs?: () => bigint;
}

const usSince = (startNs: bigint, nowNs: bigint): number => Number(nowNs - startNs) / 1_000;

/** The SDK's own Anchor account coder (its IDL); the connection is never used to encode. */
const poolCoder = getPumpAmmProgram(new Connection('http://127.0.0.1:1')).coder.accounts;

/**
 * A pump.fun mint account as the SDK's buy reads it, without RPC: pump mints
 * are created with 1,000,000,000 × 10^6 supply and 6 decimals; the token
 * program comes from the migrate tx's own token balances. Supply can shrink
 * later (seen: 968.5M on a migrated mint, likely buyback burns), and only
 * moves the buy's fee tier / quote — never its accounts. Audited off the hot
 * path: owner + decimals must match; the supply delta is recorded.
 */
export const PUMP_MINT_SUPPLY = 1_000_000_000_000_000n;
export function pumpMintAccount(tokenProgram: string, decimals = 6): RawAccountRead {
  const buf = Buffer.alloc(82);
  buf.writeBigUInt64LE(PUMP_MINT_SUPPLY, 36); // supply
  buf.writeUInt8(decimals, 44);
  buf.writeUInt8(1, 45); // is_initialized
  return { data: buf.toString('base64'), owner: tokenProgram, lamports: 0 };
}

/**
 * Fee-recipient accounts the SDK picks AT RANDOM per build (protocol, reserved
 * and buyback recipients from the global config, plus their WSOL accounts):
 * two builds of the same pool legitimately differ there.
 */
function feeRecipientSet(configs: AmmConfigs): Set<string> {
  const g = configs.globalConfig as unknown as Record<string, unknown>;
  const keys = new Set<string>();
  for (const field of ['protocolFeeRecipients', 'reservedFeeRecipients', 'buybackFeeRecipients', 'reservedFeeRecipient']) {
    const v = g[field];
    for (const k of Array.isArray(v) ? v : v ? [v] : []) {
      const pk = (k as { toBase58?: () => string }).toBase58?.();
      if (!pk || pk === PublicKey.default.toBase58()) continue;
      keys.add(pk);
      keys.add(deriveAta(pk, WSOL_MINT, false));
      keys.add(deriveAta(pk, WSOL_MINT, true));
    }
  }
  return keys;
}

/**
 * Program + account metas of every instruction: what makes two builds the same
 * transaction shape. Randomly chosen fee recipients are normalised.
 */
function accountsSignature(ixs: readonly TransactionInstruction[], feeRecipients: Set<string>): string {
  return JSON.stringify(
    ixs.map((ix) => [
      ix.programId.toBase58(),
      ix.keys.map((k) => {
        const key = k.pubkey.toBase58();
        return `${feeRecipients.has(key) ? 'FEE_RECIPIENT' : key}:${k.isSigner ? 1 : 0}${k.isWritable ? 1 : 0}`;
      }),
    ]),
  );
}

export class SendReadyProbe {
  private readonly deps: SendReadyDeps;
  private readonly offline = new PumpAmmSdk();
  private readonly prep = new Map<string, SendReadyPrep>();
  private readonly pools = new Map<string, RawAccountRead>();
  private readonly synth = new Map<string, RawAccountRead>();
  private readonly coinCreators = new Map<string, string>();
  /** Synthesized builds awaiting the real pool for verification. */
  private readonly pendingChecks = new Map<string, { accounts: string; pool: RawAccountRead; baseReserve: bigint; quoteReserveLamports: bigint }>();
  private readonly nowNs: () => bigint;
  /** Unused by assembly when a blockhash provider is supplied; satisfies the type. */
  private readonly connection = null as unknown as Connection;
  private template: RawAccountRead | null = null;
  /** Last real build inputs, replayed by warm(). */
  private warmInputs: { prep: SendReadyPrep; pool: RawAccountRead; baseReserve: bigint; quoteReserveLamports: bigint } | null = null;
  warmRuns = 0;

  constructor(deps: SendReadyDeps) {
    this.deps = deps;
    this.nowNs = deps.nowNs ?? (() => process.hrtime.bigint());
  }

  /** A real canonical pool account to synthesize from (boot seed, then each new pool's creation state). */
  setPoolTemplate(account: RawAccountRead): void {
    if (account.owner !== PROGRAM_IDS.PUMP_SWAP) return;
    try {
      PUMP_AMM_SDK.decodePool(toAccountInfo(account));
      this.template = account;
    } catch {
      /* not a pool */
    }
  }

  get hasTemplate(): boolean {
    return this.template !== null;
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

  /**
   * The pool this mint's migrate will create, from the template with every
   * per-mint field the buy reads re-derived. Null without a template or a
   * coin creator.
   */
  async synthesizePool(mint: string, prep: SendReadyPrep, coinCreator: string | null): Promise<RawAccountRead | null> {
    const template = this.template;
    if (!template || !coinCreator) return null;
    const mintKey = new PublicKey(mint);
    const poolKey = new PublicKey(prep.poolKey);
    const baseIs2022 = prep.baseMint.owner === PROGRAM_IDS.TOKEN_2022;
    const decoded = PUMP_AMM_SDK.decodePool(toAccountInfo(template));
    const patched = {
      ...decoded,
      creator: pumpPoolAuthorityPda(mintKey),
      baseMint: mintKey,
      quoteMint: new PublicKey(WSOL_MINT),
      lpMint: lpMintPda(poolKey),
      poolBaseTokenAccount: new PublicKey(deriveAta(prep.poolKey, mint, baseIs2022)),
      poolQuoteTokenAccount: new PublicKey(deriveAta(prep.poolKey, WSOL_MINT, false)),
      coinCreator: new PublicKey(coinCreator),
    };
    const encoded = await poolCoder.encode('pool', patched);
    const bytes = Buffer.from(template.data, 'base64');
    encoded.copy(bytes, 0);
    return { data: bytes.toString('base64'), owner: PROGRAM_IDS.PUMP_SWAP, lamports: template.lamports };
  }

  private async buildIxs(prep: SendReadyPrep, pool: RawAccountRead, baseReserve: bigint, quoteReserveLamports: bigint, configs: AmmConfigs) {
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
    return { ixs, t2, t3: this.nowNs() };
  }

  private async buildAndSign(
    prep: SendReadyPrep,
    pool: RawAccountRead,
    baseReserve: bigint,
    quoteReserveLamports: bigint,
    configs: AmmConfigs,
  ): Promise<{ bytes: Uint8Array; ixs: TransactionInstruction[]; t2: bigint; t3: bigint; t4: bigint }> {
    const { ixs, t2, t3 } = await this.buildIxs(prep, pool, baseReserve, quoteReserveLamports, configs);
    const tipAccount = this.deps.tipAccount();
    const bytes = await assembleSignedSwapTx(ixs, {
      connection: this.connection,
      wallet: this.deps.wallet,
      feePlan: this.deps.feePlan,
      ...(tipAccount ? { jitoTipAccount: tipAccount } : {}),
      blockhashProvider: this.deps.blockhash,
    });
    return { bytes, ixs, t2, t3, t4: this.nowNs() };
  }

  /**
   * Off the hot path (admission): everything that exists before the migrate,
   * including the synthesized pool when a template and coin creator are known.
   */
  async prepare(mint: string, prep: SendReadyPrep, coinCreator: string | null = null): Promise<void> {
    this.prep.set(mint, prep);
    if (coinCreator) this.coinCreators.set(mint, coinCreator);
    const pool = await this.synthesizePool(mint, prep, coinCreator).catch(() => null);
    if (pool && this.prep.has(mint)) this.synth.set(mint, pool);
  }

  isPrepared(mint: string): boolean {
    return this.prep.has(mint);
  }

  hasSynthesized(mint: string): boolean {
    return this.synth.has(mint);
  }

  /** The pool account was pushed (created by the migrate tx, then rewritten by swaps). */
  onPool(mint: string, account: RawAccountRead): void {
    if (!this.pools.has(mint)) this.setPoolTemplate(account); // a fresh pool's creation state
    this.pools.set(mint, account);
  }

  release(mint: string): void {
    this.prep.delete(mint);
    this.pools.delete(mint);
    this.synth.delete(mint);
    this.coinCreators.delete(mint);
    this.pendingChecks.delete(mint);
  }

  async probe(input: SendReadyInput): Promise<SendReadyResult> {
    const start = input.receivedAtNs;
    const prep = this.prep.get(input.mint);
    if (!prep) return { ok: false, reason: 'not-prepared', totalUs: usSince(start, this.nowNs()) };
    const configs = this.deps.configs();
    if (!configs) return { ok: false, reason: 'no-configs', totalUs: usSince(start, this.nowNs()) };
    // No waiting: the pushed pool if it already arrived, else the synthesized one
    // (synthesized now — CPU only — if the template arrived after admission).
    const pushed = this.pools.get(input.mint);
    const pool =
      pushed ??
      this.synth.get(input.mint) ??
      (await this.synthesizePool(input.mint, prep, this.coinCreators.get(input.mint) ?? null).catch(() => null));
    if (!pool) return { ok: false, reason: 'no-pool', totalUs: usSince(start, this.nowNs()) };
    const poolSource: PoolSource = pushed ? 'pushed' : 'synthesized';
    try {
      const t1 = this.nowNs();
      const { bytes, ixs, t2, t3, t4 } = await this.buildAndSign(prep, pool, input.baseReserve, input.quoteReserveLamports, configs);
      this.warmInputs = { prep, pool, baseReserve: input.baseReserve, quoteReserveLamports: input.quoteReserveLamports };
      if (poolSource === 'synthesized') {
        this.pendingChecks.set(input.mint, {
          accounts: accountsSignature(ixs, feeRecipientSet(configs)),
          pool,
          baseReserve: input.baseReserve,
          quoteReserveLamports: input.quoteReserveLamports,
        });
      }
      return {
        ok: true,
        totalUs: usSince(start, t4),
        stateUs: usSince(t1, t2),
        buildUs: usSince(t2, t3),
        signUs: usSince(t3, t4),
        bytes: bytes.length,
        poolSource,
      };
    } catch (err) {
      return { ok: false, reason: 'error', totalUs: usSince(start, this.nowNs()), detail: err instanceof Error ? err.message : String(err) };
    }
  }

  /** A synthesized build is waiting for the real pool. */
  awaitingCheck(mint: string): boolean {
    return this.pendingChecks.has(mint);
  }

  /**
   * Off the hot path, once the real pool has been pushed: rebuild from it and
   * compare the instructions' accounts with the synthesized build. Null when
   * there is nothing to check yet.
   */
  async verify(mint: string): Promise<SynthCheck | null> {
    const pending = this.pendingChecks.get(mint);
    const real = this.pools.get(mint);
    const prep = this.prep.get(mint);
    const configs = this.deps.configs();
    if (!pending || !real || !prep || !configs) return null;
    this.pendingChecks.delete(mint);
    const { ixs } = await this.buildIxs(prep, real, pending.baseReserve, pending.quoteReserveLamports, configs);
    const realPool = PUMP_AMM_SDK.decodePool(toAccountInfo(real));
    const synthPool = PUMP_AMM_SDK.decodePool(toAccountInfo(pending.pool));
    return {
      match: accountsSignature(ixs, feeRecipientSet(configs)) === pending.accounts,
      vDeltaLamports: BigInt(realPool.virtualQuoteReserves.toString()) - BigInt(synthPool.virtualQuoteReserves.toString()),
    };
  }
}
