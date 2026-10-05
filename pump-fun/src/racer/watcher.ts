/**
 * Near-complete curve watcher. Tails launches, polls each tracked mint's
 * bonding curve, and emits first-fire-only events:
 *
 *   would-fire — curve still open and completion cost <= ceiling (with
 *                buy-leg build timing when a builder is wired)
 *   migrated   — complete flag flipped (lead slots vs the would-fire, when any)
 *   expired    — tracked past the window with no migration
 *
 * Measures race feasibility (lead time, build latency) with zero sends. The
 * migrate-bundle leg is intentionally absent (verdict: no maintained builder).
 */
import type { CurveReserves } from '../enrichment/curve.ts';
import { evaluateFire } from './economics.ts';

export type RacerEventKind = 'would-fire' | 'migrated' | 'complete-no-pool' | 'expired';

export interface RacerEvent {
  mint: string;
  kind: RacerEventKind;
  /** Chain slot when observed (approx: one getSlot per tick). */
  slot: number | null;
  costSol: number | null;
  realTokenReserves: string | null;
  /** Slots between would-fire and migration (migrated only, when fired). */
  leadSlots: number | null;
  buyLegMs: number | null;
  buyLegOk: boolean | null;
}

/** Builds the completion-buy leg and reports shape validity + latency. No signing, no send. */
export type BuyLegBuilder = (mint: string, costSol: number) => Promise<{ ok: boolean; ms: number }>;

export interface WatcherDeps {
  fetchCurve: (mint: string) => Promise<CurveReserves | null>;
  getSlot: () => Promise<number | null>;
  sink: (e: RacerEvent) => void;
  now?: () => number;
  buildBuyLeg?: BuyLegBuilder;
  /**
   * Pool readability check on complete flips. Curves can complete without a
   * pool ever existing (observed live: Token-2022 curves with complete=1,
   * drained reserves, no migrate, no pool) — those emit `complete-no-pool`
   * instead of `migrated` so paper tracks only start on real migrations.
   * Absent = unchecked, complete always emits `migrated` (legacy).
   */
  readPool?: (mint: string) => Promise<boolean>;
}

export interface WatcherOptions {
  maxCostSol: number;
  maxTracked?: number;
  trackWindowMs?: number;
  maxConcurrent?: number;
}

interface Tracked {
  firstSeenMs: number;
  fired: boolean;
  fireSlot: number | null;
}

export class RacerWatcher {
  private readonly deps: WatcherDeps;
  private readonly opts: Required<WatcherOptions>;
  private readonly tracked = new Map<string, Tracked>();
  private readonly now: () => number;

  constructor(deps: WatcherDeps, opts: WatcherOptions) {
    this.deps = deps;
    this.opts = {
      maxCostSol: opts.maxCostSol,
      maxTracked: opts.maxTracked ?? 200,
      trackWindowMs: opts.trackWindowMs ?? 120 * 60_000,
      maxConcurrent: opts.maxConcurrent ?? 4,
    };
    this.now = deps.now ?? (() => Date.now());
  }

  get size(): number {
    return this.tracked.size;
  }

  /** Track a newly launched mint. Drops the oldest when over capacity (no event — never fired). */
  addLaunch(mint: string): void {
    if (this.tracked.has(mint)) return;
    if (this.tracked.size >= this.opts.maxTracked) {
      const oldest = this.tracked.keys().next();
      if (!oldest.done) this.tracked.delete(oldest.value);
    }
    this.tracked.set(mint, { firstSeenMs: this.now(), fired: false, fireSlot: null });
  }

  /** One poll cycle over every tracked mint, bounded concurrency. */
  async tick(): Promise<{ polled: number; events: number }> {
    const slot = await this.deps.getSlot().catch(() => null);
    const mints = [...this.tracked.keys()];
    let events = 0;
    let i = 0;
    const workers = Array.from({ length: Math.min(this.opts.maxConcurrent, mints.length) }, async () => {
      while (i < mints.length) {
        const mint = mints[i++]!;
        if (await this.pollOne(mint, slot)) events++;
      }
    });
    await Promise.all(workers);
    return { polled: mints.length, events };
  }

  private async pollOne(mint: string, slot: number | null): Promise<boolean> {
    const t = this.tracked.get(mint);
    if (!t) return false;
    if (this.now() - t.firstSeenMs > this.opts.trackWindowMs) {
      this.tracked.delete(mint);
      if (!t.fired) {
        this.deps.sink({ mint, kind: 'expired', slot, costSol: null, realTokenReserves: null, leadSlots: null, buyLegMs: null, buyLegOk: null });
        return true;
      }
      return false;
    }
    const reserves = await this.deps.fetchCurve(mint).catch(() => null);
    if (reserves?.complete) {
      this.tracked.delete(mint);
      const hasPool = this.deps.readPool ? await this.deps.readPool(mint).catch(() => null) : null;
      this.deps.sink({
        mint,
        kind: hasPool === false ? 'complete-no-pool' : 'migrated',
        slot,
        costSol: null,
        realTokenReserves: reserves.realTokenReserves.toString(),
        leadSlots: t.fired && t.fireSlot !== null && slot !== null ? slot - t.fireSlot : null,
        buyLegMs: null,
        buyLegOk: null,
      });
      return true;
    }
    if (t.fired || !reserves) return false;
    const decision = evaluateFire(reserves, this.opts.maxCostSol);
    if (!decision.fire) return false;
    t.fired = true;
    t.fireSlot = slot;
    let buyLegMs: number | null = null;
    let buyLegOk: boolean | null = null;
    if (this.deps.buildBuyLeg && decision.costSol !== null) {
      try {
        const leg = await this.deps.buildBuyLeg(mint, decision.costSol);
        buyLegMs = leg.ms;
        buyLegOk = leg.ok;
      } catch {
        buyLegOk = false;
      }
    }
    this.deps.sink({
      mint,
      kind: 'would-fire',
      slot,
      costSol: decision.costSol,
      realTokenReserves: reserves.realTokenReserves.toString(),
      leadSlots: null,
      buyLegMs,
      buyLegOk,
    });
    return true;
  }
}
