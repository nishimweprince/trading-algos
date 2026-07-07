import { Connection } from '@solana/web3.js';
import type { Config } from '../config/schema.ts';
import type { RpcClient } from '../core/rpc.ts';
import { logger } from '../core/logger.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import { Wallet } from './wallet.ts';
import { PumpAmmClient } from './pumpAmm.ts';
import { Broadcaster, type BroadcastResult, type TxSender } from './broadcaster.ts';
import { RpcTxSender } from './sender.ts';
import { buildFeePlan } from './fees.ts';
import { assembleSignedSwapTx } from './assemble.ts';

/**
 * Execution orchestrator (Section 7.1). Builds a swap via the SDK, assembles a
 * signed transaction with fees, and hands it to the mode-gated broadcaster.
 * Only constructed in dry-run/live mode; paper mode never touches it.
 */
export class Executor {
  private readonly config: Config;
  private readonly rpc: RpcClient;
  private readonly connection: Connection;
  private readonly wallet: Wallet;
  private readonly pumpAmm: PumpAmmClient;
  private readonly broadcaster: Broadcaster;
  private readonly log = logger.child({ mod: 'executor' });

  constructor(deps: { config: Config; rpc: RpcClient; httpUrl: string }) {
    this.config = deps.config;
    this.rpc = deps.rpc;
    this.connection = new Connection(deps.httpUrl, 'confirmed');
    this.wallet = Wallet.load(deps.config.wallet.keypairEnvVar, deps.config.mode);
    this.pumpAmm = new PumpAmmClient(deps.httpUrl);

    const senders: TxSender[] = [new RpcTxSender('primary', deps.httpUrl)];
    if (deps.config.rpc?.secondaryHttp) senders.push(new RpcTxSender('secondary', deps.config.rpc.secondaryHttp));
    this.broadcaster = new Broadcaster(deps.config.mode, senders);

    this.log.info('executor ready', { mode: deps.config.mode, wallet: this.wallet.publicKey, ephemeral: this.wallet.ephemeral });
  }

  get publicKey(): string {
    return this.wallet.publicKey;
  }

  /** Build + broadcast a buy for `sizeSol` worth of the pool's base token. */
  async buy(poolAddress: string, baseMint: string, sizeSol: number): Promise<BroadcastResult> {
    const feePlan = await buildFeePlan(this.rpc, this.config);
    const quoteLamports = BigInt(Math.floor(sizeSol * LAMPORTS_PER_SOL));
    const ixs = await this.pumpAmm.buildBuy(
      poolAddress,
      this.wallet.keypair.publicKey,
      quoteLamports,
      this.config.entry.maxSlippagePct,
    );
    const bytes = await assembleSignedSwapTx(ixs, { connection: this.connection, wallet: this.wallet, feePlan });
    const result = await this.broadcaster.broadcast(bytes, `buy:${short(baseMint)}`);
    this.log.info('buy broadcast', { mint: baseMint, ...summarize(result) });
    return result;
  }

  /** Build + broadcast a sell of `baseAmount` raw base-token units. */
  async sell(poolAddress: string, baseMint: string, baseAmount: bigint, slippagePct: number): Promise<BroadcastResult> {
    const feePlan = await buildFeePlan(this.rpc, this.config);
    const ixs = await this.pumpAmm.buildSell(
      poolAddress,
      this.wallet.keypair.publicKey,
      baseAmount,
      slippagePct,
    );
    const bytes = await assembleSignedSwapTx(ixs, { connection: this.connection, wallet: this.wallet, feePlan });
    const result = await this.broadcaster.broadcast(bytes, `sell:${short(baseMint)}`);
    this.log.info('sell broadcast', { mint: baseMint, ...summarize(result) });
    return result;
  }
}

function summarize(r: BroadcastResult): Record<string, unknown> {
  return { simulated: r.simulated, sent: r.sent, ok: !r.simErr, ...(r.signature ? { signature: r.signature } : {}) };
}

function short(mint: string): string {
  return mint.length > 10 ? `${mint.slice(0, 4)}…${mint.slice(-4)}` : mint;
}
