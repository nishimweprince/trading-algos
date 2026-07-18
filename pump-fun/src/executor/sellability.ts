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
  | 'buy_only_ok'
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

/**
 * Outcome of assembling + simulating one probe. `simErr === null` is a clean
 * simulation; a non-null `simErr` is an on-chain program error; `assembleErr`
 * is a build/transport failure (e.g. the 1232-byte overflow).
 */
type ProbeRun =
  | { txBytes: number; simErr: unknown | null }
  | { assembleErr: unknown; txBytes?: number };

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
  private readonly buyOnlyBackstop: boolean;
  private lookupTable: AddressLookupTableAccount | null | undefined;
  private readonly log = logger.child({ mod: 'sellability' });

  constructor(deps: { httpUrl: string; config: Config }) {
    this.connection = new Connection(deps.httpUrl, 'confirmed');
    this.wallet = Wallet.load(deps.config.wallet.keypairEnvVar, deps.config.mode);
    this.pumpAmm = new PumpAmmClient(deps.httpUrl);
    this.lookupTableAddress = deps.config.guardrails.sellabilityLookupTableAddress;
    this.buyOnlyBackstop = deps.config.guardrails.sellabilityBuyOnlyBackstop;
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

      // Primary probe: atomic buy+sell in one tx proves sellability directly.
      const atomic = await this.runProbe([...setupIxs, ...buyIxs, ...sellIxs], lookupTable);
      if (atomic.txBytes !== undefined) {
        txBytes = atomic.txBytes;
        this.log.debug('sellability tx assembled', { poolAddress, sellabilityTxBytes: txBytes, usedLookupTable });
      }

      const atomicErr = 'assembleErr' in atomic ? atomic.assembleErr : atomic.simErr;
      if (atomicErr === null) {
        return {
          status: 'pass',
          detail: 'atomic buy+sell simulated cleanly',
          usedLookupTable,
          ...(txBytes !== undefined ? { txBytes } : {}),
        };
      }
      const atomicReason = classifySellabilityError(
        atomicErr,
        'assembleErr' in atomic ? 'transport' : 'simulation',
      );

      // When the atomic probe overflows the 1232-byte tx limit — the dominant H4
      // "unknown" cause — fall back to simulating the buy leg alone. A clean buy
      // proves the pool is real, buyable, and the account setup lands; the
      // sell-block honeypot vectors are already covered on-chain by H2 (freeze)
      // and H9 (Token-2022 traps), which the guardrail engine requires to pass
      // before admitting this as a relaxed-risk accept.
      if (atomicReason === 'tx_too_large' && this.buyOnlyBackstop) {
        const buyOnly = await this.runProbe([...setupIxs, ...buyIxs], lookupTable);
        const buyErr = 'assembleErr' in buyOnly ? buyOnly.assembleErr : buyOnly.simErr;
        if (buyErr === null) {
          this.log.debug('sellability buy-leg backstop clean', {
            poolAddress,
            atomicTxBytes: txBytes,
            buyTxBytes: buyOnly.txBytes,
          });
          const backstopBytes = buyOnly.txBytes ?? txBytes;
          return {
            status: 'unknown',
            reason: 'buy_only_ok',
            detail:
              `atomic probe too large (${txBytes ?? '?'}B); buy leg simulated cleanly — ` +
              `sell safety covered by H2/H9 static checks`,
            usedLookupTable,
            ...(backstopBytes !== undefined ? { txBytes: backstopBytes } : {}),
          };
        }
        // Buy leg also failed: a failing buy proves nothing about sellability, so
        // keep it inconclusive rather than a false honeypot verdict.
        const buyReason = classifySellabilityError(
          buyErr,
          'assembleErr' in buyOnly ? 'transport' : 'simulation',
        );
        const reason = buyReason === 'tx_too_large' ? 'tx_too_large' : 'account_setup_unavailable';
        return {
          status: 'unknown',
          reason,
          detail: `atomic probe too large; buy-leg backstop inconclusive (${buyReason}): ${errText(buyErr)}`,
          usedLookupTable,
          ...(txBytes !== undefined ? { txBytes } : {}),
        };
      }

      if (atomicReason !== 'sell_failed') {
        this.log.debug('sellability probe inconclusive', { poolAddress, reason: atomicReason, err: atomicErr });
        return {
          status: 'unknown',
          reason: atomicReason,
          detail: `${atomicReason}: ${errText(atomicErr)}`,
          usedLookupTable,
          ...(txBytes !== undefined ? { txBytes } : {}),
        };
      }
      return {
        status: 'fail',
        reason: 'sell_failed',
        detail: `sell leg failed: ${errText(atomicErr)}`,
        usedLookupTable,
        ...(txBytes !== undefined ? { txBytes } : {}),
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

  /**
   * Assemble + simulate a single probe instruction list. Returns the simulated
   * error (null = clean) with the serialized byte count, or the assemble/transport
   * error when the tx can't even be built or sent (e.g. the 1232-byte overflow,
   * raised either by web3.js serialization or the RPC's own size reject).
   */
  private async runProbe(
    ixs: TransactionInstruction[],
    lookupTable: AddressLookupTableAccount | null,
  ): Promise<ProbeRun> {
    let bytes: Uint8Array;
    try {
      bytes = await assembleSignedSwapTx(ixs, {
        connection: this.connection,
        wallet: this.wallet,
        feePlan: { priorityMicroLamports: 50_000, jitoTipLamports: 0 },
        computeUnitLimit: ATOMIC_CU_LIMIT,
        ...(lookupTable ? { addressLookupTableAccounts: [lookupTable] } : {}),
      });
    } catch (err) {
      return { assembleErr: err };
    }
    const txBytes = bytes.length;
    try {
      const sim = await this.connection.simulateTransaction(VersionedTransaction.deserialize(bytes), {
        sigVerify: false,
        replaceRecentBlockhash: true,
        commitment: 'confirmed',
      });
      return { txBytes, simErr: sim.value.err ?? null };
    } catch (err) {
      return { assembleErr: err, txBytes };
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

function errText(err: unknown): string {
  if (err instanceof Error) return err.message;
  try {
    return JSON.stringify(err);
  } catch {
    return String(err);
  }
}
