import type { FeedGraduation } from '../core/types.ts';
import type { DetectionFeed, FeedActivity, FeedLiveness } from './feed.ts';
import type { RpcClient } from '../core/rpc.ts';
import { WSOL_MINT, PROGRAM_IDS } from '../core/constants.ts';
import { hasMigrateLog } from './migrateLog.ts';
import { registerSecret, logger } from '../core/logger.ts';
import { base58Encode } from '../core/base58.ts';

/**
 * LaserStream detection feed — the official Helius gRPC client
 * (`helius-laserstream`: native Rust bindings, automatic reconnect + slot
 * replay, zero data loss).
 *
 * Same detection contract used across all feeds: a `Migrate`/`MigrateV2`
 * instruction log yields the signature; the graduated mint is read inline from
 * the update's token balances (non-WSOL) — no RPC round trip — falling back to
 * a `getTransaction` lookup only when the balances are missing. Latency is
 * stamped at stream receipt.
 *
 * The subscription is narrowed with `accountRequired: [pumpFun, migration
 * authority]` so the stream carries migrations only instead of the whole
 * pump.fun firehose, and subscribes to slots so the ~400 ms slot ticks act as
 * the liveness heartbeat for the detector's watchdog.
 *
 * The SDK owns reconnection and replay internally (`replay: true`); the local
 * backoff loop only covers a failed initial subscribe and watchdog-forced
 * reconnects. Requires a Developer+ Helius plan (even on devnet); without an
 * endpoint this feed reports unhealthy once and stops, leaving the other
 * feeds to carry detection.
 */

const MINT_LOOKUP_RETRIES = 4;
const MINT_LOOKUP_INTERVAL_MS = 600;
/** LaserStream commitment for the earliest signal. */
const COMMITMENT_PROCESSED = 0;

export interface LaserstreamFeedOptions {
  /** LaserStream gRPC endpoint (e.g. https://laserstream-devnet-…helius-rpc.com). */
  endpoint: string;
  /** Helius API key, already resolved from its env var. */
  token?: string;
  /** RPC client used to recover the graduated mint from a migration signature. */
  rpc: RpcClient;
  pumpFunProgramId: string;
  /** pump.fun migration authority; when set, `accountRequired` narrows the stream to migrations. */
  migrationAuthority?: string | undefined;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  /** Test hook: replaces the dynamic `helius-laserstream` import. */
  subscribeFn?: SubscribeFn;
}

export type SubscribeFn = (
  config: { apiKey: string; endpoint: string; replay?: boolean },
  request: Record<string, unknown>,
  onData: (update: unknown) => void | Promise<void>,
  onError?: (error: unknown) => void | Promise<void>,
) => Promise<LaserstreamHandle>;

export class LaserstreamFeed implements DetectionFeed {
  readonly name = 'laserstream';

  private readonly endpoint: string;
  private readonly token: string | undefined;
  private readonly rpc: RpcClient;
  private readonly pumpFun: string;
  private readonly migrationAuthority: string | undefined;
  private readonly reconnectBaseMs: number;
  private readonly reconnectMaxMs: number;
  private readonly subscribeFn: SubscribeFn | undefined;
  private readonly log = logger.child({ mod: 'laserstream' });

  private handle: LaserstreamHandle | null = null;
  private stopped = false;
  private connecting = false;
  private attempts = 0;
  private reconnectTimer: NodeJS.Timeout | null = null;
  /** Bumped per subscribe; frames from a cancelled handle are ignored. */
  private generation = 0;
  /** Degrades to 'silence' if the server rejects the slots filter. */
  private livenessMode: FeedLiveness = 'slot';
  private slotsFilterEnabled = true;
  private seen = new Set<string>();

  private gradHandler: (g: FeedGraduation) => void = () => {};
  private healthHandler: (healthy: boolean, detail?: string) => void = () => {};
  private activityHandler: (a: FeedActivity) => void = () => {};

  constructor(opts: LaserstreamFeedOptions) {
    this.endpoint = opts.endpoint;
    this.token = opts.token;
    this.rpc = opts.rpc;
    this.pumpFun = opts.pumpFunProgramId;
    this.migrationAuthority = opts.migrationAuthority || undefined;
    this.reconnectBaseMs = opts.reconnectBaseMs ?? 500;
    this.reconnectMaxMs = opts.reconnectMaxMs ?? 30_000;
    this.subscribeFn = opts.subscribeFn;
    if (this.token) registerSecret(this.token);
    registerSecret(this.endpoint);
  }

  get liveness(): FeedLiveness {
    return this.livenessMode;
  }

  onGraduation(handler: (g: FeedGraduation) => void): void {
    this.gradHandler = handler;
  }
  onHealth(handler: (healthy: boolean, detail?: string) => void): void {
    this.healthHandler = handler;
  }
  onActivity(handler: (a: FeedActivity) => void): void {
    this.activityHandler = handler;
  }

  start(): void {
    this.stopped = false;
    void this.connect();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.generation++;
    try {
      this.handle?.cancel();
    } catch {
      /* ignore */
    }
    this.handle = null;
  }

  reconnect(reason: string): void {
    if (this.stopped || this.reconnectTimer || this.connecting || !this.handle) return;
    const handle = this.handle;
    this.handle = null;
    this.generation++;
    this.healthHandler(false, reason);
    this.log.warn('forcing reconnect', { reason });
    try {
      handle.cancel();
    } catch {
      /* ignore */
    }
    this.attempts = 0;
    this.scheduleReconnect();
  }

  private buildRequest(): Record<string, unknown> {
    return {
      accounts: {},
      // Slot ticks (~400 ms at processed) are the liveness heartbeat; with the
      // narrowed transaction filter the stream is otherwise quiet for minutes.
      slots: this.slotsFilterEnabled ? { heartbeat: { filterByCommitment: true } } : {},
      transactions: {
        pumpfun: {
          vote: false,
          failed: false,
          accountInclude: [this.pumpFun],
          accountExclude: [],
          // Every migration references the authority — this drops the pump.fun
          // firehose to migrations only. `replay: true` re-delivering a few
          // old migrations after a reconnect is harmless: the detector's
          // seenMints + maxStaleSlots drop them.
          accountRequired: this.migrationAuthority ? [this.pumpFun, this.migrationAuthority] : [],
        },
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
    if (this.stopped || this.connecting) return;

    if (!this.endpoint) {
      this.healthHandler(false, 'laserstream endpoint not configured (rpc.primaryGrpc) — running on other feeds');
      return;
    }

    let subscribe = this.subscribeFn;
    if (!subscribe) {
      let mod: LaserstreamModule;
      try {
        mod = (await import('helius-laserstream')) as unknown as LaserstreamModule;
      } catch {
        this.healthHandler(false, 'helius-laserstream not installed — falling back to other feeds');
        this.log.warn('LaserStream SDK missing — feed disabled, other feeds still active');
        return;
      }
      subscribe = (config, request, onData, onError) => mod.subscribe(config, request, onData, onError);
    }

    this.connecting = true;
    const gen = ++this.generation;
    try {
      const handle = await subscribe(
        { apiKey: this.token ?? '', endpoint: this.endpoint, replay: true },
        this.buildRequest(),
        (data: unknown) => {
          if (gen !== this.generation) return;
          this.healthHandler(true);
          this.handleUpdate(data, process.hrtime.bigint());
        },
        (err: unknown) => {
          if (gen !== this.generation) return;
          // The SDK reconnects + replays internally; surface the blip and let
          // the detector's grace window decide whether it is an outage.
          this.log.warn('laserstream stream error', { detail: describeError(err) });
          this.healthHandler(false, 'stream error (sdk reconnecting)');
        },
      );
      if (gen !== this.generation || this.stopped) {
        // stop()/reconnect() raced the subscribe — drop the orphan handle.
        try {
          handle.cancel();
        } catch {
          /* ignore */
        }
        return;
      }
      this.handle = handle;
      this.attempts = 0;
      this.seen.clear();
      this.log.info('laserstream subscribed to pump.fun transactions', {
        endpoint: '[redacted]',
        narrowed: Boolean(this.migrationAuthority),
        slotsHeartbeat: this.slotsFilterEnabled,
      });
      this.healthHandler(true);
    } catch (err) {
      const detail = describeError(err);
      this.log.warn('laserstream subscribe failed', { detail });
      this.healthHandler(false, 'subscribe failed');
      if (this.slotsFilterEnabled && /slot/i.test(detail)) {
        // Subscribe is all-or-nothing: if the plan rejects the slots filter,
        // retry without it and fall back to the absolute-silence bound.
        this.slotsFilterEnabled = false;
        this.livenessMode = 'silence';
        this.log.warn('slots filter rejected — retrying without it; liveness degrades to the absolute-silence bound');
      }
      this.scheduleReconnect();
    } finally {
      this.connecting = false;
    }
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer) return;
    const wait = Math.min(this.reconnectBaseMs * 2 ** this.attempts, this.reconnectMaxMs);
    this.attempts++;
    this.reconnectTimer = setTimeout(() => void this.connect(), wait);
  }

  private handleUpdate(data: unknown, receivedAtNs: bigint): void {
    const atMs = Date.now();
    if (data && typeof data === 'object' && 'ping' in data && (data as { ping: unknown }).ping) {
      // The SDK answers pongs internally; count it as activity only.
      this.activityHandler({ atMs, kind: 'data' });
      return;
    }
    const slotUpdate = (data as { slot?: { slot?: unknown } } | null)?.slot;
    if (slotUpdate && typeof slotUpdate === 'object') {
      const slot = toSlot(slotUpdate.slot);
      this.activityHandler(slot !== undefined ? { atMs, kind: 'slot', slot } : { atMs, kind: 'data' });
      return;
    }
    this.activityHandler({ atMs, kind: 'data' });

    const tx = extractTransaction(data);
    if (!tx || tx.err) return;
    if (!hasMigrateLog(tx.logs)) return;
    if (!tx.signature) return;
    if (this.seen.has(tx.signature)) return;
    this.seen.add(tx.signature);
    if (this.seen.size > 5000) this.seen.clear();

    // The update already carries the token balances: resolve the mint inline
    // (no getTransaction round trip), same as the Helius Atlas path.
    const mints = tx.mints.filter((m) => m !== WSOL_MINT && m !== PROGRAM_IDS.SYSTEM);
    if (mints.length === 1) {
      const grad: FeedGraduation = {
        mint: mints[0]!,
        feedSource: 'laserstream',
        receivedAtNs,
        venue: 'pumpswap',
        signature: tx.signature,
      };
      if (tx.slot !== undefined) grad.slot = tx.slot;
      this.gradHandler(grad);
      return;
    }
    if (mints.length > 1) {
      this.log.debug('ambiguous migrate mints — skipping (other feeds cover)', { signature: tx.signature });
      return;
    }
    void this.emitGraduation(tx.signature, receivedAtNs, tx.slot);
  }

  private async emitGraduation(signature: string, receivedAtNs: bigint, slot: number | undefined): Promise<void> {
    for (let attempt = 0; attempt < MINT_LOOKUP_RETRIES; attempt++) {
      try {
        const found = await this.rpc.getTransactionTokenMints(signature);
        const mints = found.mints.filter((m) => m !== WSOL_MINT && m !== PROGRAM_IDS.SYSTEM);
        if (mints.length === 1) {
          const grad: FeedGraduation = {
            mint: mints[0]!,
            feedSource: 'laserstream',
            receivedAtNs,
            venue: 'pumpswap',
            signature,
          };
          const s = slot ?? found.slot;
          if (s !== undefined) grad.slot = s;
          this.gradHandler(grad);
          return;
        }
        if (mints.length > 1) {
          this.log.debug('ambiguous migrate mints — skipping (other feeds cover)', { signature });
          return;
        }
      } catch (err) {
        this.log.debug('mint lookup attempt failed', { signature, attempt, err });
      }
      await delay(MINT_LOOKUP_INTERVAL_MS);
    }
    this.log.debug('could not resolve migrate mint within budget', { signature });
  }
}

interface LaserstreamHandle {
  id: string;
  cancel(): void;
  write(request: unknown): void | Promise<void>;
}

interface LaserstreamModule {
  subscribe(
    config: { apiKey: string; endpoint: string; replay?: boolean },
    request: Record<string, unknown>,
    onData: (update: unknown) => void | Promise<void>,
    onError?: (error: unknown) => void | Promise<void>,
  ): Promise<LaserstreamHandle>;
}

export interface ExtractedTx {
  signature: string | null;
  logs: string[];
  err: unknown;
  /** Distinct mints from pre/post token balances (unfiltered). */
  mints: string[];
  /** Slot of the update (the SDK decodes uint64 as a string). */
  slot: number | undefined;
}

/**
 * Pull signature + log messages + error + token mints + slot out of a
 * LaserStream transaction update, tolerating the nesting the client uses
 * (`transaction.transaction.{meta,signature}`) and encoding signatures
 * (bytes → base58) defensively.
 */
export function extractTransaction(data: unknown): ExtractedTx | null {
  const root = data as { transaction?: unknown } | null;
  const outer = root?.transaction as { transaction?: unknown; slot?: unknown } | undefined;
  if (!outer) return null;
  const inner = (outer.transaction as {
    signature?: unknown;
    meta?: TxMeta;
  }) ?? outer;

  const meta = inner.meta as TxMeta | undefined;
  const logs = Array.isArray(meta?.logMessages) ? (meta!.logMessages as string[]) : [];
  const balances = [...(meta?.preTokenBalances ?? []), ...(meta?.postTokenBalances ?? [])];
  const mints = [...new Set(balances.map((b) => b?.mint).filter((m): m is string => typeof m === 'string'))];
  return {
    signature: encodeSignature((inner as { signature?: unknown }).signature),
    logs,
    err: meta?.err ?? null,
    mints,
    slot: toSlot(outer.slot),
  };
}

interface TxMeta {
  err?: unknown;
  logMessages?: unknown;
  preTokenBalances?: Array<{ mint?: unknown }>;
  postTokenBalances?: Array<{ mint?: unknown }>;
}

/** uint64 arrives as string (SDK `longs: String`), number, or bigint. */
function toSlot(v: unknown): number | undefined {
  if (typeof v === 'number') return Number.isFinite(v) ? v : undefined;
  if (typeof v === 'bigint') return Number(v);
  if (typeof v === 'string' && v !== '') {
    const n = Number(v);
    return Number.isFinite(n) ? n : undefined;
  }
  return undefined;
}

function encodeSignature(sig: unknown): string | null {
  if (typeof sig === 'string') return sig;
  if (sig instanceof Uint8Array) return base58Encode(sig);
  if (Array.isArray(sig)) return base58Encode(Uint8Array.from(sig as number[]));
  if (sig && typeof sig === 'object' && 'data' in sig) {
    const d = (sig as { data: unknown }).data;
    if (Array.isArray(d)) return base58Encode(Uint8Array.from(d as number[]));
  }
  return null;
}

function describeError(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) return String((err as { message: unknown }).message);
  return String(err);
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
