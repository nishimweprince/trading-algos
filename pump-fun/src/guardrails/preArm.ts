import { PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { base58Encode } from '../core/base58.ts';
import { deriveAta } from '../core/ata.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../core/constants.ts';
import { logger, registerSecret } from '../core/logger.ts';
import type { RpcClient } from '../core/rpc.ts';
import { BONDING_CURVE_DISCRIMINATOR, deriveBondingCurvePda } from '../enrichment/curve.ts';
import type { RawAccountRead } from '../executor/swapState.ts';

/**
 * Pre-arm at bonding-curve completion (feat/fra-lean WS4b).
 *
 * A pump.fun curve completes on its last buy; the migration to PumpSwap
 * follows 0–3 slots later (measured 2026-10-04 on six graduations: two in the
 * same slot, four ~3 slots / ~1.2 s later). The fast screen used to spend one
 * getMultipleAccounts round trip (~24 ms p50, 93 ms p90) after the migration
 * to read the pool, mint and wallet accounts. Everything except the
 * pool itself exists before the migration, and the pool's address is a PDA of
 * the mint, so at completion this:
 *
 *  - prefetches the mint account and the wallet's WSOL/base ATAs (one RPC,
 *    off the hot path);
 *  - adds the canonical pool PDA to a LaserStream account subscription, so
 *    the migrate tx's pool write is pushed the slot it happens.
 *
 * At detection the fast reader peeks the armed state: if the pool account has
 * been pushed it builds the verdict and the buy state with no RPC at all;
 * otherwise it starts its normal read at once and races it against the push
 * (whichever lands first wins), so pre-arm can never make a read slower. Nothing here can make a buy happen — it only removes a
 * read from in front of one the fast path already decided.
 *
 * Not a pre-signed transaction: the pool account (its fee, creator and
 * virtual-reserve fields) does not exist until the migrate tx writes it, and
 * a synthesized one would be a guess. Building + signing from the pushed
 * state is the remaining ~3–14 ms.
 *
 * Completion is observed with an owner + memcmp account filter on pump.fun
 * curves (discriminator @0, complete=1 @48), so only completing curves are
 * streamed. Curve → mint comes from the launch feed (and the launches table
 * at boot); a completion whose launch was never seen is skipped (fast read
 * covers it).
 */

/** Curve layout: complete bool @48. */
const CURVE_COMPLETE_OFFSET = 48;
/** realSolReserves u64 @32: ~85 SOL at completion, drained to 0 by the migration. */
const CURVE_REAL_SOL_OFFSET = 32;
const COMMITMENT_PROCESSED = 0;

export interface PreArmConfig {
  enabled: boolean;
  /** Drop an armed pool this long after completion if no migration came. */
  ttlMs: number;
  /**
   * At detection, how long the pool push may still win the race against the
   * fast read's RPC call (which starts immediately). Past it, the read decides.
   */
  poolWaitMs: number;
  /** Cap on curve→mint map entries (oldest dropped). */
  maxTrackedLaunches: number;
}

export interface ArmedPool {
  mint: string;
  poolKey: string;
  curve: string;
  armedAtMs: number;
  /** Disarmed by the sweep after this (completion: ttlMs; racer fire: its own TTL). */
  expiresAtMs: number;
  pool: RawAccountRead | null;
  mintAccount: RawAccountRead | null;
  /**
   * Keyed by address: user WSOL ATA, user base ATAs (SPL + 2022), and the
   * pool's quote / base vaults once the migrate creates them (pushed).
   */
  accounts: Map<string, RawAccountRead | null>;
  /** Every address subscribed for this mint (pool, vaults, user ATAs). */
  watchKeys: string[];
  prefetched: boolean;
}

type SubscribeFn = (
  config: { apiKey: string; endpoint: string; replay?: boolean },
  request: Record<string, unknown>,
  onData: (update: unknown) => void | Promise<void>,
  onError?: (error: unknown) => void | Promise<void>,
) => Promise<{ cancel(): void; write(request: unknown): void | Promise<void> }>;

export interface PreArmerDeps {
  endpoint: string;
  token?: string | undefined;
  rpc: Pick<RpcClient, 'getMultipleAccountsBase64'>;
  /** Trading wallet; its ATAs are prefetched. */
  user?: string | undefined;
  config: PreArmConfig;
  now?: () => number;
  subscribeFn?: SubscribeFn;
}

interface PoolWaiter {
  resolve: () => void;
}

export class PreArmer {
  private readonly deps: PreArmerDeps;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'pre-arm' });
  private readonly curveToMint = new Map<string, string>();
  private readonly armed = new Map<string, ArmedPool>();
  /** Watched account address → mint it belongs to. */
  private readonly watched = new Map<string, string>();
  private readonly poolWaiters = new Map<string, PoolWaiter[]>();
  private handle: { cancel(): void; write(request: unknown): void | Promise<void> } | null = null;
  private connecting = false;
  private stopped = false;
  private sweepTimer: NodeJS.Timeout | null = null;
  private lastKey = '';
  readonly stats = { completions: 0, alreadyMigrated: 0, unknownCurve: 0, armed: 0, armedOnFire: 0, poolPushes: 0, hits: 0, misses: 0, expired: 0 };

  constructor(deps: PreArmerDeps) {
    this.deps = deps;
    this.now = deps.now ?? Date.now;
    if (deps.token) registerSecret(deps.token);
    if (deps.endpoint) registerSecret(deps.endpoint);
  }

  /** A pump.fun launch: remember its curve so a completion can name the mint. */
  noteLaunch(mint: string): void {
    const curve = deriveBondingCurvePda(mint);
    if (!curve) return;
    this.curveToMint.set(curve, mint);
    if (this.curveToMint.size > this.deps.config.maxTrackedLaunches) {
      const oldest = this.curveToMint.keys().next().value;
      if (oldest !== undefined) this.curveToMint.delete(oldest);
    }
  }

  start(): void {
    if (!this.deps.config.enabled) return;
    this.stopped = false;
    this.sweepTimer = setInterval(() => this.sweep(), 5_000);
    this.sweepTimer.unref?.();
    if (!this.deps.endpoint) {
      this.log.warn('pre-arm enabled but rpc.primaryGrpc is blank — every buy takes the fast read');
      return;
    }
    void this.connect();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.sweepTimer) clearInterval(this.sweepTimer);
    this.sweepTimer = null;
    try {
      this.handle?.cancel();
    } catch {
      /* ignore */
    }
    this.handle = null;
    for (const ws of this.poolWaiters.values()) for (const w of ws) w.resolve();
    this.poolWaiters.clear();
  }

  /** The armed state for a mint, without waiting or consuming it. */
  peek(mint: string): ArmedPool | null {
    return this.armed.get(mint) ?? null;
  }

  /**
   * Resolves true as soon as the mint's pool account has been pushed (at
   * once when it already has), false after `ms`. The fast reader races this
   * against its own RPC read.
   */
  waitForPool(mint: string, ms: number): Promise<boolean> {
    const a = this.armed.get(mint);
    if (!a) return Promise.resolve(false);
    if (a.pool) return Promise.resolve(true);
    return new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => resolve(false), ms);
      timer.unref?.();
      const list = this.poolWaiters.get(mint) ?? [];
      list.push({ resolve: () => { clearTimeout(timer); resolve(true); } });
      this.poolWaiters.set(mint, list);
    });
  }

  /** Done with a migrating mint: count the outcome and drop its subscription. */
  release(mint: string, servedFromPush: boolean): void {
    if (!this.armed.has(mint)) return;
    if (servedFromPush) this.stats.hits++;
    else this.stats.misses++;
    this.disarm(mint);
  }

  /** Test / replay hook: route one SubscribeUpdate. */
  ingest(data: unknown): void {
    const upd = (data as { account?: { account?: { pubkey?: unknown; data?: unknown; owner?: unknown; lamports?: unknown } } } | null)?.account?.account;
    if (!upd) return;
    const address = encodeKey(upd.pubkey);
    const bytes = toBytes(upd.data);
    const owner = encodeKey(upd.owner);
    if (!address || !bytes) return;
    const mint = this.watched.get(address);
    if (mint) {
      this.onWatchedAccount(mint, address, bytes, owner, Number(upd.lamports ?? 0));
      return;
    }
    if (owner === PROGRAM_IDS.PUMP_FUN && bytes.length > CURVE_COMPLETE_OFFSET && bytes[CURVE_COMPLETE_OFFSET] === 1) {
      this.onCurveComplete(address, bytes);
    }
  }

  private onCurveComplete(curve: string, bytes: Uint8Array): void {
    this.stats.completions++;
    // Completed curves keep receiving writes after their migration (live probe
    // 2026-10-04: 50 'complete' updates in 3 min, 3 migrations). A migrated
    // curve is drained; only one still holding its SOL is about to migrate.
    if (bytes.length >= CURVE_REAL_SOL_OFFSET + 8) {
      const realSol = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).readBigUInt64LE(CURVE_REAL_SOL_OFFSET);
      if (realSol === 0n) {
        this.stats.alreadyMigrated++;
        return;
      }
    }
    const mint = this.curveToMint.get(curve);
    if (!mint) {
      this.stats.unknownCurve++;
      return;
    }
    this.arm(mint, curve, this.deps.config.ttlMs);
  }

  /**
   * Arm a mint before its curve completes — the trader's racer gate calls
   * this when the racer fires (usually 3+ slots before the migrate, where
   * completion mostly lands with the migrate itself). Idempotent; a fire
   * also extends an existing arm's expiry.
   */
  armMint(mint: string, ttlMs: number): void {
    const existing = this.armed.get(mint);
    if (existing) {
      existing.expiresAtMs = Math.max(existing.expiresAtMs, this.now() + ttlMs);
      return;
    }
    const curve = deriveBondingCurvePda(mint);
    if (!curve) return;
    if (this.arm(mint, curve, ttlMs)) this.stats.armedOnFire++;
  }

  private arm(mint: string, curve: string, ttlMs: number): boolean {
    if (this.armed.has(mint)) return false;
    let poolKey: string;
    try {
      poolKey = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
    } catch {
      return false;
    }
    const now = this.now();
    // Vaults are pushed too: without them a pre-armed verdict could only
    // price from the migrate tx's post balances, blind to a bundled buy that
    // pumped the pool later in the same slot (H7's ceiling). User ATAs are
    // pushed so a long (racer-fire) arm never serves a stale wallet account.
    const user = this.deps.user;
    const watchKeys = [
      poolKey,
      deriveAta(poolKey, WSOL_MINT, false),
      deriveAta(poolKey, mint, false),
      deriveAta(poolKey, mint, true),
      ...(user ? [deriveAta(user, WSOL_MINT, false), deriveAta(user, mint, false), deriveAta(user, mint, true)] : []),
    ];
    const a: ArmedPool = {
      mint, poolKey, curve, armedAtMs: now, expiresAtMs: now + ttlMs,
      pool: null, mintAccount: null, accounts: new Map(), watchKeys, prefetched: false,
    };
    this.armed.set(mint, a);
    this.stats.armed++;
    for (const k of watchKeys) this.watched.set(k, mint);
    void this.resubscribe();
    void this.prefetch(a);
    return true;
  }

  private onWatchedAccount(mint: string, address: string, bytes: Uint8Array, owner: string | null, lamports: number): void {
    const a = this.armed.get(mint);
    if (!a) return;
    const read: RawAccountRead = { data: Buffer.from(bytes).toString('base64'), owner: owner ?? '', lamports: Number.isFinite(lamports) ? lamports : 0 };
    if (address === a.poolKey) {
      if (owner !== PROGRAM_IDS.PUMP_SWAP) return;
      a.pool = read;
      this.stats.poolPushes++;
      for (const w of this.poolWaiters.get(mint) ?? []) w.resolve();
      this.poolWaiters.delete(mint);
      return;
    }
    a.accounts.set(address, read);
  }

  /** Accounts that exist before the migration: mint and wallet ATAs. */
  private async prefetch(a: ArmedPool): Promise<void> {
    const user = this.deps.user;
    const addrs = [
      a.mint,
      ...(user ? [deriveAta(user, WSOL_MINT, false), deriveAta(user, a.mint, false), deriveAta(user, a.mint, true)] : []),
    ];
    try {
      const res = await this.deps.rpc.getMultipleAccountsBase64(addrs, 'processed');
      a.mintAccount = res[0] ?? null;
      addrs.slice(1).forEach((addr, i) => {
        // A push that already arrived is fresher than this read.
        if (!a.accounts.has(addr)) a.accounts.set(addr, res[i + 1] ?? null);
      });
      a.prefetched = true;
    } catch (err) {
      this.log.debug('pre-arm prefetch failed — fast read will cover', { mint: a.mint, err });
    }
  }

  private disarm(mint: string): void {
    const a = this.armed.get(mint);
    if (!a) return;
    this.armed.delete(mint);
    for (const k of a.watchKeys) this.watched.delete(k);
    void this.resubscribe();
  }

  private sweeps = 0;

  private sweep(): void {
    if (++this.sweeps % 60 === 0) this.log.info('pre-arm stats', { ...this.stats, armedNow: this.armed.size, launchesMapped: this.curveToMint.size });
    const now = this.now();
    for (const [mint, a] of this.armed) {
      if (a.expiresAtMs < now) {
        this.stats.expired++;
        this.disarm(mint);
      }
    }
  }

  private request(): Record<string, unknown> {
    const watched = [...this.watched.keys()];
    return {
      accounts: {
        completions: {
          account: [],
          owner: [PROGRAM_IDS.PUMP_FUN],
          filters: [
            { memcmp: { offset: 0, base58: base58Encode(Buffer.from(BONDING_CURVE_DISCRIMINATOR, 'hex')) } },
            { memcmp: { offset: CURVE_COMPLETE_OFFSET, base58: base58Encode(Uint8Array.of(1)) } },
          ],
        },
        ...(watched.length ? { armed: { account: watched, owner: [], filters: [] } } : {}),
      },
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
    if (!this.handle) return;
    const key = [...this.watched.keys()].sort().join(',');
    if (key === this.lastKey) return;
    this.lastKey = key;
    try {
      await this.handle.write(this.request());
    } catch (err) {
      this.lastKey = '';
      this.log.warn('pre-arm subscription update failed', { err });
    }
  }

  private async connect(): Promise<void> {
    if (this.stopped || this.connecting || this.handle) return;
    this.connecting = true;
    try {
      const subscribe = this.deps.subscribeFn ?? (await loadSdk());
      if (!subscribe) {
        this.log.warn('helius-laserstream SDK missing — pre-arm disabled');
        return;
      }
      this.lastKey = [...this.watched.keys()].sort().join(',');
      const handle = await subscribe(
        { apiKey: this.deps.token ?? '', endpoint: this.deps.endpoint, replay: false },
        this.request(),
        (data) => this.ingest(data),
        (err) => {
          this.lastKey = '';
          this.log.warn('pre-arm stream error (sdk reconnecting)', { detail: describeError(err) });
          void this.resubscribe();
        },
      );
      if (this.stopped) {
        handle.cancel();
        return;
      }
      this.handle = handle;
      this.log.info('pre-arm subscribed to pump.fun curve completions');
      this.lastKey = '';
      await this.resubscribe();
    } catch (err) {
      this.log.warn('pre-arm subscribe failed — fast read carries every buy', { detail: describeError(err) });
    } finally {
      this.connecting = false;
    }
  }
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
