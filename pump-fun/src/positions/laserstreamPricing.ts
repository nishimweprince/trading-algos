import { base58Encode } from '../core/base58.ts';
import { logger, registerSecret } from '../core/logger.ts';
import type { Mint } from '../core/types.ts';
import { computePrice, type PoolRef, type PriceIngest, type PriceTick, type TickSink } from './pricing.ts';

/**
 * LaserStream account-subscribe price ingest (Business+ plan: mainnet gRPC).
 *
 * One gRPC stream carries account updates for every tracked pool's base and
 * quote vault (and the creator's ATA when dev-dump monitoring is on). Each
 * update lands within the slot it was written in, so an exit decision reads
 * the reserves ~400 ms after the swap that moved them instead of up to a full
 * poll interval later — the 4-pt trailing give-back and 5-pt stop overshoot
 * measured in the dry run were both that poll latency.
 *
 * Emits the same PriceTick shape as PricePoller into the same sinks, so the
 * live manager, the twin and shadow consume push and poll ticks through one
 * handler. The poller keeps running: it guarantees a tick per interval even
 * when nothing traded (time-stops must fire), and covers gaps while the SDK
 * reconnects.
 *
 * Balances are cached per account; a tick is emitted only once both vaults
 * of a pool are known, seeded from the reserves the position was priced off
 * so the very first vault update already produces a tick. Per-pool ticks are
 * coalesced to at most one per `minIntervalMs` (trailing edge kept) because a
 * hot pool can change every transaction and every tick is an FSM pass plus a
 * price_ticks row.
 *
 * Self-disabling: no endpoint → never connects, logs once. The SDK owns
 * reconnection + replay; the request is re-written whenever the tracked set
 * changes, and again after every (re)connect.
 */

/** LaserStream commitment: PROCESSED — earliest write, matches detection. */
const COMMITMENT_PROCESSED = 0;
/** Token account layout: amount is the u64 LE at offset 64. */
const TOKEN_AMOUNT_OFFSET = 64;

export interface LaserstreamPriceIngestOptions {
  endpoint: string;
  token?: string | undefined;
  /** Coalesce ticks per pool to one per this many ms (0 = every update). */
  minIntervalMs?: number;
  now?: () => number;
  /** Test hook: replaces the dynamic `helius-laserstream` import. */
  subscribeFn?: SubscribeFn;
}

type SubscribeFn = (
  config: { apiKey: string; endpoint: string; replay?: boolean },
  request: Record<string, unknown>,
  onData: (update: unknown) => void | Promise<void>,
  onError?: (error: unknown) => void | Promise<void>,
) => Promise<StreamHandle>;

interface StreamHandle {
  id: string;
  cancel(): void;
  write(request: unknown): void | Promise<void>;
}

interface Tracked {
  ref: PoolRef;
  sink: TickSink;
  lastEmitAtMs: number;
  trailing: NodeJS.Timeout | null;
}

export class LaserstreamPriceIngest implements PriceIngest {
  private readonly endpoint: string;
  private readonly token: string | undefined;
  private readonly minIntervalMs: number;
  private readonly now: () => number;
  private readonly subscribeFn: SubscribeFn | undefined;
  private readonly log = logger.child({ mod: 'laserstream-pricing' });

  private readonly tracked = new Map<Mint, Tracked>();
  /** account address → latest raw token amount, across every tracked pool. */
  private readonly balances = new Map<string, bigint>();
  /** account address → mints that read it (a creator ATA may serve one pool; vaults are unique). */
  private readonly accountToMints = new Map<string, Set<Mint>>();

  private handle: StreamHandle | null = null;
  private connecting = false;
  private stopped = false;
  private healthy = false;
  private updates = 0;
  private ticks = 0;
  private lastWrittenKey = '';
  /** Wall clock of the last emitted tick — the liveness signal. */
  private lastTickAtMs: number | null = null;
  /** When the current stream came up, so staleness has a baseline before tick 1. */
  private subscribedAtMs: number | null = null;

  constructor(opts: LaserstreamPriceIngestOptions) {
    this.endpoint = opts.endpoint;
    this.token = opts.token;
    this.minIntervalMs = opts.minIntervalMs ?? 100;
    this.now = opts.now ?? (() => Date.now());
    this.subscribeFn = opts.subscribeFn;
    if (this.token) registerSecret(this.token);
    if (this.endpoint) registerSecret(this.endpoint);
  }

  get size(): number {
    return this.tracked.size;
  }

  get stats(): {
    tracked: number;
    accounts: number;
    updates: number;
    ticks: number;
    healthy: boolean;
    lastTickAtMs: number | null;
  } {
    return {
      tracked: this.tracked.size,
      accounts: this.accountToMints.size,
      updates: this.updates,
      ticks: this.ticks,
      healthy: this.healthy,
      lastTickAtMs: this.lastTickAtMs,
    };
  }

  /**
   * Reconnect when the stream is tracking pools but has gone silent.
   *
   * The SDK owns its own reconnect, and it re-subscribes with the request it
   * was originally given — which can come back without our current account
   * filter. `resubscribe()` then early-returns because the key still matches
   * `lastWrittenKey`, so the stream stays subscribed to the wrong set and
   * delivers nothing, at warn level, forever. A full teardown is the only thing
   * that provably re-sends the account set.
   *
   * Safe to call on a timer. Returns true when a reconnect was started.
   */
  reconnectIfStale(staleMs: number): boolean {
    if (this.stopped || !this.endpoint || this.connecting) return false;
    if (this.tracked.size === 0) return false;
    const since = this.lastTickAtMs ?? this.subscribedAtMs;
    if (since === null || this.now() - since < staleMs) return false;
    this.log.warn('laserstream price ingest silent — tearing down and reconnecting', {
      tracked: this.tracked.size,
      silentMs: this.now() - since,
      ticks: this.ticks,
    });
    try {
      this.handle?.cancel();
    } catch (err) {
      this.log.debug('laserstream cancel during stale reconnect failed', { err });
    }
    this.handle = null;
    this.healthy = false;
    this.lastWrittenKey = '';
    this.subscribedAtMs = this.now();
    void this.connect();
    return true;
  }

  start(): void {
    this.stopped = false;
    if (!this.endpoint) {
      this.log.warn('laserstream price ingest enabled but rpc.primaryGrpc is blank — push ticks disabled, poller carries pricing');
      return;
    }
    void this.connect();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    for (const t of this.tracked.values()) if (t.trailing) clearTimeout(t.trailing);
    this.tracked.clear();
    this.balances.clear();
    this.accountToMints.clear();
    try {
      this.handle?.cancel();
    } catch {
      /* ignore */
    }
    this.handle = null;
    this.healthy = false;
  }

  register(ref: PoolRef, sink: TickSink, seed?: { baseReserve: bigint; quoteReserveLamports: bigint }): void {
    if (this.tracked.has(ref.mint)) this.unregister(ref.mint);
    this.tracked.set(ref.mint, { ref, sink, lastEmitAtMs: 0, trailing: null });
    for (const acct of this.accountsOf(ref)) {
      let set = this.accountToMints.get(acct);
      if (!set) this.accountToMints.set(acct, (set = new Set()));
      set.add(ref.mint);
    }
    // Seed so the first single-vault update already yields a full tick. Only
    // when nothing fresher is cached: a vault shared across registrations
    // (re-register after a restart) must not be rolled back.
    if (seed) {
      if (!this.balances.has(ref.baseVault) && seed.baseReserve > 0n) this.balances.set(ref.baseVault, seed.baseReserve);
      if (!this.balances.has(ref.quoteVault) && seed.quoteReserveLamports > 0n) {
        this.balances.set(ref.quoteVault, seed.quoteReserveLamports);
      }
    }
    void this.resubscribe();
  }

  unregister(mint: Mint): void {
    const t = this.tracked.get(mint);
    if (!t) return;
    if (t.trailing) clearTimeout(t.trailing);
    this.tracked.delete(mint);
    for (const acct of this.accountsOf(t.ref)) {
      const set = this.accountToMints.get(acct);
      if (!set) continue;
      set.delete(mint);
      if (set.size === 0) {
        this.accountToMints.delete(acct);
        this.balances.delete(acct);
      }
    }
    void this.resubscribe();
  }

  /**
   * Test / replay hook: apply one account update. Returns the ticks emitted
   * (or scheduled for the trailing edge, in which case the list is empty).
   */
  applyAccountUpdate(address: string, rawAmount: bigint, atMs?: number): PriceTick[] {
    this.updates++;
    const mints = this.accountToMints.get(address);
    if (!mints) return [];
    this.balances.set(address, rawAmount);
    const out: PriceTick[] = [];
    for (const mint of mints) {
      const tick = this.emitFor(mint, atMs ?? this.now());
      if (tick) out.push(tick);
    }
    return out;
  }

  private accountsOf(ref: PoolRef): string[] {
    return ref.creatorAta ? [ref.baseVault, ref.quoteVault, ref.creatorAta] : [ref.baseVault, ref.quoteVault];
  }

  private buildTick(t: Tracked, atMs: number): PriceTick | null {
    const base = this.balances.get(t.ref.baseVault);
    const quote = this.balances.get(t.ref.quoteVault);
    if (base === undefined || quote === undefined) return null;
    const tick: PriceTick = {
      mint: t.ref.mint,
      price: computePrice(base, quote, t.ref.baseDecimals),
      baseReserve: base,
      quoteReserveLamports: quote,
      atMs,
    };
    if (t.ref.creatorAta) {
      const creator = this.balances.get(t.ref.creatorAta);
      if (creator !== undefined) tick.creatorBaseBalance = creator;
    }
    return tick;
  }

  private emitFor(mint: Mint, atMs: number, force = false): PriceTick | null {
    const t = this.tracked.get(mint);
    if (!t) return null;
    const since = atMs - t.lastEmitAtMs;
    if (!force && this.minIntervalMs > 0 && since < this.minIntervalMs) {
      // Coalesce: keep the trailing edge so the last state in a burst is
      // never lost, without an FSM pass per transaction. The trailing emit is
      // forced — it was scheduled for exactly the interval boundary.
      if (!t.trailing) {
        t.trailing = setTimeout(() => {
          t.trailing = null;
          this.emitFor(mint, this.now(), true);
        }, this.minIntervalMs - since);
        t.trailing.unref?.();
      }
      return null;
    }
    const tick = this.buildTick(t, atMs);
    if (!tick) return null;
    t.lastEmitAtMs = atMs;
    this.ticks++;
    this.lastTickAtMs = this.now();
    try {
      t.sink(tick);
    } catch (err) {
      this.log.debug('laserstream tick sink failed', { mint, err });
    }
    return tick;
  }

  private request(): Record<string, unknown> {
    const accounts = [...this.accountToMints.keys()];
    return {
      accounts: accounts.length
        ? { vaults: { account: accounts, owner: [], filters: [], nonemptyTxnSignature: false } }
        : {},
      slots: {},
      transactions: {},
      transactionsStatus: {},
      blocks: {},
      blocksMeta: {},
      entry: {},
      accountsDataSlice: [],
      commitment: COMMITMENT_PROCESSED,
    };
  }

  private async resubscribe(): Promise<void> {
    if (this.stopped || !this.endpoint) return;
    if (!this.handle) {
      // connect() writes the current set once the stream is up.
      if (!this.connecting) void this.connect();
      return;
    }
    const key = [...this.accountToMints.keys()].sort().join(',');
    if (key === this.lastWrittenKey) return;
    this.lastWrittenKey = key;
    try {
      await this.handle.write(this.request());
    } catch (err) {
      this.log.warn('laserstream subscription update failed — will retry on next change', { err });
      this.lastWrittenKey = '';
    }
  }

  private async connect(): Promise<void> {
    if (this.stopped || this.connecting || this.handle) return;
    this.connecting = true;
    try {
      const subscribe = this.subscribeFn ?? (await loadSdk());
      if (!subscribe) {
        this.log.warn('helius-laserstream SDK missing — push ticks disabled, poller carries pricing');
        return;
      }
      this.lastWrittenKey = [...this.accountToMints.keys()].sort().join(',');
      const handle = await subscribe(
        { apiKey: this.token ?? '', endpoint: this.endpoint, replay: false },
        this.request(),
        (data) => this.onUpdate(data),
        (err) => {
          this.healthy = false;
          // Clear the written-filter key so the next resubscribe() actually
          // re-pushes the account set. The SDK reconnects with the request it
          // was originally handed, which may no longer match what we track,
          // and without this the key-equality short-circuit keeps it wrong.
          this.lastWrittenKey = '';
          this.log.warn('laserstream pricing stream error (sdk reconnecting)', { detail: describeError(err) });
          void this.resubscribe();
        },
      );
      if (this.stopped) {
        handle.cancel();
        return;
      }
      this.handle = handle;
      this.healthy = true;
      this.subscribedAtMs = this.now();
      this.log.info('laserstream price ingest subscribed', { accounts: this.accountToMints.size });
      // The tracked set may have changed while the connect was in flight.
      this.lastWrittenKey = '';
      await this.resubscribe();
    } catch (err) {
      this.healthy = false;
      this.log.warn('laserstream price ingest subscribe failed — poller carries pricing', { detail: describeError(err) });
    } finally {
      this.connecting = false;
    }
  }

  private onUpdate(data: unknown): void {
    this.healthy = true;
    const parsed = extractAccountUpdate(data);
    if (!parsed) return;
    this.applyAccountUpdate(parsed.address, parsed.amount);
  }
}

/**
 * Pull (address, token amount) out of a SubscribeUpdate carrying an account
 * update; null for pings, slots, non-token accounts and malformed payloads.
 */
export function extractAccountUpdate(data: unknown): { address: string; amount: bigint } | null {
  const acc = (data as { account?: { account?: unknown } } | null)?.account?.account as
    | { pubkey?: unknown; data?: unknown }
    | undefined;
  if (!acc) return null;
  const address = encodeKey(acc.pubkey);
  const bytes = toBytes(acc.data);
  if (!address || !bytes || bytes.length < TOKEN_AMOUNT_OFFSET + 8) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return { address, amount: view.getBigUint64(TOKEN_AMOUNT_OFFSET, true) };
}

function encodeKey(k: unknown): string | null {
  if (typeof k === 'string') return k;
  const bytes = toBytes(k);
  return bytes && bytes.length === 32 ? base58Encode(bytes) : null;
}

function toBytes(v: unknown): Uint8Array | null {
  if (v instanceof Uint8Array) return v;
  if (Array.isArray(v)) return Uint8Array.from(v as number[]);
  if (typeof v === 'string') return Uint8Array.from(Buffer.from(v, 'base64'));
  if (v && typeof v === 'object' && 'data' in v && Array.isArray((v as { data: unknown }).data)) {
    return Uint8Array.from((v as { data: number[] }).data);
  }
  return null;
}

async function loadSdk(): Promise<SubscribeFn | null> {
  try {
    const mod = (await import('helius-laserstream')) as unknown as { subscribe: SubscribeFn };
    return mod.subscribe;
  } catch {
    return null;
  }
}

function describeError(ev: unknown): string {
  if (ev instanceof Error) return ev.message;
  if (ev && typeof ev === 'object' && 'message' in ev) return String((ev as { message: unknown }).message);
  return 'unknown';
}
