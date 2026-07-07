import { Connection, VersionedTransaction } from '@solana/web3.js';
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

export interface SellabilityResult {
  status: SellabilityStatus;
  detail: string;
}

const PROBE_SOL = 0.02;
const PROBE_SLIPPAGE_PCT = 15;
const ATOMIC_CU_LIMIT = 600_000;

/** Errors that mean "couldn't evaluate" rather than "sell blocked". */
function isInconclusive(err: unknown): boolean {
  const s = JSON.stringify(err);
  return /AccountNotFound|InsufficientFunds|insufficient lamports|debit an account/i.test(s);
}

export class SellabilitySimulator {
  private readonly connection: Connection;
  private readonly wallet: Wallet;
  private readonly pumpAmm: PumpAmmClient;
  private readonly log = logger.child({ mod: 'sellability' });

  constructor(deps: { httpUrl: string; config: Config }) {
    this.connection = new Connection(deps.httpUrl, 'confirmed');
    this.wallet = Wallet.load(deps.config.wallet.keypairEnvVar, deps.config.mode);
    this.pumpAmm = new PumpAmmClient(deps.httpUrl);
  }

  async check(
    poolAddress: string,
    baseReserve: bigint,
    quoteReserveLamports: bigint,
  ): Promise<SellabilityResult> {
    if (this.wallet.ephemeral) {
      return { status: 'unknown', detail: 'no persistent wallet for sell simulation' };
    }
    const probeLamports = BigInt(Math.floor(PROBE_SOL * 1e9));
    if (baseReserve <= 0n || quoteReserveLamports <= 0n) {
      return { status: 'unknown', detail: 'pool reserves unavailable' };
    }
    // Constant-product estimate of base tokens the probe buy yields; sell 90% to
    // stay safely under the actually-received amount after fees/slippage.
    const baseOut = (probeLamports * baseReserve) / (quoteReserveLamports + probeLamports);
    const sellAmount = (baseOut * 90n) / 100n;
    if (sellAmount <= 0n) return { status: 'unknown', detail: 'probe too small for reserves' };

    try {
      const user = this.wallet.keypair.publicKey;
      const buyIxs = await this.pumpAmm.buildBuy(poolAddress, user, probeLamports, PROBE_SLIPPAGE_PCT);
      const sellIxs = await this.pumpAmm.buildSell(poolAddress, user, sellAmount, PROBE_SLIPPAGE_PCT);
      const bytes = await assembleSignedSwapTx([...buyIxs, ...sellIxs], {
        connection: this.connection,
        wallet: this.wallet,
        feePlan: { priorityMicroLamports: 50_000, jitoTipLamports: 0 },
        computeUnitLimit: ATOMIC_CU_LIMIT,
      });
      const sim = await this.connection.simulateTransaction(VersionedTransaction.deserialize(bytes), {
        sigVerify: false,
        replaceRecentBlockhash: true,
        commitment: 'confirmed',
      });

      if (!sim.value.err) return { status: 'pass', detail: 'atomic buy+sell simulated cleanly' };
      if (isInconclusive(sim.value.err)) {
        this.log.debug('sellability inconclusive (likely unfunded wallet)', { poolAddress, err: sim.value.err });
        return { status: 'unknown', detail: `inconclusive: ${JSON.stringify(sim.value.err)}` };
      }
      return { status: 'fail', detail: `sell leg failed: ${JSON.stringify(sim.value.err)}` };
    } catch (err) {
      return { status: 'unknown', detail: `simulation error: ${(err as Error).message}` };
    }
  }
}
