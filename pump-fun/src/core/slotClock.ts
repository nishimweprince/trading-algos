import type { RpcClient } from './rpc.ts';

/**
 * Process-wide view of the chain head, fed by the detection feeds' slot
 * subscriptions (Helius WS `slotSubscribe`, LaserStream slots filter) at zero
 * RPC cost. Consumers stamp chain-relative latency with it: slot at detection
 * vs the migration's slot, slot at submission vs the slot a tx landed in.
 *
 * A reading is `undefined` when nothing has been observed or the last push is
 * stale (no slot feed live); `current()` then falls back to one `getSlot`.
 */
export interface SlotClockOptions {
  rpc?: Pick<RpcClient, 'getSlot'> | undefined;
  /** A push reading older than this is treated as stale. Default 2000 ms. */
  staleAfterMs?: number;
  now?: () => number;
}

export interface SlotReading {
  slot: number;
  /** ms since the reading was observed. */
  ageMs: number;
  source: string;
}

export class SlotClock {
  private readonly rpc: Pick<RpcClient, 'getSlot'> | undefined;
  private readonly staleAfterMs: number;
  private readonly now: () => number;

  private latest: { slot: number; atMs: number; source: string } | null = null;

  constructor(opts: SlotClockOptions = {}) {
    this.rpc = opts.rpc;
    this.staleAfterMs = opts.staleAfterMs ?? 2_000;
    this.now = opts.now ?? Date.now;
  }

  /** Record an observed slot; monotonic (an older slot never rolls the clock back). */
  observe(slot: number, source: string): void {
    if (!Number.isFinite(slot) || slot < 0) return;
    if (this.latest && slot < this.latest.slot) return;
    this.latest = { slot, atMs: this.now(), source };
  }

  /** Synchronous reading; undefined when never observed or stale. */
  get(): SlotReading | undefined {
    if (!this.latest) return undefined;
    const ageMs = this.now() - this.latest.atMs;
    if (ageMs > this.staleAfterMs) return undefined;
    return { slot: this.latest.slot, ageMs, source: this.latest.source };
  }

  /** True while a fresh push reading exists (some slot feed is live). */
  get isLive(): boolean {
    return this.get() !== undefined;
  }

  /**
   * Fresh push reading, else one `getSlot('processed')` round trip. Never
   * throws; undefined when neither is available.
   */
  async current(): Promise<number | undefined> {
    const fresh = this.get();
    if (fresh) return fresh.slot;
    if (!this.rpc) return undefined;
    try {
      const slot = await this.rpc.getSlot('processed');
      this.observe(slot, 'rpc');
      return slot;
    } catch {
      return undefined;
    }
  }
}
