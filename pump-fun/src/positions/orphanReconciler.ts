import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { PoolPricingRef } from '../core/types.ts';
import { LAMPORTS_PER_SOL, PROGRAM_IDS, WSOL_MINT } from '../core/constants.ts';
import { logger } from '../core/logger.ts';
import type { Executor } from '../executor/index.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { ActualsRecorder } from './actuals.ts';

/**
 * Wallet orphan reconciler.
 *
 * The position manager only sells what it tracks. On 2026-10-03 buys that
 * landed after their confirm window were recorded FAILED, so their tokens sat
 * in the wallet with nothing selling them while new buys kept spending SOL.
 * This is the backstop for that whole class of bug: at boot and every
 * execution.orphanSweepSec, any non-zero token balance that no live component
 * is tracking is sold back to SOL and booked as an ORPHAN_RECOVERY close.
 *
 * Live only. Never touches WSOL, execution.orphanIgnoreMints, mints the
 * manager is tracking or still entering, or mints the curve lane holds.
 */

export type OrphanExecutor = Pick<
  Executor,
  'listTokenAccounts' | 'canonicalPoolFor' | 'estimateSellLamports' | 'sellAndConfirm' | 'readTokenBalance' | 'solBalanceLamports'
>;

export interface OrphanSweepResult {
  found: number;
  sold: string[];
  dust: string[];
  stuck: string[];
}

/** How long a stuck mint waits before the reconciler tries it again. */
const STUCK_RETRY_MS = 5 * 60_000;

export class OrphanReconciler {
  private readonly config: Config;
  private readonly bus: TypedBus;
  private readonly repos: Repositories;
  private readonly executor: OrphanExecutor;
  private readonly trackedMints: () => Iterable<string>;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'orphans' });
  private readonly actuals: ActualsRecorder | undefined;
  private readonly dustAlerted = new Set<string>();
  /** Mints with no PumpSwap pool (never traded by this bot, e.g. airdropped spam): skipped for good. */
  private readonly unsellable = new Set<string>();
  private readonly stuckUntil = new Map<string, number>();
  private running = false;
  private timer: NodeJS.Timeout | null = null;

  constructor(deps: {
    config: Config;
    bus: TypedBus;
    repos: Repositories;
    executor: OrphanExecutor;
    /** Every mint a live component owns right now (open, exiting, entering). */
    trackedMints: () => Iterable<string>;
    /** Books wallet-true proceeds onto the recovery row (Phase 2). */
    actuals?: ActualsRecorder;
    now?: () => number;
  }) {
    this.actuals = deps.actuals;
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.executor = deps.executor;
    this.trackedMints = deps.trackedMints;
    this.now = deps.now ?? (() => Date.now());
  }

  /** Boot pass, then the timer. */
  async start(): Promise<void> {
    if (this.config.mode !== 'live') return;
    await this.sweep();
    const sec = this.config.execution.orphanSweepSec;
    if (sec > 0) {
      this.timer = setInterval(() => void this.sweep(), sec * 1000);
      this.timer.unref?.();
    }
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  async sweep(): Promise<OrphanSweepResult> {
    const result: OrphanSweepResult = { found: 0, sold: [], dust: [], stuck: [] };
    if (this.running) return result;
    this.running = true;
    try {
      const accounts = await this.executor.listTokenAccounts();
      const tracked = new Set<string>(this.trackedMints());
      for (const m of this.safeCurveMints()) tracked.add(m);
      const ignore = new Set<string>([WSOL_MINT, ...this.config.execution.orphanIgnoreMints]);
      const orphans = accounts.filter((a) => a.amount > 0n && !tracked.has(a.mint) && !ignore.has(a.mint) && !this.unsellable.has(a.mint));
      result.found = orphans.length;
      if (orphans.length) this.log.warn('wallet holds untracked tokens', { count: orphans.length, mints: orphans.map((o) => o.mint) });
      for (const o of orphans) {
        // Re-check: the manager may have adopted it while we were working.
        if (new Set(this.trackedMints()).has(o.mint)) continue;
        if ((this.stuckUntil.get(o.mint) ?? 0) > this.now()) continue;
        const outcome = await this.sellOrphan(o.mint, o.amount, o.programId === PROGRAM_IDS.TOKEN_2022);
        result[outcome].push(o.mint);
      }
    } catch (err) {
      this.log.warn('orphan sweep failed', { err });
    } finally {
      this.running = false;
    }
    return result;
  }

  private safeCurveMints(): string[] {
    try {
      return this.repos.activeCurveMints();
    } catch {
      return [];
    }
  }

  private async sellOrphan(mint: string, amount: bigint, isToken2022: boolean): Promise<'sold' | 'dust' | 'stuck'> {
    const row = this.safeLatestRow(mint);
    const pricing = parsePricing(row?.pricingJson);
    const poolAddress = pricing?.poolAddress ?? this.executor.canonicalPoolFor(mint);

    let estimate: bigint | null = null;
    try {
      estimate = await this.executor.estimateSellLamports(poolAddress, amount);
    } catch (err) {
      // No readable pool: nothing this lane can sell into. Seen in production
      // for mints this bot never traded (no positions/graduations rows) —
      // airdropped spam or pre-graduation coins. Warn once, never halt.
      if (!row) {
        this.unsellable.add(mint);
        this.bus.emit('alert', { level: 'warn', message: `ignoring ${short(mint)} — not a PumpSwap token this bot traded (no pool ${short(poolAddress)})`, telegram: true });
        this.log.warn('untracked token has no PumpSwap pool and no trade history — ignoring', { mint, poolAddress, err: (err as Error).message });
        return 'dust';
      }
      this.markStuck(mint, `no readable PumpSwap pool ${poolAddress}: ${(err as Error).message}`, false);
      return 'stuck';
    }
    const minLamports = BigInt(Math.floor(this.config.execution.orphanMinProceedsSol * LAMPORTS_PER_SOL));
    if (estimate < minLamports) {
      if (!this.dustAlerted.has(mint)) {
        this.dustAlerted.add(mint);
        this.bus.emit('alert', { level: 'warn', message: `orphan ${short(mint)} left as dust (~${lamportsToSol(estimate).toFixed(6)} SOL)`, telegram: true });
        this.log.warn('orphan below dust floor — leaving it', { mint, estimateLamports: estimate.toString() });
      }
      return 'dust';
    }

    this.bus.emit('alert', { level: 'warn', message: `🧹 orphan ${short(mint)} found (${amount.toString()} raw, ~${lamportsToSol(estimate).toFixed(4)} SOL) — selling`, telegram: true });
    const startedAtMs = this.now();
    const before = await this.safeSolBalance();
    const tiers = [...this.config.exits.ladderSlippageTiers, this.config.exits.emergencySlippagePct];
    const maxAttempts = Math.max(this.config.exits.maxExitAttempts, 1);
    let remaining = amount;
    let lastErr = '';
    const signatures: string[] = [];
    for (let attempt = 0; attempt < maxAttempts && remaining > 0n; attempt++) {
      const slippagePct = tiers[Math.min(attempt, tiers.length - 1)]!;
      try {
        const r = await this.executor.sellAndConfirm(poolAddress, mint, remaining, slippagePct);
        if (r.signature) signatures.push(r.signature);
        if (!r.confirmed) lastErr = String(r.sendErr ?? r.simErr ?? 'not confirmed');
      } catch (err) {
        lastErr = (err as Error).message;
      }
      try {
        remaining = await this.executor.readTokenBalance(mint, isToken2022);
      } catch (err) {
        lastErr = `balance read failed: ${(err as Error).message}`;
      }
    }

    if (remaining > 0n) {
      this.markStuck(mint, lastErr || 'sell attempts exhausted', true);
      return 'stuck';
    }

    const after = await this.safeSolBalance();
    const proceedsSol = before !== null && after !== null ? (after - before) / LAMPORTS_PER_SOL : lamportsToSol(estimate);
    const costUnbooked = this.book(mint, row, pricing, poolAddress, amount, proceedsSol, signatures, startedAtMs);
    // Replace the balance-delta estimate with the chain's own numbers.
    if (this.actuals) {
      const entryTx = costUnbooked ? row?.entryTx ?? null : null;
      void this.actuals.bookTrade({
        mint,
        baseIsToken2022: isToken2022,
        entryTx,
        exitTxs: signatures,
        entryOptional: !costUnbooked || !entryTx,
      });
    }
    this.stuckUntil.delete(mint);
    this.bus.emit('alert', { level: 'warn', message: `✅ orphan ${short(mint)} sold for ${proceedsSol.toFixed(4)} SOL`, telegram: true });
    this.log.warn('orphan sold', { mint, proceedsSol, signatures });
    return 'sold';
  }

  /**
   * One CLOSED row per recovered orphan. If the mint's last row never booked
   * its cost (FAILED / PENDING_ENTRY: the buy was written off as not landed),
   * the cost is booked here; otherwise the sale is pure recovered proceeds.
   */
  private book(
    mint: string,
    row: ReturnType<Repositories['latestPositionForMint']>,
    pricing: PoolPricingRef | null,
    poolAddress: string,
    amount: bigint,
    proceedsSol: number,
    signatures: string[],
    startedAtMs: number,
  ): boolean {
    const costUnbooked = row !== null && (row.state === 'FAILED' || row.state === 'PENDING_ENTRY');
    const costSol = costUnbooked ? row.sizeSol : 0;
    const pnlSol = proceedsSol - costSol;
    try {
      this.repos.upsertPosition(
        {
          mint,
          state: 'CLOSED',
          sizeSol: costSol,
          entryPrice: row?.entryPrice ?? 0,
          openedAt: row?.openedAt ? Date.parse(row.openedAt) : startedAtMs,
          closedAt: this.now(),
          exitTrigger: 'ORPHAN_RECOVERY',
          pnlSol,
          pnlPct: costSol > 0 ? (pnlSol / costSol) * 100 : 0,
        },
        {
          entryTx: row?.entryTx ?? null,
          exitTx: signatures.at(-1) ?? null,
          rawBaseAmount: 0n,
          pricingJson: JSON.stringify(pricing ?? { mint, poolAddress }),
          executionJson: JSON.stringify({ event: 'orphan_recovery', soldRaw: amount.toString(), proceedsSol, signatures, previousState: row?.state ?? null }),
          grossPnlSol: pnlSol,
          feesSol: 0,
          netPnlSol: pnlSol,
          mode: this.config.mode,
          simulated: false,
        },
      );
    } catch (err) {
      this.log.error('failed to persist orphan recovery', { mint, err });
    }
    return costUnbooked;
  }

  private markStuck(mint: string, reason: string, halt: boolean): void {
    const first = !this.stuckUntil.has(mint);
    this.stuckUntil.set(mint, this.now() + STUCK_RETRY_MS);
    this.log.error('orphan could not be sold', { mint, reason });
    if (!first) return;
    this.bus.emit('alert', { level: 'error', message: `CRITICAL orphan ${short(mint)} unsold — ${reason}`, telegram: true });
    // Untracked tokens we cannot sell: stop buying until a human looks.
    if (halt) this.bus.emit('killSwitch', { source: 'internal', detail: `orphan ${mint} unsold: ${reason}` });
  }

  private safeLatestRow(mint: string): ReturnType<Repositories['latestPositionForMint']> {
    try {
      return this.repos.latestPositionForMint(mint);
    } catch {
      return null;
    }
  }

  private async safeSolBalance(): Promise<number | null> {
    try {
      return await this.executor.solBalanceLamports();
    } catch {
      return null;
    }
  }
}

function parsePricing(json: string | null | undefined): PoolPricingRef | null {
  if (!json) return null;
  try {
    const p = JSON.parse(json) as Partial<PoolPricingRef>;
    return typeof p.poolAddress === 'string' && typeof p.baseMint === 'string' ? (p as PoolPricingRef) : null;
  } catch {
    return null;
  }
}

function lamportsToSol(l: bigint): number {
  return Number(l) / LAMPORTS_PER_SOL;
}

function short(mint: string): string {
  return mint.length > 10 ? `${mint.slice(0, 4)}…${mint.slice(-4)}` : mint;
}
