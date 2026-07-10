import { Connection, PublicKey, VersionedTransaction, type AddressLookupTableAccount } from '@solana/web3.js';
import type { Config } from '../config/schema.ts';
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
export type SellabilityReason = 'tx_too_large' | 'inconclusive' | 'sell_failed' | 'not_run';

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

/** Errors that mean "couldn't evaluate" rather than "sell blocked". */
function isInconclusive(err: unknown): boolean {
  const s = JSON.stringify(err);
  return /AccountNotFound|InsufficientFunds|insufficient lamports|debit an account/i.test(s);
}

function isTxTooLarge(err: unknown): boolean {
  const s = JSON.stringify(err);
  return /encoding overruns Uint8Array|VersionedTransaction too large|transaction.*too large/i.test(s);
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
  ): Promise<SellabilityResult> {
    if (this.wallet.ephemeral) {
      return { status: 'unknown', reason: 'inconclusive', detail: 'no persistent wallet for sell simulation' };
    }
    const probeLamports = BigInt(Math.floor(PROBE_SOL * 1e9));
    if (baseReserve <= 0n || quoteReserveLamports <= 0n) {
      return { status: 'unknown', reason: 'inconclusive', detail: 'pool reserves unavailable' };
    }
    // Constant-product estimate of base tokens the probe buy yields; sell 90% to
    // stay safely under the actually-received amount after fees/slippage.
    const baseOut = (probeLamports * baseReserve) / (quoteReserveLamports + probeLamports);
    const sellAmount = (baseOut * 90n) / 100n;
    if (sellAmount <= 0n) return { status: 'unknown', reason: 'inconclusive', detail: 'probe too small for reserves' };

    try {
      const user = this.wallet.keypair.publicKey;
      const buyIxs = await this.pumpAmm.buildBuy(poolAddress, user, probeLamports, PROBE_SLIPPAGE_PCT);
      const sellIxs = await this.pumpAmm.buildSell(poolAddress, user, sellAmount, PROBE_SLIPPAGE_PCT);
      const lookupTable = await this.getLookupTable();
      const usedLookupTable = Boolean(lookupTable);
      const bytes = await assembleSignedSwapTx([...buyIxs, ...sellIxs], {
        connection: this.connection,
        wallet: this.wallet,
        feePlan: { priorityMicroLamports: 50_000, jitoTipLamports: 0 },
        computeUnitLimit: ATOMIC_CU_LIMIT,
        ...(lookupTable ? { addressLookupTableAccounts: [lookupTable] } : {}),
      });
      const txBytes = bytes.length;
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
      if (isTxTooLarge(sim.value.err)) {
        return {
          status: 'unknown',
          reason: 'tx_too_large',
          detail: `transaction too large: ${JSON.stringify(sim.value.err)}`,
          txBytes,
          usedLookupTable,
        };
      }
      if (isInconclusive(sim.value.err)) {
        this.log.debug('sellability inconclusive (likely unfunded wallet)', { poolAddress, err: sim.value.err });
        return {
          status: 'unknown',
          reason: 'inconclusive',
          detail: `inconclusive: ${JSON.stringify(sim.value.err)}`,
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
      if (isTxTooLarge(err)) {
        return {
          status: 'unknown',
          reason: 'tx_too_large',
          detail: `simulation error: ${(err as Error).message}`,
          usedLookupTable: false,
        };
      }
      return {
        status: 'unknown',
        reason: 'inconclusive',
        detail: `simulation error: ${(err as Error).message}`,
        usedLookupTable: false,
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
