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
 */
import { deriveBondingCurvePda } from '../enrichment/curve.ts';
import { WSOL_MINT } from '../core/constants.ts';

export type TxMintLookup = (signature: string) => Promise<string[]>;

export interface MintResolverOptions {
  maxEntries?: number;
  retries?: number;
  retryMs?: number;
  /** Max lookups in flight; extra unknown curves wait for the next update. */
  maxInFlight?: number;
}

export class MintResolver<T> {
  private readonly lookup: TxMintLookup;
  private readonly map = new Map<string, string>();
  private readonly misses = new Set<string>();
  private readonly pending = new Map<string, T[]>();
  private readonly opts: Required<MintResolverOptions>;
  private readonly delay: (ms: number) => Promise<void>;
  readonly stats = { lookups: 0, resolved: 0, unresolved: 0, skippedBusy: 0 };

  constructor(lookup: TxMintLookup, opts: MintResolverOptions = {}, delay?: (ms: number) => Promise<void>) {
    this.lookup = lookup;
    this.opts = {
      maxEntries: opts.maxEntries ?? 50_000,
      retries: opts.retries ?? 3,
      retryMs: opts.retryMs ?? 800,
      maxInFlight: opts.maxInFlight ?? 8,
    };
    this.delay = delay ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
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
      queue.push(item);
      return;
    }
    if (!signature || this.misses.has(curve)) return;
    if (this.pending.size >= this.opts.maxInFlight) {
      this.stats.skippedBusy++;
      return;
    }
    this.pending.set(curve, [item]);
    void this.run(curve, signature, apply);
  }

  private async run(curve: string, signature: string, apply: (mint: string, item: T) => void): Promise<void> {
    this.stats.lookups++;
    let mint: string | undefined;
    for (let attempt = 0; attempt < this.opts.retries && !mint; attempt++) {
      if (attempt > 0) await this.delay(this.opts.retryMs);
      try {
        const mints = await this.lookup(signature);
        mint = mints.find((m) => m !== WSOL_MINT && deriveBondingCurvePda(m) === curve);
      } catch {
        /* retry */
      }
      if (!mint && this.map.has(curve)) mint = this.map.get(curve);
    }
    const queue = this.pending.get(curve) ?? [];
    this.pending.delete(curve);
    if (!mint) {
      this.stats.unresolved++;
      this.misses.add(curve);
      return;
    }
    this.stats.resolved++;
    this.noteMint(mint);
    for (const item of queue) apply(mint, item);
  }
}
