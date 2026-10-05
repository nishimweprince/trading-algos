/**
 * The racer's own LaserStream subscription (one gRPC stream, PROCESSED):
 *
 *   band<v>  — pump.fun bonding curves whose realTokenReserves (u64 LE @24)
 *              has bytes 30–31 = 0 and byte 29 = v, for v in [0, bandValues).
 *              That is fewer than bandValues·2^40 raw tokens left, ≈5.2 SOL
 *              to complete at bandValues=12 (verified live 2026-10-05: max
 *              streamed cost 5.10 SOL). Complete curves (real = 0) land in
 *              band0, so the complete flip arrives here too.
 *   migrate  — every tx that references the migration authority, failed
 *              ones included: the winner and every losing attempt.
 *   creates  — pump.fun creations, so a curve update can name its mint.
 *   slots    — liveness heartbeat (~400 ms).
 *   vaults   — base vaults for delayed entries plus base/quote vaults retained
 *              for path exits, rewritten as the watch set changes.
 *   poolBuys — buys involving watched pools, including failed transactions;
 *              this is an on-chain-only landing proxy, not an off-chain drop feed.
 *
 * No polling anywhere: the band filter replaces launch-time admission (G1)
 * and the 2 s RPC tick (G6).
 */
import { base58Encode } from '../core/base58.ts';
import { PROGRAM_IDS, PUMP_FUN_MIGRATION_AUTHORITY, PUMP_FUN_MINT_AUTHORITY, WSOL_MINT } from '../core/constants.ts';
import { logger, registerSecret } from '../core/logger.ts';
import { BONDING_CURVE_DISCRIMINATOR, decodeBondingCurve, type CurveReserves } from '../enrichment/curve.ts';
import { extractTransaction } from '../detector/laserstream.ts';
import { hasCreateLog } from '../detector/migrateLog.ts';
import { extractMigrateTx, instructionTrace, type MigrateTx } from './migrateTx.ts';

/** realTokenReserves u64 LE @24; bytes 29..31 are its top three. */
export const REAL_TOKEN_TOP_OFFSET = 29;
const COMMITMENT_PROCESSED = 0;

export interface CurveUpdate {
  curve: string;
  reserves: CurveReserves;
  slot: number | null;
  /** The tx that wrote it (base58), when the stream carries it. */
  signature: string | null;
}

export interface RacerStreamHandlers {
  onCurve: (u: CurveUpdate) => void;
  /** Watched (non-curve) account writes: armed pool base vaults. */
  isWatched?: (address: string) => boolean;
  onWatched?: (address: string, data: Uint8Array, slot: number | null, signature: string | null, owner: string | null) => void;
  /** `receivedAtNs` = process.hrtime.bigint() when the frame arrived (the hot-path clock start). */
  onMigrateTx: (tx: MigrateTx, receivedAtNs?: bigint) => void;
  isWatchedPool?: (pool: string) => boolean;
  onPoolBuy?: (attempt: { signature: string; poolAddress: string; slot: number; ok: boolean; buyerOwner: string | null; quoteVolumeSol: number | null; networkFeeSol: number | null }) => void;
  onLaunch: (mint: string) => void;
}

type Handle = { cancel(): void; write?(request: unknown): void | Promise<void> };
export type SubscribeFn = (
  config: { apiKey: string; endpoint: string; replay?: boolean },
  request: Record<string, unknown>,
  onData: (update: unknown) => void | Promise<void>,
  onError?: (error: unknown) => void | Promise<void>,
) => Promise<Handle>;

export interface RacerStreamOptions {
  endpoint: string;
  token: string;
  bandValues: number;
  /**
   * Called whenever account writes may have been dropped: our reconnect, an
   * SDK stream error, a failed watch update — and, independently of what the
   * SDK reports (it can reconnect internally, and we subscribe without
   * replay), a jump in the slot heartbeat or a silence between frames.
   */
  onGap?: (reason: string) => void;
  /** Frame silence that counts as a gap (heartbeat ticks ~400 ms). Default 3000. */
  gapSilenceMs?: number;
  /** Heartbeat slot advance that counts as a gap (skipped leader slots are normal). Default 12. */
  gapSlotJump?: number;
  now?: () => number;
  /** Force a reconnect after this long with no frame at all. */
  silenceMs?: number;
  subscribeFn?: SubscribeFn;
}

export function buildRacerRequest(bandValues: number, watched: readonly string[] = [], watchedPools: readonly string[] = []): Record<string, unknown> {
  const disc = base58Encode(Buffer.from(BONDING_CURVE_DISCRIMINATOR, 'hex'));
  const accounts: Record<string, unknown> = {};
  for (let v = 0; v < bandValues; v++) {
    accounts[`band${v}`] = {
      account: [],
      owner: [PROGRAM_IDS.PUMP_FUN],
      filters: [
        { memcmp: { offset: 0, base58: disc } },
        { memcmp: { offset: REAL_TOKEN_TOP_OFFSET, base58: base58Encode(Uint8Array.of(v, 0, 0)) } },
      ],
    };
  }
  if (watched.length) accounts.vaults = { account: [...watched], owner: [], filters: [] };
  return {
    accounts,
    slots: { heartbeat: { filterByCommitment: true } },
    transactions: {
      // `failed` unset = landed AND failed attempts.
      migrate: { vote: false, accountInclude: [], accountExclude: [], accountRequired: [PROGRAM_IDS.PUMP_FUN, PUMP_FUN_MIGRATION_AUTHORITY] },
      creates: { vote: false, failed: false, accountInclude: [], accountExclude: [], accountRequired: [PROGRAM_IDS.PUMP_FUN, PUMP_FUN_MINT_AUTHORITY] },
      ...(watchedPools.length ? { poolBuys: { vote: false, accountInclude: [...watchedPools], accountExclude: [], accountRequired: [PROGRAM_IDS.PUMP_SWAP] } } : {}),
    },
    transactionsStatus: {},
    blocks: {},
    blocksMeta: {},
    entry: {},
    accountsDataSlice: [],
    commitment: COMMITMENT_PROCESSED,
  };
}

/** Route one SubscribeUpdate to the handlers. Exported for tests / replay. */
export function routeUpdate(
  data: unknown,
  h: RacerStreamHandlers,
  receivedAtNs?: bigint,
): 'account' | 'watched' | 'migrate' | 'pool-buy' | 'launch' | 'other' {
  const acct = (data as { account?: { slot?: unknown; account?: { pubkey?: unknown; data?: unknown; owner?: unknown; txnSignature?: unknown } } } | null)?.account;
  if (acct?.account) {
    const a = acct.account;
    const curve = encodeKey(a.pubkey);
    const bytes = toBytes(a.data);
    if (!curve) return 'other';
    if (h.isWatched?.(curve)) {
      if (bytes === null) return 'other'; // malformed/unknown encoding is not proof of a closed vault
      const s = Number(acct.slot);
      const sig = toBytes(a.txnSignature);
      h.onWatched?.(curve, bytes, Number.isFinite(s) ? s : null, sig && sig.length ? base58Encode(sig) : null, encodeKey(a.owner));
      return 'watched';
    }
    if (!bytes) return 'other';
    const owner = encodeKey(a.owner) ?? PROGRAM_IDS.PUMP_FUN;
    const reserves = decodeBondingCurve(Buffer.from(bytes).toString('base64'), owner, curve);
    if (!reserves) return 'other';
    const sigBytes = toBytes(a.txnSignature);
    const slot = Number(acct.slot);
    h.onCurve({ curve, reserves, slot: Number.isFinite(slot) ? slot : null, signature: sigBytes && sigBytes.length ? base58Encode(sigBytes) : null });
    return 'account';
  }
  if ((data as { transaction?: unknown } | null)?.transaction) {
    const tx = extractTransaction(data);
    let poolBuy = false;
    if (tx?.signature && tx.slot !== undefined && instructionTrace(tx.logs).some((i) => i.program === PROGRAM_IDS.PUMP_SWAP && /^Buy/.test(i.name))) {
      const outer = (data as { transaction?: { transaction?: { transaction?: { message?: { accountKeys?: unknown[] } }; meta?: {
        loadedWritableAddresses?: unknown[]; loadedReadonlyAddresses?: unknown[];
        preTokenBalances?: unknown[]; postTokenBalances?: unknown[];
        fee?: unknown;
      } } } }).transaction;
      const inner = outer?.transaction;
      const keys = [...(inner?.transaction?.message?.accountKeys ?? []), ...(inner?.meta?.loadedWritableAddresses ?? []), ...(inner?.meta?.loadedReadonlyAddresses ?? [])];
      const poolAddress = keys.map(encodeKey).find((k): k is string => !!k && !!h.isWatchedPool?.(k));
      if (poolAddress) {
        const details = poolBuyDetails(inner?.meta, poolAddress);
        const fee = Number(inner?.meta?.fee);
        h.onPoolBuy?.({ signature: tx.signature, poolAddress, slot: tx.slot, ok: !tx.err,
          networkFeeSol: Number.isFinite(fee) && fee >= 0 ? fee / 1e9 : null, ...details });
        poolBuy = true;
      }
    }
    const mig = extractMigrateTx(data);
    if (mig) {
      h.onMigrateTx(mig, receivedAtNs);
      return 'migrate';
    }
    if (poolBuy) return 'pool-buy';
    if (tx && !tx.err && hasCreateLog(tx.logs)) {
      const pre = new Set(tx.preMints);
      const created = [...new Set(tx.postBalances.map((b) => b.mint))].filter((m) => m !== WSOL_MINT && !pre.has(m));
      for (const m of created) h.onLaunch(m);
      return 'launch';
    }
  }
  return 'other';
}

type TokenBalance = { mint?: unknown; owner?: unknown; uiTokenAmount?: { amount?: unknown } };
function poolBuyDetails(meta: { preTokenBalances?: unknown[]; postTokenBalances?: unknown[] } | undefined, pool: string): {
  buyerOwner: string | null; quoteVolumeSol: number | null;
} {
  const pre = (meta?.preTokenBalances ?? []) as TokenBalance[];
  const post = (meta?.postTokenBalances ?? []) as TokenBalance[];
  const amount = (b: TokenBalance): bigint => { try { return BigInt(String(b.uiTokenAmount?.amount ?? '0')); } catch { return 0n; } };
  const sum = (bs: TokenBalance[], pred: (b: TokenBalance) => boolean): bigint => bs.filter(pred).reduce((n, b) => n + amount(b), 0n);
  const quote = (bs: TokenBalance[]) => sum(bs, (b) => b.mint === WSOL_MINT && b.owner === pool);
  const volume = quote(post) - quote(pre);
  const byOwner = (bs: TokenBalance[]) => {
    const out = new Map<string, bigint>();
    for (const b of bs) if (typeof b.owner === 'string' && typeof b.mint === 'string' && b.mint !== WSOL_MINT && b.owner !== pool) {
      const key = `${b.mint}:${b.owner}`;
      out.set(key, (out.get(key) ?? 0n) + amount(b));
    }
    return out;
  };
  const before = byOwner(pre);
  const after = byOwner(post);
  let buyerOwner: string | null = null;
  let largestGain = 0n;
  for (const [key, balance] of after) {
    const gain = balance - (before.get(key) ?? 0n);
    if (gain > largestGain) { largestGain = gain; buyerOwner = key.split(':')[1] ?? null; }
  }
  return { buyerOwner,
    quoteVolumeSol: volume > 0n ? Number(volume) / 1e9 : null };
}

export class RacerStream {
  private readonly opts: RacerStreamOptions;
  private readonly handlers: RacerStreamHandlers;
  private readonly log = logger.child({ mod: 'racer-stream' });
  private handle: Handle | null = null;
  private watched: string[] = [];
  private watchedPools: string[] = [];
  private stopped = false;
  private connecting = false;
  private lastFrameMs = 0;
  private watchdog: NodeJS.Timeout | null = null;
  private retryMs = 1_000;
  private lastHeartbeatSlot: number | null = null;
  readonly stats = { frames: 0, curves: 0, vaultWrites: 0, migrates: 0, poolBuys: 0, launches: 0, reconnects: 0, watchWrites: 0, gaps: 0 };

  constructor(opts: RacerStreamOptions, handlers: RacerStreamHandlers) {
    this.opts = opts;
    this.handlers = handlers;
    if (opts.token) registerSecret(opts.token);
    if (opts.endpoint) registerSecret(opts.endpoint);
  }

  /** Latest processed slot from the heartbeat; null before the first tick. */
  get heartbeatSlot(): number | null {
    return this.lastHeartbeatSlot;
  }

  get connected(): boolean {
    return this.handle !== null;
  }

  get lastFrameAtMs(): number {
    return this.lastFrameMs;
  }

  start(): void {
    this.stopped = false;
    const silence = this.opts.silenceMs ?? 30_000;
    this.watchdog = setInterval(() => {
      if (this.handle && Date.now() - this.lastFrameMs > silence) this.reconnect(`no frame for ${silence} ms`);
    }, 5_000);
    this.watchdog.unref?.();
    void this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.watchdog) clearInterval(this.watchdog);
    this.watchdog = null;
    try {
      this.handle?.cancel();
    } catch {
      /* ignore */
    }
    this.handle = null;
  }

  /** Replace the watched-account set on the live subscription (and on every reconnect). */
  setWatched(addresses: readonly string[]): void {
    this.watched = [...addresses];
    this.writeWatches();
  }

  setWatchedPools(addresses: readonly string[]): void {
    this.watchedPools = [...addresses];
    this.writeWatches();
  }

  /** Change account and transaction filters in one subscription write. */
  setWatches(vaults: readonly string[], pools: readonly string[]): void {
    this.watched = [...vaults];
    this.watchedPools = [...pools];
    this.writeWatches();
  }

  private writeWatches(): void {
    const h = this.handle;
    if (!h?.write) return;
    this.stats.watchWrites++;
    Promise.resolve(h.write(buildRacerRequest(this.opts.bandValues, this.watched, this.watchedPools))).catch((err) => {
      this.log.warn('racer stream watch update failed', { detail: describeError(err) });
      this.opts.onGap?.('watch update failed');
    });
  }

  private reconnect(reason: string): void {
    this.log.warn('racer stream reconnecting', { reason });
    this.stats.reconnects++;
    this.opts.onGap?.(`reconnect: ${reason}`);
    try {
      this.handle?.cancel();
    } catch {
      /* ignore */
    }
    this.handle = null;
    void this.connect();
  }

  private async connect(): Promise<void> {
    if (this.stopped || this.connecting || this.handle) return;
    this.connecting = true;
    try {
      const subscribe = this.opts.subscribeFn ?? (await loadSdk());
      if (!subscribe) throw new Error('helius-laserstream SDK missing');
      const handle = await subscribe(
        { apiKey: this.opts.token, endpoint: this.opts.endpoint, replay: false },
        buildRacerRequest(this.opts.bandValues, this.watched, this.watchedPools),
        (data) => this.onData(data),
        (err) => {
          this.log.warn('racer stream error (sdk reconnecting)', { detail: describeError(err) });
          this.opts.onGap?.('stream error');
        },
      );
      if (this.stopped) {
        handle.cancel();
        return;
      }
      this.handle = handle;
      this.lastFrameMs = (this.opts.now ?? Date.now)();
      this.retryMs = 1_000;
      this.log.info('racer stream subscribed', { bandValues: this.opts.bandValues });
    } catch (err) {
      this.log.warn('racer stream subscribe failed — retrying', { detail: describeError(err), retryMs: this.retryMs });
      const t = setTimeout(() => void this.connect(), this.retryMs);
      t.unref?.();
      this.retryMs = Math.min(this.retryMs * 2, 30_000);
    } finally {
      this.connecting = false;
    }
  }

  private onData(data: unknown): void {
    const receivedAtNs = process.hrtime.bigint();
    const now = (this.opts.now ?? Date.now)();
    const silence = this.opts.gapSilenceMs ?? 3_000;
    if (this.lastFrameMs > 0 && now - this.lastFrameMs > silence) {
      this.stats.gaps++;
      this.opts.onGap?.(`frame silence ${now - this.lastFrameMs} ms`);
    }
    this.lastFrameMs = now;
    this.stats.frames++;
    const hb = Number((data as { slot?: { slot?: unknown } } | null)?.slot?.slot);
    if (Number.isFinite(hb) && hb > 0) {
      const prev = this.lastHeartbeatSlot;
      if (prev !== null && hb > prev + (this.opts.gapSlotJump ?? 12)) {
        this.stats.gaps++;
        this.opts.onGap?.(`slot jump ${prev} -> ${hb}`);
      }
      if (prev === null || hb > prev) this.lastHeartbeatSlot = hb;
    }
    try {
      const kind = routeUpdate(data, this.handlers, receivedAtNs);
      if (kind === 'account') this.stats.curves++;
      else if (kind === 'watched') this.stats.vaultWrites++;
      else if (kind === 'migrate') this.stats.migrates++;
      else if (kind === 'pool-buy') this.stats.poolBuys++;
      else if (kind === 'launch') this.stats.launches++;
    } catch (err) {
      this.log.warn('racer stream handler threw', { err });
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
