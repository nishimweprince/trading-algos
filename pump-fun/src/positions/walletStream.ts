import { base58Encode } from '../core/base58.ts';
import { logger, registerSecret } from '../core/logger.ts';

/**
 * Our own wallet's transactions, pushed by LaserStream at `processed`.
 *
 * Why (2026-10-03): three buys landed while Helius answered 429 and the
 * secondary RPC was out of quota. Every way the bot learned about a landed
 * buy — signature status, token balance reads, the orphan sweep — was an
 * HTTP poll against those same endpoints, so the buys were declared FAILED,
 * held no exits, and were sold 93–96 % down. This stream needs no HTTP RPC:
 * every transaction that touches the wallet arrives with its error, its fee
 * payer, our SOL delta and our per-mint token deltas.
 *
 * Consumers:
 *  - the buy path waits on `waitForMintIncrease` (passed to the broadcaster
 *    as its `landed` signal) and takes the filled amount from the delta;
 *  - sent-buy resolution races `waitForSignature` against RPC polling;
 *  - orphan adoption and the foreign-signer guard subscribe to every tx.
 *
 * A short history buffer answers waits registered just after the tx arrived.
 */

/** LaserStream commitment: PROCESSED. */
const COMMITMENT_PROCESSED = 0;
const HISTORY_MS = 120_000;
const HISTORY_MAX = 512;

export interface WalletTokenDelta {
  /** post − pre raw amount over every token account the wallet owns for this mint. */
  raw: bigint;
  decimals: number;
  programId?: string;
}

export interface WalletTx {
  signature: string;
  slot?: number;
  /** Non-null when the transaction failed on-chain. */
  err: unknown;
  feePayer: string | null;
  /** Our wallet's lamport change (fee included); 0n when the wallet is not a static key. */
  solDeltaLamports: bigint;
  tokenDeltas: Map<string, WalletTokenDelta>;
  seenAtMs: number;
}

export interface WalletWatch {
  readonly healthy: boolean;
  /** Start of the current unbroken stream window, or null while down. */
  readonly upSinceMs: number | null;
  /** Resolves with the tx once seen (or null on timeout). Checks recent history first. */
  waitForSignature(signature: string, timeoutMs: number): Promise<WalletTx | null>;
  /**
   * First successful tx, seen from now on (or in the last `lookbackMs`), that
   * raised our balance of `mint`. `cancel()` resolves the promise with null.
   */
  waitForMintIncrease(mint: string, timeoutMs: number, lookbackMs?: number): { promise: Promise<WalletTx | null>; cancel(): void };
  onTransaction(handler: (tx: WalletTx) => void): () => void;
}

type SubscribeFn = (
  config: { apiKey: string; endpoint: string; replay?: boolean },
  request: Record<string, unknown>,
  onData: (update: unknown) => void | Promise<void>,
  onError?: (error: unknown) => void | Promise<void>,
) => Promise<{ cancel(): void }>;

export interface WalletStreamOptions {
  endpoint: string;
  token?: string | undefined;
  wallet: string;
  now?: () => number;
  /** Test hook: replaces the dynamic `helius-laserstream` import. */
  subscribeFn?: SubscribeFn;
}

interface Waiter {
  match: (tx: WalletTx) => boolean;
  resolve: (tx: WalletTx | null) => void;
  timer: NodeJS.Timeout | null;
}

export class WalletStream implements WalletWatch {
  private readonly endpoint: string;
  private readonly token: string | undefined;
  private readonly wallet: string;
  private readonly now: () => number;
  private readonly subscribeFn: SubscribeFn | undefined;
  private readonly log = logger.child({ mod: 'wallet-stream' });

  private handle: { cancel(): void } | null = null;
  private stopped = false;
  private connecting = false;
  private upSince: number | null = null;
  private readonly history: WalletTx[] = [];
  private readonly waiters = new Set<Waiter>();
  private readonly handlers = new Set<(tx: WalletTx) => void>();
  private reconnectTimer: NodeJS.Timeout | null = null;
  private attempts = 0;
  private seenCount = 0;

  constructor(opts: WalletStreamOptions) {
    this.endpoint = opts.endpoint;
    this.token = opts.token;
    this.wallet = opts.wallet;
    this.now = opts.now ?? Date.now;
    this.subscribeFn = opts.subscribeFn;
    if (this.token) registerSecret(this.token);
    if (this.endpoint) registerSecret(this.endpoint);
  }

  get healthy(): boolean {
    return this.upSince !== null;
  }

  get upSinceMs(): number | null {
    return this.upSince;
  }

  get stats(): { healthy: boolean; seen: number; waiters: number } {
    return { healthy: this.healthy, seen: this.seenCount, waiters: this.waiters.size };
  }

  start(): void {
    this.stopped = false;
    if (!this.endpoint) {
      this.log.warn('wallet stream disabled — rpc.primaryGrpc is blank; landed buys are found by RPC polling only');
      return;
    }
    void this.connect();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.upSince = null;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    try {
      this.handle?.cancel();
    } catch {
      /* ignore */
    }
    this.handle = null;
    for (const w of this.waiters) this.settle(w, null);
  }

  onTransaction(handler: (tx: WalletTx) => void): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  waitForSignature(signature: string, timeoutMs: number): Promise<WalletTx | null> {
    const hit = this.history.find((t) => t.signature === signature);
    if (hit) return Promise.resolve(hit);
    return this.addWaiter((tx) => tx.signature === signature, timeoutMs).promise;
  }

  waitForMintIncrease(mint: string, timeoutMs: number, lookbackMs = 0): { promise: Promise<WalletTx | null>; cancel(): void } {
    const increased = (tx: WalletTx) => !tx.err && (tx.tokenDeltas.get(mint)?.raw ?? 0n) > 0n;
    if (lookbackMs > 0) {
      const since = this.now() - lookbackMs;
      const hit = this.history.find((t) => t.seenAtMs >= since && increased(t));
      if (hit) return { promise: Promise.resolve(hit), cancel: () => {} };
    }
    return this.addWaiter(increased, timeoutMs);
  }

  /** Test / replay hook: feed one raw SubscribeUpdate. */
  ingest(data: unknown): WalletTx | null {
    const tx = extractWalletTx(data, this.wallet, this.now());
    if (!tx) return null;
    if (this.history.some((t) => t.signature === tx.signature)) return null; // replay / dup
    this.seenCount++;
    this.history.push(tx);
    const cutoff = this.now() - HISTORY_MS;
    while (this.history.length > HISTORY_MAX || (this.history[0] && this.history[0].seenAtMs < cutoff)) this.history.shift();
    for (const w of [...this.waiters]) if (w.match(tx)) this.settle(w, tx);
    for (const h of this.handlers) {
      try {
        h(tx);
      } catch (err) {
        this.log.warn('wallet tx handler failed', { err, signature: tx.signature });
      }
    }
    return tx;
  }

  private addWaiter(match: (tx: WalletTx) => boolean, timeoutMs: number): { promise: Promise<WalletTx | null>; cancel(): void } {
    let waiter!: Waiter;
    const promise = new Promise<WalletTx | null>((resolve) => {
      waiter = { match, resolve, timer: null };
    });
    if (timeoutMs > 0) {
      waiter.timer = setTimeout(() => this.settle(waiter, null), timeoutMs);
      waiter.timer.unref?.();
    }
    this.waiters.add(waiter);
    return { promise, cancel: () => this.settle(waiter, null) };
  }

  private settle(w: Waiter, tx: WalletTx | null): void {
    if (!this.waiters.delete(w)) return;
    if (w.timer) clearTimeout(w.timer);
    w.resolve(tx);
  }

  private request(): Record<string, unknown> {
    return {
      accounts: {},
      slots: {},
      transactions: {
        wallet: { vote: false, failed: true, accountInclude: [this.wallet], accountExclude: [], accountRequired: [] },
      },
      transactionsStatus: {},
      blocks: {},
      blocksMeta: {},
      entry: {},
      accountsDataSlice: [],
      commitment: COMMITMENT_PROCESSED,
    };
  }

  private async connect(): Promise<void> {
    this.reconnectTimer = null;
    if (this.stopped || this.connecting || this.handle) return;
    this.connecting = true;
    try {
      const subscribe = this.subscribeFn ?? (await loadSdk());
      if (!subscribe) {
        this.log.warn('helius-laserstream SDK missing — wallet stream disabled');
        return;
      }
      const handle = await subscribe(
        // replay: a tx that landed during an SDK reconnect must still arrive.
        { apiKey: this.token ?? '', endpoint: this.endpoint, replay: true },
        this.request(),
        (data) => {
          if (this.upSince === null) this.upSince = this.now();
          this.ingest(data);
        },
        (err) => {
          this.upSince = null;
          this.log.warn('wallet stream error (sdk reconnecting)', { detail: describeError(err) });
        },
      );
      if (this.stopped) {
        handle.cancel();
        return;
      }
      this.handle = handle;
      this.attempts = 0;
      this.upSince = this.now();
      this.log.info('wallet stream subscribed', { wallet: short(this.wallet) });
    } catch (err) {
      this.upSince = null;
      this.log.warn('wallet stream subscribe failed', { detail: describeError(err) });
      const wait = Math.min(500 * 2 ** this.attempts++, 30_000);
      this.reconnectTimer = setTimeout(() => void this.connect(), wait);
      this.reconnectTimer.unref?.();
    } finally {
      this.connecting = false;
    }
  }
}

interface RawTokenBalance {
  accountIndex?: unknown;
  mint?: unknown;
  owner?: unknown;
  programId?: unknown;
  uiTokenAmount?: { amount?: unknown; decimals?: unknown };
}

/**
 * Decode a LaserStream transaction update into our wallet's view of it.
 * Tolerates the SDK nesting (`transaction.transaction.{signature, meta,
 * transaction.message}`) and byte/base58 key encodings.
 */
export function extractWalletTx(data: unknown, wallet: string, seenAtMs: number): WalletTx | null {
  const outer = (data as { transaction?: { transaction?: unknown; slot?: unknown } } | null)?.transaction;
  if (!outer) return null;
  const inner = (outer.transaction ?? outer) as {
    signature?: unknown;
    meta?: {
      err?: unknown;
      preBalances?: unknown[];
      postBalances?: unknown[];
      preTokenBalances?: RawTokenBalance[];
      postTokenBalances?: RawTokenBalance[];
    };
    transaction?: { message?: { accountKeys?: unknown[] } };
  };
  const signature = encodeKey(inner.signature, 64);
  if (!signature) return null;
  const meta = inner.meta ?? {};
  const keys = (inner.transaction?.message?.accountKeys ?? []).map((k) => encodeKey(k, 32));
  const walletIndex = keys.indexOf(wallet);
  const lamports = (arr: unknown[] | undefined, i: number): bigint => toBigInt(arr?.[i]) ?? 0n;
  const solDeltaLamports = walletIndex >= 0 ? lamports(meta.postBalances, walletIndex) - lamports(meta.preBalances, walletIndex) : 0n;

  const tokenDeltas = new Map<string, WalletTokenDelta>();
  const add = (bs: RawTokenBalance[] | undefined, sign: 1n | -1n) => {
    for (const b of bs ?? []) {
      if (b?.owner !== wallet || typeof b.mint !== 'string') continue;
      const raw = toBigInt(b.uiTokenAmount?.amount);
      if (raw === null) continue;
      const prev = tokenDeltas.get(b.mint);
      const decimals = Number(b.uiTokenAmount?.decimals ?? prev?.decimals ?? 0);
      const programId = typeof b.programId === 'string' && b.programId ? b.programId : prev?.programId;
      tokenDeltas.set(b.mint, { raw: (prev?.raw ?? 0n) + sign * raw, decimals, ...(programId ? { programId } : {}) });
    }
  };
  add(meta.postTokenBalances, 1n);
  add(meta.preTokenBalances, -1n);

  const slot = toBigInt(outer.slot);
  const err = meta.err ?? null;
  return {
    signature,
    ...(slot !== null ? { slot: Number(slot) } : {}),
    err: err && typeof err === 'object' && Object.keys(err).length === 0 ? null : err,
    feePayer: keys[0] ?? null,
    solDeltaLamports,
    tokenDeltas,
    seenAtMs,
  };
}

function toBigInt(v: unknown): bigint | null {
  if (typeof v === 'bigint') return v;
  if (typeof v === 'number' && Number.isFinite(v)) return BigInt(Math.trunc(v));
  if (typeof v === 'string' && /^-?\d+$/.test(v)) return BigInt(v);
  return null;
}

function encodeKey(k: unknown, len: number): string | null {
  if (typeof k === 'string') return k;
  let bytes: Uint8Array | null = null;
  if (k instanceof Uint8Array) bytes = k;
  else if (Array.isArray(k)) bytes = Uint8Array.from(k as number[]);
  else if (k && typeof k === 'object' && 'data' in k && Array.isArray((k as { data: unknown }).data)) {
    bytes = Uint8Array.from((k as { data: number[] }).data);
  }
  return bytes && bytes.length === len ? base58Encode(bytes) : null;
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

function short(a: string): string {
  return `${a.slice(0, 4)}…${a.slice(-4)}`;
}
