import type { RpcClient } from '../core/rpc.ts';
import type { Mint, Address } from '../core/types.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import { decodeTokenAccountAmount } from '../enrichment/pool.ts';
import { logger } from '../core/logger.ts';
import { withTimeout, TimeoutError } from '../executor/timeout.ts';

/** One entry of a getMultipleAccounts response. */
type AccountRead = Awaited<ReturnType<RpcClient['getMultipleAccountsBase64']>>[number];

/**
 * Local pool pricing (Section 7.2). Price is computed from the pool's vault
 * balances — never an external price API — so exit decisions run on data we
 * control. On the free tier we poll the vaults; the paid gRPC feed would give
 * per-slot updates as a drop-in later.
 *
 * Price is SOL per whole token: (quoteLamports / 1e9) / (baseReserve / 10^dec).
 */
export function computePrice(baseReserve: bigint, quoteReserveLamports: bigint, baseDecimals: number): number {
  if (baseReserve === 0n) return 0;
  const quoteSol = Number(quoteReserveLamports) / LAMPORTS_PER_SOL;
  const baseTokens = Number(baseReserve) / 10 ** baseDecimals;
  return baseTokens === 0 ? 0 : quoteSol / baseTokens;
}

export interface PoolRef {
  mint: Mint;
  baseVault: Address;
  quoteVault: Address;
  baseDecimals: number;
  /** Creator's base-token ATA — monitored for dev-dump (batched into the same fetch). */
  creatorAta?: Address;
}

export interface PriceTick {
  mint: Mint;
  price: number;
  baseReserve: bigint;
  quoteReserveLamports: bigint;
  atMs: number;
  /** Creator base-token balance this tick (undefined when unregistered/absent). */
  creatorBaseBalance?: bigint;
}

export type TickSink = (tick: PriceTick) => void;

/**
 * A PUSH tick source (Helius webhook, LaserStream account-subscribe) that
 * mirrors the pools a poller tracks and emits the same PriceTick shape into
 * the same handler. The poller stays the liveness fallback.
 */
export interface PriceIngest {
  register(ref: PoolRef, sink: TickSink, seed?: { baseReserve: bigint; quoteReserveLamports: bigint }): void;
  unregister(mint: Mint): void;
}

export interface PriceRead {
  price: number;
  baseReserve: bigint;
  quoteReserveLamports: bigint;
}

export interface PricePollerOptions {
  /**
   * Commitment for vault reads. MUST match the commitment the entry path
   * quotes at: a pool created 1-2 slots ago has vault accounts that are not
   * yet visible at 'confirmed', and a miss emits NO tick at all — the exit
   * FSM then runs blind. 14 of 25 live positions exited on a single tick
   * because of this. See positions.priceCommitment.
   */
  commitment?: 'processed' | 'confirmed' | 'finalized';
  /** Max pubkeys per getMultipleAccounts call (Solana's server-side cap is 100). */
  batchSize?: number;
  /**
   * A poll cycle may never hold the in-flight guard longer than this. RpcClient's
   * own timeoutMs does NOT bound it — Semaphore.acquire() sits outside the
   * AbortController — so without a deadline one queued read starves EVERY
   * registered position for the whole queue wait.
   */
  deadlineMs?: number;
}

/** Per-mint tick delivery health. Absent tick delivery used to be silent. */
export interface MintPriceHealth {
  registeredAtMs: number;
  lastTickAtMs: number | null;
  ticks: number;
  /** Batch returned null for base or quote — this mint got no tick that cycle. */
  consecutiveMisses: number;
  totalMisses: number;
}

export interface PollStats {
  cycles: number;
  overlapSkips: number;
  deadlineExpired: number;
  failures: number;
  lastErr: string | null;
}

const DEFAULT_BATCH_SIZE = 100;
const MISS_LOG_EVERY = 5;
const FAILURE_LOG_MIN_INTERVAL_MS = 5_000;

/**
 * Polls the vault balances of every registered pool on a fixed cadence and
 * pushes a PriceTick per pool per tick (even when the price is unchanged, so
 * time-based exits still fire). Vaults are fetched in batched
 * getMultipleAccounts calls (chunked at `batchSize`) to minimise RPC load.
 */
export class PricePoller {
  private readonly rpc: RpcClient;
  private readonly intervalMs: number;
  private readonly log = logger.child({ mod: 'pricing' });
  private readonly refs = new Map<Mint, PoolRef>();
  private readonly health = new Map<Mint, MintPriceHealth>();
  private timer: NodeJS.Timeout | null = null;
  private polling = false;
  private onTick: (tick: PriceTick) => void = () => {};
  private readonly now: () => number;
  private readonly commitment: 'processed' | 'confirmed' | 'finalized';
  private readonly batchSize: number;
  private readonly deadlineMs: number;
  private readonly stats: PollStats = {
    cycles: 0,
    overlapSkips: 0,
    deadlineExpired: 0,
    failures: 0,
    lastErr: null,
  };
  private consecutiveOverlapSkips = 0;
  private lastFailureLogAtMs = 0;

  constructor(
    rpc: RpcClient,
    intervalMs: number,
    now: () => number = () => Date.now(),
    opts: PricePollerOptions = {},
  ) {
    this.rpc = rpc;
    this.intervalMs = intervalMs;
    this.now = now;
    this.commitment = opts.commitment ?? 'confirmed';
    this.batchSize = Math.min(opts.batchSize ?? DEFAULT_BATCH_SIZE, DEFAULT_BATCH_SIZE);
    this.deadlineMs = opts.deadlineMs ?? 0;
  }

  setHandler(handler: (tick: PriceTick) => void): void {
    this.onTick = handler;
  }

  register(ref: PoolRef): void {
    this.refs.set(ref.mint, ref);
    if (!this.health.has(ref.mint)) {
      this.health.set(ref.mint, {
        registeredAtMs: this.now(),
        lastTickAtMs: null,
        ticks: 0,
        consecutiveMisses: 0,
        totalMisses: 0,
      });
    }
  }

  unregister(mint: Mint): void {
    this.refs.delete(mint);
    this.health.delete(mint);
  }

  get size(): number {
    return this.refs.size;
  }

  /** Tick-delivery health for one mint, or null when not registered. */
  healthFor(mint: Mint): MintPriceHealth | null {
    return this.health.get(mint) ?? null;
  }

  /** Aggregate poll-loop health. Consumed by the manager's degraded-poller alert. */
  get pollStats(): PollStats {
    return { ...this.stats };
  }

  async readOnce(ref: Pick<PoolRef, 'baseVault' | 'quoteVault' | 'baseDecimals'>): Promise<PriceRead | null> {
    try {
      const [baseAcct, quoteAcct] = await this.rpc.getMultipleAccountsBase64(
        [ref.baseVault, ref.quoteVault],
        this.commitment,
      );
      if (!baseAcct || !quoteAcct) {
        this.log.warn('single price read: vault account not visible', {
          baseVault: ref.baseVault,
          commitment: this.commitment,
        });
        return null;
      }
      const baseReserve = decodeTokenAccountAmount(baseAcct.data);
      const quoteReserveLamports = decodeTokenAccountAmount(quoteAcct.data);
      return {
        price: computePrice(baseReserve, quoteReserveLamports, ref.baseDecimals),
        baseReserve,
        quoteReserveLamports,
      };
    } catch (err) {
      this.log.warn('single price read failed', { err });
      return null;
    }
  }

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => void this.poll(), this.intervalMs);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.refs.clear();
  }

  private async poll(): Promise<void> {
    if (this.refs.size === 0) return;
    if (this.polling) {
      // Overlap skip drops the cycle for EVERY position, so it is counted and
      // escalated rather than silently swallowed. The deadline below bounds it.
      this.stats.overlapSkips++;
      this.consecutiveOverlapSkips++;
      if (this.consecutiveOverlapSkips % 3 === 0) {
        this.log.warn('price poll cycles skipped — a read is still in flight', {
          consecutive: this.consecutiveOverlapSkips,
          tracked: this.refs.size,
        });
      }
      return;
    }
    this.consecutiveOverlapSkips = 0;
    this.polling = true;
    this.stats.cycles++;
    try {
      const refs = [...this.refs.values()];
      // Build a flat address list (base, quote, [creatorAta]) with an index map
      // so a variable number of accounts per ref still maps back correctly after
      // the list is chunked at the 100-pubkey getMultipleAccounts cap.
      const addrs: string[] = [];
      const idx: Array<{ base: number; quote: number; creator: number | null }> = [];
      for (const r of refs) {
        const base = addrs.push(r.baseVault) - 1;
        const quote = addrs.push(r.quoteVault) - 1;
        const creator = r.creatorAta ? addrs.push(r.creatorAta) - 1 : null;
        idx.push({ base, quote, creator });
      }
      const accts = await this.fetchAccounts(addrs);
      const atMs = this.now();
      for (let i = 0; i < refs.length; i++) {
        const ref = refs[i]!;
        const map = idx[i]!;
        const baseAcct = accts[map.base];
        const quoteAcct = accts[map.quote];
        const h = this.health.get(ref.mint);
        if (!baseAcct || !quoteAcct) {
          // Per-mint, not per-batch: one unreadable pool must not cost the others
          // anything, and it must be attributable instead of a bare `continue`.
          if (h) {
            h.consecutiveMisses++;
            h.totalMisses++;
            if (h.consecutiveMisses === 1 || h.consecutiveMisses % MISS_LOG_EVERY === 0) {
              this.log.warn('price poll: vault account not visible — no tick for this mint', {
                mint: ref.mint,
                consecutiveMisses: h.consecutiveMisses,
                commitment: this.commitment,
              });
            }
          }
          continue;
        }
        if (h) {
          h.consecutiveMisses = 0;
          h.ticks++;
          h.lastTickAtMs = atMs;
        }
        const baseReserve = decodeTokenAccountAmount(baseAcct.data);
        const quoteReserveLamports = decodeTokenAccountAmount(quoteAcct.data);
        const price = computePrice(baseReserve, quoteReserveLamports, ref.baseDecimals);
        const creatorAcct = map.creator !== null ? accts[map.creator] : null;
        const creatorBaseBalance = creatorAcct ? decodeTokenAccountAmount(creatorAcct.data) : undefined;
        this.onTick({
          mint: ref.mint,
          price,
          baseReserve,
          quoteReserveLamports,
          atMs,
          ...(creatorBaseBalance !== undefined ? { creatorBaseBalance } : {}),
        });
      }
    } catch (err) {
      this.stats.failures++;
      this.stats.lastErr = err instanceof Error ? err.message : String(err);
      if (err instanceof TimeoutError) this.stats.deadlineExpired++;
      const now = this.now();
      if (now - this.lastFailureLogAtMs >= FAILURE_LOG_MIN_INTERVAL_MS) {
        this.lastFailureLogAtMs = now;
        this.log.warn('price poll failed — every tracked position missed this tick', {
          tracked: this.refs.size,
          failures: this.stats.failures,
          err,
        });
      }
    } finally {
      this.polling = false;
    }
  }

  /**
   * Chunked at the 100-pubkey getMultipleAccounts cap and bounded by
   * `deadlineMs`. Chunks go out in parallel: they are independent reads and
   * serialising them would multiply the starvation window by the chunk count.
   */
  private async fetchAccounts(addrs: string[]): Promise<AccountRead[]> {
    const chunks: string[][] = [];
    for (let i = 0; i < addrs.length; i += this.batchSize) {
      chunks.push(addrs.slice(i, i + this.batchSize));
    }
    const read = Promise.all(chunks.map((c) => this.rpc.getMultipleAccountsBase64(c, this.commitment))).then((parts) =>
      parts.flat(),
    );
    return this.deadlineMs > 0 ? withTimeout(read, this.deadlineMs, 'price poll') : read;
  }
}
