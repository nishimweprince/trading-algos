import type { Mint } from '../core/types.ts';

/**
 * Cross-feed dedupe by mint with TTL (Section 4.2). The same graduation can
 * arrive from PumpPortal and gRPC within seconds; only the first is acted on.
 * Entries expire after ttlMs so long-running processes don't leak memory and a
 * re-graduation (rare) after the window is not silently dropped.
 */
export class MintDedupe {
  private readonly seen = new Map<Mint, number>();
  private readonly ttlMs: number;
  private readonly now: () => number;

  constructor(ttlMs: number, now: () => number = () => Date.now()) {
    this.ttlMs = ttlMs;
    this.now = now;
  }

  /** Returns true if this mint is new (and records it); false if a duplicate. */
  firstSeen(mint: Mint): boolean {
    const t = this.now();
    this.evict(t);
    const prior = this.seen.get(mint);
    if (prior !== undefined && t - prior < this.ttlMs) return false;
    this.seen.set(mint, t);
    return true;
  }

  private evict(t: number): void {
    for (const [mint, ts] of this.seen) {
      if (t - ts >= this.ttlMs) this.seen.delete(mint);
    }
  }

  get size(): number {
    return this.seen.size;
  }
}
