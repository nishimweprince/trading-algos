import type { RpcClient } from '../core/rpc.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { Mint } from '../core/types.ts';
import { PricePoller, type PoolRef, type PriceTick } from '../positions/pricing.ts';
import { logger } from '../core/logger.ts';

/**
 * Counterfactual ("shadow") outcome tracker for candidates we did NOT trade —
 * vetoed candidates, or accepts that never entered. It samples the pool price
 * for a bounded window after graduation and records the peak the token reached,
 * so we can later ask "of tokens vetoed for reason X, what fraction would have
 * hit +25% / +50%?". This quantifies false-positive vetoes and is the evidence
 * that must precede any loosening of a guardrail threshold.
 *
 * It never trades capital and runs on its own PricePoller (separate from the
 * live position poller) at a slower cadence with a hard cap on how many pools it
 * watches at once, so it can never compete with live exit pricing.
 */
export interface ShadowTrackRequest {
  mint: Mint;
  verdict: 'veto' | 'accept_not_entered';
  primaryVetoCode: string | null;
  /** Price at graduation — the hypothetical entry price. Must be > 0. */
  baselinePrice: number;
  poolRef: PoolRef;
  sessionId?: number | null;
  configHash?: string | null;
}

interface ShadowState {
  req: ShadowTrackRequest;
  peak: number;
  trough: number;
  samples: number;
  startedMs: number;
}

export interface ShadowTrackerOptions {
  /** How long to track each mint (ms). Default 20 min. */
  windowMs?: number;
  /** Poll cadence (ms). Default 3 s — slower than the live 1 s poller. */
  pollMs?: number;
  /** Max pools tracked at once. Excess candidates are dropped (logged). */
  maxConcurrent?: number;
  now?: () => number;
}

export class ShadowTracker {
  private readonly repos: Repositories;
  private readonly poller: PricePoller;
  private readonly states = new Map<Mint, ShadowState>();
  private readonly windowMs: number;
  private readonly pollMs: number;
  private readonly maxConcurrent: number;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'shadow' });
  private sweepTimer: NodeJS.Timeout | null = null;
  private droppedAtCapacity = 0;

  constructor(rpc: RpcClient, repos: Repositories, opts: ShadowTrackerOptions = {}) {
    this.repos = repos;
    this.windowMs = opts.windowMs ?? 20 * 60_000;
    this.pollMs = opts.pollMs ?? 3_000;
    this.maxConcurrent = opts.maxConcurrent ?? 25;
    this.now = opts.now ?? (() => Date.now());
    this.poller = new PricePoller(rpc, this.pollMs, this.now);
    this.poller.setHandler((tick) => this.onTick(tick));
  }

  start(): void {
    this.poller.start();
    // A sweeper finishes windows even for pools that stopped producing ticks
    // (e.g. a rugged pool whose vaults read as empty), so states never leak.
    if (!this.sweepTimer) {
      this.sweepTimer = setInterval(() => this.sweep(), this.pollMs);
      this.sweepTimer.unref?.();
    }
  }

  stop(): void {
    this.poller.stop();
    if (this.sweepTimer) clearInterval(this.sweepTimer);
    this.sweepTimer = null;
    this.states.clear();
  }

  get size(): number {
    return this.states.size;
  }

  track(req: ShadowTrackRequest): void {
    if (!(req.baselinePrice > 0)) return; // can't compute MFE without a baseline
    if (this.states.has(req.mint)) return; // already tracking
    if (this.states.size >= this.maxConcurrent) {
      this.droppedAtCapacity++;
      // Log periodically so bounded coverage is visible, never silent.
      if (this.droppedAtCapacity % 25 === 1) {
        this.log.warn('shadow tracker at capacity — dropping candidate (coverage bounded)', {
          cap: this.maxConcurrent,
          droppedTotal: this.droppedAtCapacity,
          mint: req.mint,
        });
      }
      return;
    }
    this.states.set(req.mint, {
      req,
      peak: req.baselinePrice,
      trough: req.baselinePrice,
      samples: 0,
      startedMs: this.now(),
    });
    this.poller.register(req.poolRef);
  }

  private onTick(tick: PriceTick): void {
    const st = this.states.get(tick.mint);
    if (!st) return;
    if (tick.price > 0) {
      if (tick.price > st.peak) st.peak = tick.price;
      if (tick.price < st.trough) st.trough = tick.price;
      st.samples++;
    }
    if (this.now() - st.startedMs >= this.windowMs) this.finish(tick.mint);
  }

  private sweep(): void {
    const cutoff = this.now() - this.windowMs;
    for (const [mint, st] of this.states) {
      if (st.startedMs <= cutoff) this.finish(mint);
    }
  }

  private finish(mint: Mint): void {
    const st = this.states.get(mint);
    if (!st) return;
    this.states.delete(mint);
    this.poller.unregister(mint);
    const base = st.req.baselinePrice;
    const peakMfePct = (st.peak / base - 1) * 100;
    const maxMaePct = (st.trough / base - 1) * 100;
    try {
      this.repos.recordShadowOutcome({
        mint,
        verdict: st.req.verdict,
        primaryVetoCode: st.req.primaryVetoCode,
        baselinePrice: base,
        peakPrice: st.peak,
        troughPrice: st.trough,
        peakMfePct,
        maxMaePct,
        hit25: peakMfePct >= 25,
        hit50: peakMfePct >= 50,
        samples: st.samples,
        trackedMs: this.now() - st.startedMs,
        sessionId: st.req.sessionId ?? null,
        configHash: st.req.configHash ?? null,
      });
    } catch (err) {
      this.log.error('failed to persist shadow outcome', { mint, err });
    }
  }
}
