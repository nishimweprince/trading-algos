import { Connection, PublicKey, type TransactionInstruction } from '@solana/web3.js';
import { OnlinePumpAmmSdk, PumpAmmSdk } from '@pump-fun/pump-swap-sdk';
import { PROGRAM_IDS } from '../core/constants.ts';
import BN from 'bn.js';
import { WHITELISTED_PROGRAM_IDS } from '../core/constants.ts';
import { createFailoverFetch } from '../core/rpc.ts';

/**
 * PumpSwap swap construction via the official `@pump-fun/pump-swap-sdk`.
 *
 * We adopt the SDK for the swap instructions specifically because the deployed
 * program requires `remaining_accounts` that are absent from both the public and
 * on-chain IDLs and vary per transaction — hand-rolling them in fund-handling
 * code would be a guess. Verified live: the SDK emits a 26-account buy / 24-
 * account sell with the correct discriminators and only whitelisted programs.
 *
 * Everything else (pool discovery/decode, pricing, guardrails) stays on our own
 * verified code; the SDK is used only to assemble the swap itself.
 */
export class PumpAmmClient {
  private readonly online: OnlinePumpAmmSdk;
  private readonly offline: PumpAmmSdk;

  constructor(
    httpUrl: string,
    commitment: 'processed' | 'confirmed' = 'confirmed',
    opts?: { fallbackHttpUrls?: readonly string[]; timeoutMs?: number },
  ) {
    // State reads at the same commitment the enricher used to accept the pool;
    // at 'confirmed' a pool created 1–2 slots ago is "Pool account not found".
    // A fallback list + timeout gives this the same immediate-failover
    // behaviour as RpcClient instead of hanging on a single stalled endpoint.
    const urls = [httpUrl, ...(opts?.fallbackHttpUrls ?? [])];
    const connection =
      urls.length > 1 || opts?.timeoutMs !== undefined
        ? new Connection(httpUrl, {
            commitment,
            fetch: createFailoverFetch(urls, { timeoutMs: opts?.timeoutMs ?? 5_000 }),
          })
        : new Connection(httpUrl, commitment);
    this.online = new OnlinePumpAmmSdk(connection);
    this.offline = new PumpAmmSdk();
  }

  /** Buy: spend `quoteLamports` of WSOL for base tokens, bounded by slippage %. */
  async buildBuy(
    poolAddress: string,
    user: PublicKey,
    quoteLamports: bigint,
    slippagePct: number,
  ): Promise<TransactionInstruction[]> {
    const state = await this.online.swapSolanaState(new PublicKey(poolAddress), user);
    const ixs = await this.offline.buyQuoteInput(state, new BN(quoteLamports.toString()), slippagePct);
    assertWhitelisted(ixs);
    return ixs;
  }

  /**
   * Same as buildBuy, plus the pool reserves the quote was built from — the
   * live entry compares them to the enrichment snapshot (entry move gate).
   */
  async buildBuyQuoted(
    poolAddress: string,
    user: PublicKey,
    quoteLamports: bigint,
    slippagePct: number,
  ): Promise<{ ixs: TransactionInstruction[]; baseReserve: bigint; quoteReserveLamports: bigint }> {
    const state = await this.online.swapSolanaState(new PublicKey(poolAddress), user);
    const ixs = await this.offline.buyQuoteInput(state, new BN(quoteLamports.toString()), slippagePct);
    assertWhitelisted(ixs);
    return {
      ixs,
      baseReserve: BigInt(state.poolBaseAmount.toString()),
      quoteReserveLamports: BigInt(state.poolQuoteAmount.toString()),
    };
  }

  /** Sell `baseAmount` base tokens for WSOL, bounded by slippage %. */
  async buildSell(
    poolAddress: string,
    user: PublicKey,
    baseAmount: bigint,
    slippagePct: number,
  ): Promise<TransactionInstruction[]> {
    const state = await this.online.swapSolanaState(new PublicKey(poolAddress), user);
    const ixs = await this.offline.sellBaseInput(state, new BN(baseAmount.toString()), slippagePct);
    assertWhitelisted(ixs);
    return ixs;
  }

  /**
   * H4 probe legs from a single `swapSolanaState` read. The buy ix is
   * `buy(base_amount_out, max_quote_in)`, so the wallet receives exactly
   * `base_amount_out` when it lands — the sell leg therefore moves that exact
   * amount, decoded from the buy ix data (the on-chain ABI, stable across SDK
   * versions), not a fee-less constant-product estimate from an older reserve
   * snapshot (which, after any up-move inside the bound, exceeded what the buy
   * returned and failed the sell with token InsufficientFunds — a false
   * honeypot verdict). Quoting itself stays inside the SDK so its fee model
   * (quote-mint schedules, mayhem mode, configurable creator fee as of 1.20)
   * is never re-implemented here.
   */
  async buildProbeSwap(
    poolAddress: string,
    user: PublicKey,
    quoteLamports: bigint,
    slippagePct: number,
  ): Promise<ProbeSwap> {
    const state = await this.online.swapSolanaState(new PublicKey(poolAddress), user);
    const buyIxs = await this.offline.buyQuoteInput(state, new BN(quoteLamports.toString()), slippagePct);
    assertWhitelisted(buyIxs);
    const base = decodeBuyBaseOut(buyIxs);
    const sellIxs = await this.offline.sellBaseInput(state, new BN(base.toString()), slippagePct);
    assertWhitelisted(sellIxs);
    return {
      buyIxs,
      sellIxs,
      base,
      stateQuoteReserveLamports: BigInt(state.poolQuoteAmount.toString()),
    };
  }
}

/** Anchor discriminator of PumpSwap `buy` (sha256("global:buy")[0..8]). */
const BUY_DISCRIMINATOR = Buffer.from('66063d1201daebea', 'hex');

/** `base_amount_out` (u64 LE at data[8..16]) of the PumpSwap buy ix in a built leg. */
export function decodeBuyBaseOut(ixs: readonly TransactionInstruction[]): bigint {
  const buy = ixs.find(
    (ix) => ix.programId.toBase58() === PROGRAM_IDS.PUMP_SWAP && ix.data.subarray(0, 8).equals(BUY_DISCRIMINATOR),
  );
  if (!buy || buy.data.length < 16) throw new Error('swap builder produced no PumpSwap buy instruction');
  return Buffer.from(buy.data.subarray(8, 16)).readBigUInt64LE();
}

/** Buy + sell legs of the H4 sellability probe, built from ONE pool-state read. */
export interface ProbeSwap {
  buyIxs: TransactionInstruction[];
  sellIxs: TransactionInstruction[];
  /** Exact base_amount_out the buy ix delivers — what the sell leg moves. */
  base: bigint;
  /** Pool quote reserve at the state read (vs the enrichment snapshot = early move). */
  stateQuoteReserveLamports: bigint;
}

/**
 * Enforce the keypair-usage policy (Section 8) on SDK output: refuse to sign a
 * transaction that invokes any non-whitelisted program. Defence against an SDK
 * change slipping in an unexpected program.
 */
function assertWhitelisted(ixs: TransactionInstruction[]): void {
  for (const ix of ixs) {
    const id = ix.programId.toBase58();
    if (!WHITELISTED_PROGRAM_IDS.includes(id)) {
      throw new Error(`swap builder produced a non-whitelisted program instruction: ${id}`);
    }
  }
}
