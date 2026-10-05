/**
 * Curve → mint for the racer. A bonding-curve account carries no mint, and
 * its address is a one-way PDA of it, so the map is filled from:
 *
 *   1. pump.fun creations seen on the racer stream (and the launches table at boot);
 *   2. mints named by migrate txs;
 *   3. otherwise one getTransaction on the update's own signature, keeping the
 *      mint whose curve PDA matches (curves that started before we booted).
 *
 * Updates for a curve still being resolved queue in arrival order and replay
 * once it resolves, so the complete flip is never lost to a pending lookup.
 * A mint learned another way while its lookup is in flight (the migrate tx
 * names it) flushes the queue at once, so curve state lands before the
 * migrate that follows it. A failed lookup is retried after a cooldown.
 */
import { deriveBondingCurvePda } from '../enrichment/curve.ts';
import { WSOL_MINT } from '../core/constants.ts';

export type TxMintLookup = (signature: string) => Promise<string[]>;

export interface MintResolverOptions {
  maxEntries?: number;
  retries?: number;
  retryMs?: number;
  /** After a failed lookup, ignore the curve this long. */
  missCooldownMs?: number;
  /** Max lookups in flight; extra unknown curves wait for the next update. */
  maxInFlight?: number;
}

export class MintResolver<T> {
  private readonly lookup: TxMintLookup;
  private readonly map = new Map<string, string>();
  /** Curve → time before which it is not looked up again. */
  private readonly misses = new Map<string, number>();
  private readonly pending = new Map<string, { items: T[]; apply: (mint: string, item: T) => void }>();
  private readonly now: () => number;
  private readonly opts: Required<MintResolverOptions>;
  private readonly delay: (ms: number) => Promise<void>;
  readonly stats = { lookups: 0, resolved: 0, unresolved: 0, skippedBusy: 0 };

  constructor(lookup: TxMintLookup, opts: MintResolverOptions = {}, delay?: (ms: number) => Promise<void>, now: () => number = Date.now) {
    this.lookup = lookup;
    this.opts = {
      maxEntries: opts.maxEntries ?? 50_000,
      retries: opts.retries ?? 6,
      retryMs: opts.retryMs ?? 1_000,
      maxInFlight: opts.maxInFlight ?? 8,
      missCooldownMs: opts.missCooldownMs ?? 10_000,
    };
    this.delay = delay ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
    this.now = now;
  }

  get size(): number {
    return this.map.size;
  }

  /** Remember a mint (creation / migrate / boot). */
  noteMint(mint: string): string | null {
    const curve = deriveBondingCurvePda(mint);
    if (!curve) return null;
    if (!this.map.has(curve)) {
      this.map.set(curve, mint);
      if (this.map.size > this.opts.maxEntries) {
        const oldest = this.map.keys().next().value;
        if (oldest !== undefined) this.map.delete(oldest);
      }
    }
    this.misses.delete(curve);
    const waiting = this.pending.get(curve);
    if (waiting) {
      this.pending.delete(curve);
      this.stats.resolved++;
      for (const item of waiting.items) waiting.apply(mint, item);
    }
    return curve;
  }

  mintOf(curve: string): string | undefined {
    return this.map.get(curve);
  }

  /**
   * Deliver `item` for `curve` to `apply` with its mint: at once when known,
   * after a lookup otherwise (queued behind earlier items for the same curve).
   */
  resolve(curve: string, signature: string | null, item: T, apply: (mint: string, item: T) => void): void {
    const known = this.map.get(curve);
    if (known) {
      apply(known, item);
      return;
    }
    const queue = this.pending.get(curve);
    if (queue) {
      queue.items.push(item);
      return;
    }
    if (!signature || (this.misses.get(curve) ?? 0) > this.now()) return;
    if (this.pending.size >= this.opts.maxInFlight) {
      this.stats.skippedBusy++;
      return;
    }
    this.pending.set(curve, { items: [item], apply });
    void this.run(curve, signature);
  }

  private async run(curve: string, signature: string): Promise<void> {
    this.stats.lookups++;
    let mint: string | undefined;
    for (let attempt = 0; attempt < this.opts.retries && !mint && this.pending.has(curve); attempt++) {
      if (attempt > 0) await this.delay(this.opts.retryMs);
      try {
        const mints = await this.lookup(signature);
        mint = mints.find((m) => m !== WSOL_MINT && deriveBondingCurvePda(m) === curve);
      } catch {
        /* retry */
      }
      if (!mint && this.map.has(curve)) mint = this.map.get(curve);
    }
    // Already flushed by noteMint while this lookup was in flight.
    if (!this.pending.has(curve)) return;
    if (!mint) {
      this.pending.delete(curve);
      this.stats.unresolved++;
      this.misses.set(curve, this.now() + this.opts.missCooldownMs);
      if (this.misses.size > this.opts.maxEntries) this.misses.clear();
      return;
    }
    this.noteMint(mint);
  }
}
