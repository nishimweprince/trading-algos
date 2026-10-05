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
 *   vaults   — base vaults armed for realistic-entry pricing (slotEntry.ts),
 *              rewritten onto the live stream as the watch set changes.
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
import { extractMigrateTx, type MigrateTx } from './migrateTx.ts';

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
  onWatched?: (address: string, data: Uint8Array, slot: number | null) => void;
  onMigrateTx: (tx: MigrateTx) => void;
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
  /** Force a reconnect after this long with no frame at all. */
  silenceMs?: number;
  subscribeFn?: SubscribeFn;
}

export function buildRacerRequest(bandValues: number, watched: readonly string[] = []): Record<string, unknown> {
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
export function routeUpdate(data: unknown, h: RacerStreamHandlers): 'account' | 'watched' | 'migrate' | 'launch' | 'other' {
  const acct = (data as { account?: { slot?: unknown; account?: { pubkey?: unknown; data?: unknown; owner?: unknown; txnSignature?: unknown } } } | null)?.account;
  if (acct?.account) {
    const a = acct.account;
    const curve = encodeKey(a.pubkey);
    const bytes = toBytes(a.data);
    if (!curve || !bytes) return 'other';
    if (h.isWatched?.(curve)) {
      const s = Number(acct.slot);
      h.onWatched?.(curve, bytes, Number.isFinite(s) ? s : null);
      return 'watched';
    }
    const owner = encodeKey(a.owner) ?? PROGRAM_IDS.PUMP_FUN;
    const reserves = decodeBondingCurve(Buffer.from(bytes).toString('base64'), owner, curve);
    if (!reserves) return 'other';
    const sigBytes = toBytes(a.txnSignature);
    const slot = Number(acct.slot);
    h.onCurve({ curve, reserves, slot: Number.isFinite(slot) ? slot : null, signature: sigBytes && sigBytes.length ? base58Encode(sigBytes) : null });
    return 'account';
  }
  if ((data as { transaction?: unknown } | null)?.transaction) {
    const mig = extractMigrateTx(data);
    if (mig) {
      h.onMigrateTx(mig);
      return 'migrate';
    }
    const tx = extractTransaction(data);
    if (tx && !tx.err && hasCreateLog(tx.logs)) {
      const pre = new Set(tx.preMints);
      const created = [...new Set(tx.postBalances.map((b) => b.mint))].filter((m) => m !== WSOL_MINT && !pre.has(m));
      for (const m of created) h.onLaunch(m);
      return 'launch';
    }
  }
  return 'other';
}

export class RacerStream {
  private readonly opts: RacerStreamOptions;
  private readonly handlers: RacerStreamHandlers;
  private readonly log = logger.child({ mod: 'racer-stream' });
  private handle: Handle | null = null;
  private watched: string[] = [];
  private stopped = false;
  private connecting = false;
  private lastFrameMs = 0;
  private watchdog: NodeJS.Timeout | null = null;
  private retryMs = 1_000;
  readonly stats = { frames: 0, curves: 0, vaultWrites: 0, migrates: 0, launches: 0, reconnects: 0, watchWrites: 0 };

  constructor(opts: RacerStreamOptions, handlers: RacerStreamHandlers) {
    this.opts = opts;
    this.handlers = handlers;
    if (opts.token) registerSecret(opts.token);
    if (opts.endpoint) registerSecret(opts.endpoint);
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
    const h = this.handle;
    if (!h?.write) return;
    this.stats.watchWrites++;
    Promise.resolve(h.write(buildRacerRequest(this.opts.bandValues, this.watched))).catch((err) =>
      this.log.warn('racer stream watch update failed', { detail: describeError(err) }),
    );
  }

  private reconnect(reason: string): void {
    this.log.warn('racer stream reconnecting', { reason });
    this.stats.reconnects++;
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
        buildRacerRequest(this.opts.bandValues, this.watched),
        (data) => this.onData(data),
        (err) => this.log.warn('racer stream error (sdk reconnecting)', { detail: describeError(err) }),
      );
      if (this.stopped) {
        handle.cancel();
        return;
      }
      this.handle = handle;
      this.lastFrameMs = Date.now();
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
    this.lastFrameMs = Date.now();
    this.stats.frames++;
    try {
      const kind = routeUpdate(data, this.handlers);
      if (kind === 'account') this.stats.curves++;
      else if (kind === 'watched') this.stats.vaultWrites++;
      else if (kind === 'migrate') this.stats.migrates++;
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
