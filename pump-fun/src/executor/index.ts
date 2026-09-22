import { Connection, VersionedTransaction } from '@solana/web3.js';
import type { Config } from '../config/schema.ts';
import type { RpcClient } from '../core/rpc.ts';
import type { SlotClock } from '../core/slotClock.ts';
import { logger } from '../core/logger.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import { Wallet } from './wallet.ts';
import { PumpAmmClient } from './pumpAmm.ts';
import { Broadcaster, type BroadcastResult, type TxSender } from './broadcaster.ts';
import { RpcTxSender } from './sender.ts';
import { buildFeePlan, type FeePlan } from './fees.ts';
import { assembleSignedSwapTx } from './assemble.ts';
import { BlockhashCache, isBlockhashNotFound } from './blockhashCache.ts';
import { createFailoverFetch } from '../core/rpc.ts';
import { JitoTxSender } from './jito.ts';
import { readSecret } from '../config/load.ts';
import { deriveAta } from '../core/ata.ts';
import { sweepEmptyTokenAccounts, type SweepResult } from './ataSweeper.ts';
import { ExitLadder } from '../positions/presign.ts';
import { buySlippageAttempts, withSlippageRetry, entryMovePct, EntryMoveExceeded, type ReserveSnapshot } from './slippage.ts';

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
  private readonly jito: JitoTxSender | undefined;
  private readonly blockhashes: BlockhashCache | undefined;
  /** Cached fee plan; see feePlan() for why staleness here is safe. */
  private feePlanCache: { atMs: number; plan: FeePlan } | null = null;
  private readonly log = logger.child({ mod: 'executor' });

  constructor(deps: { config: Config; rpc: RpcClient; httpUrl: string; slotClock?: SlotClock | undefined }) {
    this.config = deps.config;
    this.rpc = deps.rpc;
    const exec = deps.config.execution;
    // Blockhash / confirmation reads stay at 'confirmed'; pool STATE and the
    // pre-send simulate use execution.stateCommitment so a pool the enricher
    // just saw at 'processed' is also visible to the SDK and the simulator.
    //
    // web3.js has NO default fetch timeout, so a bare Connection can hang an
    // entry indefinitely on a stalled getLatestBlockhash. PumpAmmClient already
    // solves this with createFailoverFetch; reuse it verbatim.
    const fetchUrls = [deps.httpUrl, ...(deps.config.rpc?.fallbackHttp ?? []).filter((u) => u && u !== deps.httpUrl)];
    this.connection = new Connection(deps.httpUrl, {
      commitment: 'confirmed',
      fetch: createFailoverFetch(fetchUrls, { timeoutMs: deps.config.rpc?.readTimeoutMs ?? 900 }),
    });
    this.blockhashes =
      exec.blockhashCacheMs > 0 ? new BlockhashCache(this.connection, exec.blockhashCacheMs) : undefined;
    this.wallet = Wallet.load(deps.config.wallet.keypairEnvVar, deps.config.mode);
    this.pumpAmm = new PumpAmmClient(deps.httpUrl, exec.stateCommitment);

    const senderOpts = { commitment: exec.stateCommitment, simulateTimeoutMs: exec.simulateTimeoutMs };
    const primary = new RpcTxSender('primary', deps.httpUrl, senderOpts);
    const jitoAuthToken = deps.config.jito?.authTokenEnvVar ? readSecret(deps.config.jito.authTokenEnvVar) : undefined;
    this.jito = deps.config.jito
      ? new JitoTxSender({
          blockEngineUrl: deps.config.jito.blockEngineUrl,
          ...(jitoAuthToken ? { authToken: jitoAuthToken } : {}),
        })
      : undefined;

    const senders: TxSender[] = [];
    if (this.jito) senders.push(this.jito);
    senders.push(primary);
    if (deps.config.rpc?.secondaryHttp) {
      senders.push(new RpcTxSender('secondary', deps.config.rpc.secondaryHttp, senderOpts));
    }
    this.broadcaster = new Broadcaster(deps.config.mode, senders, {
      simulator: primary,
      confirmSignature: (signature) => this.confirmSignature(signature),
      slotClock: deps.slotClock,
    });

    this.log.info('executor ready', { mode: deps.config.mode, wallet: this.wallet.publicKey, ephemeral: this.wallet.ephemeral });
  }

  get publicKey(): string {
    return this.wallet.publicKey;
  }

  /**
   * Fee plan, cached for `fees.planCacheMs`.
   *
   * Safe to serve stale: the result is not pool-specific, it is clamped between
   * fees.priorityFloorMicroLamports and priorityCapMicroLamports, and every
   * failure path inside buildFeePlan already degrades to the floor — the code
   * there states outright that fee telemetry must never block a trade. The cost
   * of a few seconds of staleness is a marginally mis-bid priority fee; the cost
   * of the round trip was a serial RPC hop inside the sniper window on every buy,
   * every sell, and twice per exit-ladder build.
   */
  private async feePlan(): Promise<FeePlan> {
    const ttl = this.config.fees.planCacheMs;
    const nowMs = Date.now();
    if (ttl > 0 && this.feePlanCache && nowMs - this.feePlanCache.atMs < ttl) {
      return this.feePlanCache.plan;
    }
    const plan = await buildFeePlan(this.rpc, this.config);
    this.feePlanCache = { atMs: nowMs, plan };
    return plan;
  }

  /** Assemble deps shared by every signing path, so nobody re-fetches a blockhash. */
  private assembleExtras(): { blockhashProvider?: () => Promise<string> } {
    return this.blockhashes ? { blockhashProvider: () => this.blockhashes!.get() } : {};
  }

  /** Invalidate the cached blockhash after a send rejected the one we signed with. */
  invalidateBlockhash(): void {
    this.blockhashes?.invalidate();
  }

  /**
   * Assemble + broadcast, retrying once on a BlockhashNotFound send failure.
   *
   * The pre-send simulate passes `replaceRecentBlockhash: true`, so a blockhash
   * that has aged out is completely invisible to simulation and surfaces only at
   * sendRawTransaction. `build` is re-run so the retry signs over a fresh
   * blockhash. This costs nothing when it fires: the transaction never landed,
   * so no fee was paid.
   */
  private async broadcastSigned(
    build: () => Promise<Uint8Array>,
    label: string,
    opts?: { skipSimulation?: boolean; confirmTimeoutMs?: number; confirmPollMs?: number },
  ): Promise<BroadcastResult> {
    try {
      return await this.broadcaster.broadcast(await build(), label, opts);
    } catch (err) {
      if (!this.blockhashes || !isBlockhashNotFound(err)) throw err;
      this.log.warn('send rejected the signed blockhash — refreshing and retrying once', { label });
      this.blockhashes.invalidate();
      return this.broadcaster.broadcast(await build(), label, opts);
    }
  }

  /** Confirmation budget for buys — see execution.buyConfirmTimeoutMs. */
  private buyConfirmOpts(): { confirmTimeoutMs: number; confirmPollMs: number } {
    return {
      confirmTimeoutMs: this.config.execution.buyConfirmTimeoutMs,
      confirmPollMs: this.config.execution.buyConfirmPollMs,
    };
  }

  get keypairPublicKey() {
    return this.wallet.keypair.publicKey;
  }

  /** Build + broadcast a buy for `sizeSol` worth of the pool's base token. */
  async buy(poolAddress: string, baseMint: string, sizeSol: number, reference?: ReserveSnapshot): Promise<BroadcastResult> {
    return this.buyAndConfirm(poolAddress, baseMint, sizeSol, reference);
  }

  /**
   * `reference` is the pool snapshot the verdict was made on. The buy is
   * quoted against a fresh state read; the mid move between the two is
   * attached to the result (`entryMovePct`) and, when entry.maxEntryMovePct is
   * set, a move above it throws EntryMoveExceeded before anything is signed.
   */
  async buyAndConfirm(
    poolAddress: string,
    baseMint: string,
    sizeSol: number,
    reference?: ReserveSnapshot,
  ): Promise<BroadcastResult> {
    const feePlan = await this.feePlan();
    const quoteLamports = BigInt(Math.floor(sizeSol * LAMPORTS_PER_SOL));
    const jitoTip = await this.jitoTipAccount(feePlan.jitoTipLamports);
    // Entry retries use their own (tight) tiers — never the exit ladder's 25%.
    const attempts = buySlippageAttempts(this.config.entry.maxSlippagePct, this.config.entry.buyRetrySlippageTiers);
    const moveCap = this.config.entry.maxEntryMovePct;

    if (this.config.execution.parallelBuySimulate && attempts.length > 1) {
      return this.buyParallelSimulate({ poolAddress, baseMint, quoteLamports, feePlan, jitoTip, attempts, moveCap, reference });
    }

    return withSlippageRetry(attempts, async (slippagePct) => {
      const quoted = await this.pumpAmm.buildBuyQuoted(
        poolAddress,
        this.wallet.keypair.publicKey,
        quoteLamports,
        slippagePct,
      );
      const movePct = reference ? entryMovePct(reference, quoted) : undefined;
      if (movePct !== undefined && moveCap !== undefined && movePct > moveCap) {
        this.log.warn('entry move gate — skipping buy', { mint: baseMint, movePct, moveCap, slippagePct });
        throw new EntryMoveExceeded(movePct, moveCap);
      }
      const build = () =>
        assembleSignedSwapTx(quoted.ixs, {
          connection: this.connection,
          wallet: this.wallet,
          feePlan,
          ...jitoTip,
          ...this.assembleExtras(),
        });
      const result = await this.broadcastSigned(build, `buy:${short(baseMint)}`, this.buyConfirmOpts());
      if (movePct !== undefined) result.entryMovePct = movePct;
      this.log.info('buy broadcast', { mint: baseMint, slippagePct, entryMovePct: movePct, ...summarize(result) });
      return result;
    }, {
      onRetry: (nextPct, prev) => {
        this.log.warn('buy exceeded slippage — rebuilding with looser bound', {
          mint: baseMint,
          nextPct,
          simErr: prev.simErr,
        });
      },
    });
  }

  /**
   * Parallel speculative simulate (execution.parallelBuySimulate).
   *
   * Serially, discovering that the tight tier fails slippage costs a whole extra
   * round: a fresh state read, a fresh assemble and a fresh simulate, all inside
   * the window where the price is moving. Here every tier is quoted, assembled
   * and simulated CONCURRENTLY, and only the tightest one that passed is sent.
   *
   * Why not send before simulating, which would save the whole simulate hop:
   * each tier is an independently valid transaction and sends use
   * `skipPreflight: true`, so two of them landing means buying 2x the intended
   * size. Making them mutually exclusive needs a durable nonce account. Not
   * worth that for one round trip.
   */
  private async buyParallelSimulate(args: {
    poolAddress: string;
    baseMint: string;
    quoteLamports: bigint;
    feePlan: FeePlan;
    jitoTip: { jitoTipAccount?: string };
    attempts: number[];
    moveCap: number | undefined;
    reference?: ReserveSnapshot | undefined;
  }): Promise<BroadcastResult> {
    const { poolAddress, baseMint, quoteLamports, feePlan, jitoTip, attempts, moveCap, reference } = args;

    type Candidate = {
      slippagePct: number;
      bytes: Uint8Array;
      /** Kept so a BlockhashNotFound retry can genuinely re-sign, not resend stale bytes. */
      build: () => Promise<Uint8Array>;
      movePct: number | undefined;
      simErr: unknown;
      logs: string[];
    };

    const settled = await Promise.allSettled(
      attempts.map(async (slippagePct): Promise<Candidate> => {
        const quoted = await this.pumpAmm.buildBuyQuoted(
          poolAddress,
          this.wallet.keypair.publicKey,
          quoteLamports,
          slippagePct,
        );
        const movePct = reference ? entryMovePct(reference, quoted) : undefined;
        if (movePct !== undefined && moveCap !== undefined && movePct > moveCap) {
          this.log.warn('entry move gate — skipping buy', { mint: baseMint, movePct, moveCap, slippagePct });
          throw new EntryMoveExceeded(movePct, moveCap);
        }
        const build = () =>
          assembleSignedSwapTx(quoted.ixs, {
            connection: this.connection,
            wallet: this.wallet,
            feePlan,
            ...jitoTip,
            ...this.assembleExtras(),
          });
        const bytes = await build();
        const sim = await this.broadcaster.simulateOnly(bytes);
        return { slippagePct, bytes, build, movePct, simErr: sim.err, logs: sim.logs };
      }),
    );

    // The entry move gate is a hard veto on the whole entry, not a per-tier
    // condition: it is computed from the same reference for every tier, so if it
    // fired at all it fired everywhere. Surface it to the caller unchanged.
    const moveExceeded = settled.find(
      (s): s is PromiseRejectedResult => s.status === 'rejected' && s.reason instanceof EntryMoveExceeded,
    );
    if (moveExceeded) throw moveExceeded.reason;

    const candidates = settled
      .filter((s): s is PromiseFulfilledResult<Candidate> => s.status === 'fulfilled')
      .map((s) => s.value)
      .sort((a, b) => a.slippagePct - b.slippagePct);

    if (candidates.length === 0) {
      const firstRejection = settled.find((s): s is PromiseRejectedResult => s.status === 'rejected');
      throw firstRejection?.reason ?? new Error('buy: every slippage tier failed to build');
    }

    // Tightest passing tier wins — same preference order as the serial ladder.
    const chosen = candidates.find((c) => !c.simErr);
    if (!chosen) {
      // Nothing simulated clean. Report the tightest tier's failure so the
      // caller still sees a 6004 in simErr, exactly as the serial path did.
      const tightest = candidates[0]!;
      this.log.warn('buy: no slippage tier simulated clean', {
        mint: baseMint,
        tiers: candidates.map((c) => c.slippagePct),
        simErr: tightest.simErr,
      });
      return {
        mode: this.config.mode,
        simulated: true,
        sent: false,
        confirmed: false,
        simErr: tightest.simErr,
        logs: tightest.logs,
        attempts: [],
        ...(tightest.movePct !== undefined ? { entryMovePct: tightest.movePct } : {}),
      };
    }

    if (chosen.slippagePct !== candidates[0]!.slippagePct) {
      this.log.warn('buy: tight tier failed slippage — sending the next tier that simulated clean', {
        mint: baseMint,
        skipped: candidates[0]!.slippagePct,
        chosen: chosen.slippagePct,
      });
    }

    // Already simulated these exact bytes above, so skip the redundant hop. The
    // first call reuses them; a BlockhashNotFound retry re-assembles via build().
    let first = true;
    const result = await this.broadcastSigned(
      async () => {
        if (first) {
          first = false;
          return chosen.bytes;
        }
        return chosen.build();
      },
      `buy:${short(baseMint)}`,
      { skipSimulation: true, ...this.buyConfirmOpts() },
    );
    if (chosen.movePct !== undefined) result.entryMovePct = chosen.movePct;
    this.log.info('buy broadcast', {
      mint: baseMint,
      slippagePct: chosen.slippagePct,
      entryMovePct: chosen.movePct,
      parallelTiers: candidates.length,
      ...summarize(result),
    });
    return result;
  }

  /** Build + broadcast a sell of `baseAmount` raw base-token units. */
  async sell(poolAddress: string, baseMint: string, baseAmount: bigint, slippagePct: number): Promise<BroadcastResult> {
    return this.sellAndConfirm(poolAddress, baseMint, baseAmount, slippagePct);
  }

  async sellAndConfirm(poolAddress: string, baseMint: string, baseAmount: bigint, slippagePct: number): Promise<BroadcastResult> {
    const feePlan = await this.feePlan();
    const ixs = await this.pumpAmm.buildSell(
      poolAddress,
      this.wallet.keypair.publicKey,
      baseAmount,
      slippagePct,
    );
    const jitoTip = await this.jitoTipAccount(feePlan.jitoTipLamports);
    const build = () =>
      assembleSignedSwapTx(ixs, {
        connection: this.connection,
        wallet: this.wallet,
        feePlan,
        ...jitoTip,
        ...this.assembleExtras(),
      });
    const result = await this.broadcastSigned(build, `sell:${short(baseMint)}`);
    this.log.info('sell broadcast', { mint: baseMint, ...summarize(result) });
    return result;
  }

  buildExitLadder(poolAddress: string, baseMint: string): ExitLadder {
    return new ExitLadder({
      connection: this.connection,
      wallet: this.wallet,
      pumpAmm: this.pumpAmm,
      poolAddress,
      baseMint,
      slippageTiers: this.config.exits.ladderSlippageTiers,
      emergencySlippagePct: this.config.exits.emergencySlippagePct,
      feePlanProvider: () => this.feePlan(),
      ...this.assembleExtras(),
      jitoTipAccountProvider: async () => {
        const feePlan = await this.feePlan();
        return feePlan.jitoTipLamports > 0 ? this.jito?.getTipAccount(this.config.jito?.tipRefreshMs) : undefined;
      },
    });
  }

  /**
   * Curve-lane send (trade-local transactions arrive fully built but
   * unsigned). Signed here with the trading keypair, then broadcast through
   * the standard simulate-first gate — a failed sim refuses the send, exactly
   * like SDK-built buys. Never skips simulation.
   */
  async signAndBroadcastCurveTrade(unsignedTxBytes: Uint8Array, label: string): Promise<BroadcastResult> {
    const vtx = VersionedTransaction.deserialize(unsignedTxBytes);
    vtx.sign([this.wallet.keypair]);
    const result = await this.broadcaster.broadcast(Buffer.from(vtx.serialize()), label);
    this.log.info('curve broadcast', { label, ...summarize(result) });
    return result;
  }

  async broadcastSignedExit(bytes: Uint8Array, baseMint: string): Promise<BroadcastResult> {
    // Pre-signed ladder tx: was validated at build time, so optionally skip the
    // pre-send simulate to shave an RPC round-trip off the exit hot path. Exits
    // use a short confirm window so a non-landing attempt escalates fast.
    return this.broadcaster.broadcast(bytes, `exit:${short(baseMint)}`, {
      skipSimulation: this.config.exits.skipSimulateOnPresignedExit,
      confirmTimeoutMs: this.config.exits.exitConfirmTimeoutMs,
      confirmPollMs: this.config.exits.exitConfirmPollMs,
    });
  }

  /** Close empty token accounts and reclaim their rent (see ataSweeper.ts). */
  async sweepEmptyAtas(opts: { dryRun?: boolean } = {}): Promise<SweepResult> {
    return sweepEmptyTokenAccounts(this.connection, this.wallet, {
      ...opts,
      priorityMicroLamports: this.config.fees.priorityFloorMicroLamports,
    });
  }

  /**
   * Post-buy token balance. A single read straight after confirmation can race
   * the ledger (a 'confirmed' read served before the slot propagates), so read
   * at the state commitment and retry a few times before reporting zero.
   */
  async reconcileTokenBalance(baseMint: string, baseIsToken2022 = false): Promise<bigint> {
    const ata = deriveAta(this.wallet.publicKey, baseMint, baseIsToken2022);
    const { reconcileAttempts, reconcileDelayMs, stateCommitment } = this.config.execution;
    for (let attempt = 1; attempt <= reconcileAttempts; attempt++) {
      const balance = await this.rpc.getTokenAccountBalance(ata, stateCommitment);
      const amount = balance?.amount ?? 0n;
      if (amount > 0n) return amount;
      if (attempt < reconcileAttempts) await delay(reconcileDelayMs);
    }
    return 0n;
  }

  private async jitoTipAccount(jitoTipLamports: number): Promise<{ jitoTipAccount?: string }> {
    if (jitoTipLamports <= 0 || !this.jito) return {};
    try {
      const account = await this.jito.getTipAccount(this.config.jito?.tipRefreshMs);
      return account ? { jitoTipAccount: account } : {};
    } catch (err) {
      this.log.warn('Jito tip account unavailable — signing without tip', { err });
      return {};
    }
  }

  private async confirmSignature(signature: string) {
    const [status] = await this.rpc.getSignatureStatuses([signature]);
    if (!status) return null;
    return { confirmationStatus: status.confirmationStatus, slot: status.slot, err: status.err };
  }
}

function summarize(r: BroadcastResult): Record<string, unknown> {
  return {
    simulated: r.simulated,
    sent: r.sent,
    confirmed: r.confirmed,
    ok: !r.simErr && !r.sendErr && r.confirmed,
    route: r.route,
    ...(r.signature ? { signature: r.signature } : {}),
  };
}

function short(mint: string): string {
  return mint.length > 10 ? `${mint.slice(0, 4)}…${mint.slice(-4)}` : mint;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
