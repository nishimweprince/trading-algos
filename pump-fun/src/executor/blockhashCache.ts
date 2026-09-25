import type { Connection } from '@solana/web3.js';
import { logger } from '../core/logger.ts';

/**
 * Shared recent-blockhash cache.
 *
 * `assembleSignedSwapTx` fetched a blockhash on EVERY assemble, which meant one
 * serial round trip inside the entry's pre-send critical path and — worse — one
 * per tier on every `ExitLadder.refresh()`, i.e. 4 serial round trips every 45 s
 * per position and once synchronously while opening a live position.
 *
 * Blockhashes stay valid for ~60-90 s, so a short TTL is an enormous safety
 * margin. Two things make this safe rather than clever:
 *
 *  - A stale blockhash is INVISIBLE to simulation: `RpcTxSender.simulate` passes
 *    `replaceRecentBlockhash: true`, so it only ever bites at `sendRawTransaction`.
 *    The broadcaster therefore invalidates this cache and retries once on a
 *    BlockhashNotFound send error. Cost of that path is zero — the transaction
 *    never landed, so no fee was paid.
 *  - A failed refresh returns the previous value rather than throwing, and only
 *    a completely empty cache propagates the error. Fetching a blockhash must
 *    never be the reason a trade does not go out.
 */
export class BlockhashCache {
  private readonly connection: Connection;
  private readonly ttlMs: number;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'blockhash' });
  private value: string | null = null;
  private fetchedAtMs = 0;
  /** In-flight fetch, shared so N concurrent assembles cost one round trip. */
  private inflight: Promise<string> | null = null;

  constructor(connection: Connection, ttlMs: number, now: () => number = () => Date.now()) {
    this.connection = connection;
    this.ttlMs = ttlMs;
    this.now = now;
  }

  /** Cached blockhash, refreshed past the TTL. Shape matches a bare provider fn. */
  async get(): Promise<string> {
    if (this.value !== null && this.now() - this.fetchedAtMs < this.ttlMs) return this.value;
    if (this.inflight) return this.inflight;
    this.inflight = this.refresh().finally(() => {
      this.inflight = null;
    });
    return this.inflight;
  }

  /**
   * Drop the cached value. Called when a send fails with BlockhashNotFound, so
   * the retry cannot reuse the blockhash that just failed.
   */
  invalidate(): void {
    this.value = null;
    this.fetchedAtMs = 0;
  }

  private async refresh(): Promise<string> {
    try {
      const { blockhash } = await this.connection.getLatestBlockhash('confirmed');
      this.value = blockhash;
      this.fetchedAtMs = this.now();
      return blockhash;
    } catch (err) {
      if (this.value !== null) {
        // Serve the stale value: it is very likely still valid, and refusing to
        // assemble would turn a transient RPC blip into a missed trade.
        this.log.warn('blockhash refresh failed — serving the cached value', { err });
        return this.value;
      }
      throw err;
    }
  }
}

/** True when a send error means the blockhash we signed with is gone. */
export function isBlockhashNotFound(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err ?? '');
  return /blockhash\s*not\s*found/i.test(msg) || /BlockhashNotFound/.test(msg);
}
