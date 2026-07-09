import { Connection } from '@solana/web3.js';
import type { Wallet } from '../executor/wallet.ts';
import type { PumpAmmClient } from '../executor/pumpAmm.ts';
import type { FeePlan } from '../executor/fees.ts';
import { assembleSignedSwapTx } from '../executor/assemble.ts';
import { logger } from '../core/logger.ts';

/**
 * Pre-signed exit ladder (Section 7.2). Immediately after a buy confirms, a
 * full-exit sell is built and signed in advance at several slippage tiers and
 * refreshed every ~45s with a fresh blockhash. On an exit trigger the hot path
 * is: pick the pre-signed tx for the situation and dispatch — zero build/sign
 * time. Escalation walks from the tightest tier to the loosest; an emergency
 * jumps straight to the worst-slippage tier (any exit beats no exit in a rug).
 *
 * Full-exit tiers cover stop-loss / trailing / time-stop / emergency (all sell
 * the whole remainder). The TP1 partial is not an emergency and is built fresh.
 */

export interface LadderTier {
  slippagePct: number;
  /** Signed, serialized transaction ready to broadcast. */
  bytes: Uint8Array;
}

export interface ExitLadderDeps {
  connection: Connection;
  wallet: Wallet;
  pumpAmm: PumpAmmClient;
  feePlanProvider: () => Promise<FeePlan>;
  jitoTipAccountProvider?: () => Promise<string | undefined>;
  poolAddress: string;
  baseMint: string;
  slippageTiers: number[];
  now?: () => number;
}

export class ExitLadder {
  private readonly deps: ExitLadderDeps;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'presign' });
  private tiers: LadderTier[] = [];
  private builtAtMs = 0;
  private builtForAmount = 0n;

  constructor(deps: ExitLadderDeps) {
    this.deps = deps;
    this.now = deps.now ?? (() => Date.now());
  }

  /** Rebuild + re-sign every tier for the current holding, with a fresh blockhash. */
  async refresh(baseAmount: bigint): Promise<void> {
    if (baseAmount <= 0n) {
      this.tiers = [];
      return;
    }
    const feePlan = await this.deps.feePlanProvider();
    const jitoTipAccount = feePlan.jitoTipLamports > 0
      ? await this.deps.jitoTipAccountProvider?.()
      : undefined;
    const tiers: LadderTier[] = [];
    for (const slippagePct of this.deps.slippageTiers) {
      const ixs = await this.deps.pumpAmm.buildSell(
        this.deps.poolAddress,
        this.deps.wallet.keypair.publicKey,
        baseAmount,
        slippagePct,
      );
      const bytes = await assembleSignedSwapTx(ixs, {
        connection: this.deps.connection,
        wallet: this.deps.wallet,
        feePlan,
        ...(jitoTipAccount ? { jitoTipAccount } : {}),
      });
      tiers.push({ slippagePct, bytes });
    }
    this.tiers = tiers.sort((a, b) => a.slippagePct - b.slippagePct);
    this.builtAtMs = this.now();
    this.builtForAmount = baseAmount;
    this.log.debug('exit ladder refreshed', {
      mint: this.deps.baseMint,
      tiers: this.tiers.map((t) => t.slippagePct),
    });
  }

  /** Tightest tier whose slippage tolerance is >= target; else the worst tier. */
  pick(targetSlippagePct: number): LadderTier | null {
    return this.tiers.find((t) => t.slippagePct >= targetSlippagePct) ?? this.worst();
  }

  /** Worst-slippage tier — used for emergency exits. */
  worst(): LadderTier | null {
    return this.tiers.length > 0 ? this.tiers[this.tiers.length - 1]! : null;
  }

  /** Next tier looser than the given slippage — for escalation on non-inclusion. */
  next(afterSlippagePct: number): LadderTier | null {
    return this.tiers.find((t) => t.slippagePct > afterSlippagePct) ?? null;
  }

  isStale(maxAgeMs: number): boolean {
    return this.tiers.length === 0 || this.now() - this.builtAtMs > maxAgeMs;
  }

  get size(): number {
    return this.tiers.length;
  }

  get amount(): bigint {
    return this.builtForAmount;
  }
}
