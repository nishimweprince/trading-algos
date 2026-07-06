import { Connection, PublicKey, type TransactionInstruction } from '@solana/web3.js';
import { OnlinePumpAmmSdk, PumpAmmSdk } from '@pump-fun/pump-swap-sdk';
import BN from 'bn.js';
import { WHITELISTED_PROGRAM_IDS } from '../core/constants.ts';

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

  constructor(httpUrl: string) {
    const connection = new Connection(httpUrl, 'confirmed');
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
