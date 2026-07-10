import {
  Connection,
  PublicKey,
  SystemProgram,
  TransactionInstruction,
  VersionedTransaction,
  type AddressLookupTableAccount,
} from '@solana/web3.js';
import type { Config } from '../config/schema.ts';
import { PROGRAM_IDS } from '../core/constants.ts';
import { Wallet } from './wallet.ts';
import { PumpAmmClient } from './pumpAmm.ts';
import { assembleSignedSwapTx } from './assemble.ts';
import { logger } from '../core/logger.ts';

/**
 * H4 sellability / honeypot probe (Section 6.1). Builds an ATOMIC buy-then-sell
 * in a single transaction and simulates it: the buy provides the tokens, the
 * sell in the same tx proves they can be sold. If the sell leg is blocked
 * (honeypot / transfer trap) the simulation fails.
 *
 * Requires a FUNDED wallet — the buy leg spends SOL in simulation, so an
 * unfunded/ephemeral wallet yields an account/funds error which we classify as
 * `unknown` (not `fail`), never a false honeypot verdict. On canonical PumpSwap
 * pools the static checks (H2 freeze, H9 transfer-fee/hook/non-transferable)
 * already cover the on-chain honeypot vectors; this adds a dynamic backstop.
 */

export type SellabilityStatus = 'pass' | 'fail' | 'unknown';
export type SellabilityReason =
  | 'tx_too_large'
  | 'account_setup_unavailable'
  | 'wallet_unfunded'
  | 'rpc_unavailable'
  | 'sell_failed'
  | 'not_run';

export interface SellabilityResult {
  status: SellabilityStatus;
  detail: string;
  reason?: SellabilityReason;
  txBytes?: number;
  usedLookupTable?: boolean;
}

const PROBE_SOL = 0.02;
const PROBE_SLIPPAGE_PCT = 15;
const ATOMIC_CU_LIMIT = 600_000;
const ATA_PROGRAM = new PublicKey(PROGRAM_IDS.ASSOCIATED_TOKEN);

export function createIdempotentAtaInstruction(
  payer: PublicKey,
  owner: PublicKey,
  mint: PublicKey,
  tokenProgram: PublicKey,
): { address: PublicKey; instruction: TransactionInstruction } {
  const address = PublicKey.findProgramAddressSync(
    [owner.toBuffer(), tokenProgram.toBuffer(), mint.toBuffer()],
    ATA_PROGRAM,
  )[0];
  return {
    address,
    instruction: new TransactionInstruction({
      programId: ATA_PROGRAM,
      keys: [
        { pubkey: payer, isSigner: true, isWritable: true },
        { pubkey: address, isSigner: false, isWritable: true },
        { pubkey: owner, isSigner: false, isWritable: false },
        { pubkey: mint, isSigner: false, isWritable: false },
        { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
        { pubkey: tokenProgram, isSigner: false, isWritable: false },
      ],
      data: Buffer.from([1]), // CreateIdempotent
    }),
  };
}

function isTxTooLarge(err: unknown): boolean {
  const s = JSON.stringify(err);
  return /encoding overruns Uint8Array|VersionedTransaction too large|transaction.*too large/i.test(s);
}

/** Normalize probe failures so policy can distinguish risk from infrastructure. */
export function classifySellabilityError(
  err: unknown,
  source: 'simulation' | 'transport' = 'simulation',
): SellabilityReason {
  const s = err instanceof Error ? `${err.name}: ${err.message}` : JSON.stringify(err);
  if (isTxTooLarge(err)) return 'tx_too_large';
  if (/InsufficientFunds|insufficient (?:lamports|funds)|debit an account|attempt to debit/i.test(s)) {
    return 'wallet_unfunded';
  }
  if (/AccountNotFound|could not find account|invalid account data|account .* does not exist/i.test(s)) {
    return 'account_setup_unavailable';
  }
  if (source === 'transport' || /429|timed? out|fetch failed|network|ECONN|socket|service unavailable/i.test(s)) {
    return 'rpc_unavailable';
  }
  return 'sell_failed';
}

export class SellabilitySimulator {
  private readonly connection: Connection;
  private readonly wallet: Wallet;
  private readonly pumpAmm: PumpAmmClient;
  private readonly lookupTableAddress: string | undefined;
  private lookupTable: AddressLookupTableAccount | null | undefined;
  private readonly log = logger.child({ mod: 'sellability' });

  constructor(deps: { httpUrl: string; config: Config }) {
    this.connection = new Connection(deps.httpUrl, 'confirmed');
    this.wallet = Wallet.load(deps.config.wallet.keypairEnvVar, deps.config.mode);
    this.pumpAmm = new PumpAmmClient(deps.httpUrl);
    this.lookupTableAddress = deps.config.guardrails.sellabilityLookupTableAddress;
  }

  async check(
    poolAddress: string,
    baseReserve: bigint,
    quoteReserveLamports: bigint,
    baseMint: string,
    baseIsToken2022 = false,
  ): Promise<SellabilityResult> {
    if (this.wallet.ephemeral) {
      return { status: 'unknown', reason: 'wallet_unfunded', detail: 'no persistent wallet for sell simulation' };
    }
    const probeLamports = BigInt(Math.floor(PROBE_SOL * 1e9));
    if (baseReserve <= 0n || quoteReserveLamports <= 0n) {
      return { status: 'unknown', reason: 'account_setup_unavailable', detail: 'pool reserves unavailable' };
    }
    // Constant-product estimate of base tokens the probe buy yields; sell 90% to
    // stay safely under the actually-received amount after fees/slippage.
    const baseOut = (probeLamports * baseReserve) / (quoteReserveLamports + probeLamports);
    const sellAmount = (baseOut * 90n) / 100n;
    if (sellAmount <= 0n) {
      return { status: 'unknown', reason: 'account_setup_unavailable', detail: 'probe too small for reserves' };
    }

    let usedLookupTable = false;
    let txBytes: number | undefined;
    try {
      const user = this.wallet.keypair.publicKey;
      // Fail before expensive instruction construction when the wallet cannot
      // fund the simulated buy plus a conservative transaction-fee reserve.
      let balance: number;
      try {
        balance = await this.connection.getBalance(user, 'confirmed');
      } catch (err) {
        return { status: 'unknown', reason: 'rpc_unavailable', detail: `wallet preflight failed: ${(err as Error).message}` };
      }
      const requiredLamports = Number(probeLamports) + 5_000_000;
      if (balance < requiredLamports) {
        return {
          status: 'unknown',
          reason: 'wallet_unfunded',
          detail: `wallet balance ${balance} below probe requirement ${requiredLamports} lamports`,
        };
      }
      const buyIxs = await this.pumpAmm.buildBuy(poolAddress, user, probeLamports, PROBE_SLIPPAGE_PCT);
      const sellIxs = await this.pumpAmm.buildSell(poolAddress, user, sellAmount, PROBE_SLIPPAGE_PCT);
      const tokenProgram = new PublicKey(baseIsToken2022 ? PROGRAM_IDS.TOKEN_2022 : PROGRAM_IDS.TOKEN);
      const ataSetup = createIdempotentAtaInstruction(user, user, new PublicKey(baseMint), tokenProgram);
      let ataExists: boolean;
      try {
        ataExists = Boolean(await this.connection.getAccountInfo(ataSetup.address, 'confirmed'));
      } catch (err) {
        return { status: 'unknown', reason: 'rpc_unavailable', detail: `ATA preflight failed: ${(err as Error).message}` };
      }
      const sdkCreatesAta = buyIxs.some((ix) => ix.programId.equals(ATA_PROGRAM));
      const setupIxs = !ataExists && !sdkCreatesAta ? [ataSetup.instruction] : [];
      const lookupTable = await this.getLookupTable();
      usedLookupTable = Boolean(lookupTable);
      const bytes = await assembleSignedSwapTx([...setupIxs, ...buyIxs, ...sellIxs], {
        connection: this.connection,
        wallet: this.wallet,
        feePlan: { priorityMicroLamports: 50_000, jitoTipLamports: 0 },
        computeUnitLimit: ATOMIC_CU_LIMIT,
        ...(lookupTable ? { addressLookupTableAccounts: [lookupTable] } : {}),
      });
      txBytes = bytes.length;
      this.log.debug('sellability tx assembled', { poolAddress, sellabilityTxBytes: txBytes, usedLookupTable });
      const sim = await this.connection.simulateTransaction(VersionedTransaction.deserialize(bytes), {
        sigVerify: false,
        replaceRecentBlockhash: true,
        commitment: 'confirmed',
      });

      if (!sim.value.err) {
        return {
          status: 'pass',
          detail: 'atomic buy+sell simulated cleanly',
          txBytes,
          usedLookupTable,
        };
      }
      const reason = classifySellabilityError(sim.value.err, 'simulation');
      if (reason !== 'sell_failed') {
        this.log.debug('sellability probe inconclusive', { poolAddress, reason, err: sim.value.err });
        return {
          status: 'unknown',
          reason,
          detail: `${reason}: ${JSON.stringify(sim.value.err)}`,
          txBytes,
          usedLookupTable,
        };
      }
      return {
        status: 'fail',
        reason: 'sell_failed',
        detail: `sell leg failed: ${JSON.stringify(sim.value.err)}`,
        txBytes,
        usedLookupTable,
      };
    } catch (err) {
      const reason = classifySellabilityError(err, 'transport');
      if (reason !== 'sell_failed') {
        return {
          status: 'unknown',
          reason,
          detail: `simulation error: ${(err as Error).message}`,
          ...(txBytes !== undefined ? { txBytes } : {}),
          usedLookupTable,
        };
      }
      return {
        status: 'fail',
        reason: 'sell_failed',
        detail: `simulation error: ${(err as Error).message}`,
        ...(txBytes !== undefined ? { txBytes } : {}),
        usedLookupTable,
      };
    }
  }

  private async getLookupTable(): Promise<AddressLookupTableAccount | null> {
    if (!this.lookupTableAddress) return null;
    if (this.lookupTable !== undefined) return this.lookupTable;
    try {
      const res = await this.connection.getAddressLookupTable(new PublicKey(this.lookupTableAddress));
      this.lookupTable = res.value;
      if (!this.lookupTable) {
        this.log.warn('configured sellability lookup table not found', { lookupTable: this.lookupTableAddress });
      }
    } catch (err) {
      this.lookupTable = null;
      this.log.warn('failed to load sellability lookup table', { lookupTable: this.lookupTableAddress, err });
    }
    return this.lookupTable;
  }
}
