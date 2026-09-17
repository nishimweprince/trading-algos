/**
 * Cross-feed coverage tracker: which feeds reported each recent mint. Every
 * feed delivery is observed (before dedupe) so second-feed sightings are
 * visible; after `settleMs` a mint is settled and handed to the detector,
 * which applies two rules:
 *   - PumpPortal missed graduations on-chain feeds saw → reconnect PumpPortal.
 *   - On-chain feeds missed graduations PumpPortal saw while they were healthy
 *     → the migration authority / log format probably changed: alert.
 */
export interface SettledMint {
  mint: string;
  feeds: ReadonlySet<string>;
  firstSeenAtMs: number;
}

export class FeedCoverage {
  private readonly settleMs: number;
  private readonly now: () => number;
  private readonly pending = new Map<string, { feeds: Set<string>; firstSeenAtMs: number }>();

  constructor(opts: { settleMs: number; now?: () => number }) {
    this.settleMs = opts.settleMs;
    this.now = opts.now ?? Date.now;
  }

  observe(mint: string, feed: string): void {
    const entry = this.pending.get(mint);
    if (entry) {
      entry.feeds.add(feed);
      return;
    }
    this.pending.set(mint, { feeds: new Set([feed]), firstSeenAtMs: this.now() });
  }

  /** Mints first seen more than settleMs ago; removed from the tracker. */
  settle(now: number = this.now()): SettledMint[] {
    const out: SettledMint[] = [];
    for (const [mint, entry] of this.pending) {
      if (now - entry.firstSeenAtMs < this.settleMs) continue;
      this.pending.delete(mint);
      out.push({ mint, feeds: entry.feeds, firstSeenAtMs: entry.firstSeenAtMs });
    }
    return out;
  }

  get size(): number {
    return this.pending.size;
  }
}
