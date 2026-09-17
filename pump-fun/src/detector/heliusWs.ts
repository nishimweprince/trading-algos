import type { FeedGraduation } from '../core/types.ts';
import type { DetectionFeed, FeedActivity, FeedLiveness } from './feed.ts';
import type { RpcClient } from '../core/rpc.ts';
import { WSOL_MINT, PROGRAM_IDS, MAX_SUPPORTED_TX_VERSION } from '../core/constants.ts';
import { hasMigrateLog } from './migrateLog.ts';
import { registerSecret, logger } from '../core/logger.ts';

/**
 * Helius WebSocket detection feed (Section 4.1). Subscribes directly to the
 * pump.fun program over `logsSubscribe` using the existing Helius API key (the
 * wss endpoint is derived from the HTTP RPC URL — no new credential). This is a
 * direct on-chain subscription, typically faster/more reliable than PumpPortal's
 * relay; PumpPortal stays enabled as a cross-feed fallback.
 *
 * Detection: a migration log (`Program log: Instruction: Migrate[V2]`) yields the
 * signature; the mint is recovered index-independently from the transaction's
 * token balances (filtering out WSOL) with a short retry for the
 * processed→confirmed lag. Detection latency is stamped at log receipt, before
 * the mint lookup.
 *
 * Two transport modes:
 *   - `logs` (default, all plans): `logsSubscribe` on the pump.fun program,
 *     then a `getTransaction` lookup to recover the mint.
 *   - `atlas` (Developer+ plan): `transactionSubscribe` with an
 *     `accountInclude` filter on pump.fun, `jsonParsed`/`full` details. The
 *     notification already carries log messages + token balances, so the
 *     mint is extracted inline with NO `getTransaction` round trip. When the
 *     server rejects the subscription (e.g. free plan: "not available"), the
 *     feed automatically falls back to `logsSubscribe` on the same socket.
 *
 * Liveness: after the subscription ack the feed adds `slotSubscribe` on the
 * same socket; the ~400 ms slot ticks are reported as activity so the
 * detector's watchdog can force a reconnect when a half-open socket goes
 * silent (health alone only flips on `close`, which never comes).
 */

interface WSLike {
  send(data: string): void;
  close(): void;
  addEventListener(type: string, cb: (ev: { data?: unknown; code?: number }) => void): void;
}
type WSCtor = new (url: string) => WSLike;

const MINT_LOOKUP_RETRIES = 4;
const MINT_LOOKUP_INTERVAL_MS = 600;

export interface HeliusWsOptions {
  rpc: RpcClient;
  /** HTTP RPC URL (with api-key); the wss endpoint is derived from it. */
  httpUrl: string;
  pumpFunProgramId: string;
  reconnectBaseMs: number;
  reconnectMaxMs: number;
  /**
   * Try Atlas `transactionSubscribe` first (Developer+ plan), falling back to
   * `logsSubscribe` when the server rejects it. Default false (all plans).
   */
  atlasEnabled?: boolean;
  /**
   * pump.fun migration authority; when set, the Atlas filter adds
   * `accountRequired: [pumpFun, authority]` so only migrations are streamed.
   */
  migrationAuthority?: string | undefined;
}

export class HeliusWsFeed implements DetectionFeed {
  readonly name = 'helius-ws';
  private readonly rpc: RpcClient;
  private readonly wssUrl: string;
  private readonly pumpFun: string;
  private readonly migrationAuthority: string | undefined;
  private readonly reconnectBaseMs: number;
  private readonly reconnectMaxMs: number;
  private readonly atlasEnabled: boolean;
  private readonly log = logger.child({ mod: 'helius-ws' });

  private useAtlas = false;
  private fellBackToLogs = false;
  /** Degrades to 'silence' if the server rejects slotSubscribe. */
  private livenessMode: FeedLiveness = 'slot';

  private ws: WSLike | null = null;
  private stopped = false;
  private attempts = 0;
  private reconnectTimer: NodeJS.Timeout | null = null;
  /** Bumped per connection; stale sockets' events are ignored (see reconnect). */
  private generation = 0;
  private seen = new Set<string>(); // signatures handled this connection (cheap de-dupe)

  private gradHandler: (g: FeedGraduation) => void = () => {};
  private healthHandler: (healthy: boolean, detail?: string) => void = () => {};
  private activityHandler: (a: FeedActivity) => void = () => {};

  constructor(opts: HeliusWsOptions) {
    this.rpc = opts.rpc;
    this.wssUrl = opts.httpUrl.replace(/^http/, 'ws');
    this.pumpFun = opts.pumpFunProgramId;
    this.migrationAuthority = opts.migrationAuthority || undefined;
    this.reconnectBaseMs = opts.reconnectBaseMs;
    this.reconnectMaxMs = opts.reconnectMaxMs;
    this.atlasEnabled = opts.atlasEnabled ?? false;
    registerSecret(this.wssUrl);
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
    this.connect();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.generation++;
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
    this.ws = null;
  }

  reconnect(reason: string): void {
    if (this.stopped || this.reconnectTimer || !this.ws) return;
    const ws = this.ws;
    // Abandon the socket BEFORE closing it: a half-open socket may never emit
    // `close`, and a fake/synchronous one would otherwise schedule a second
    // reconnect from the stale close handler.
    this.ws = null;
    this.generation++;
    this.healthHandler(false, reason);
    this.log.warn('forcing reconnect', { reason });
    try {
      ws.close();
    } catch {
      /* ignore */
    }
    this.attempts = 0;
    this.scheduleReconnect();
  }

  private connect(): void {
    this.reconnectTimer = null;
    const Ctor = (globalThis as unknown as { WebSocket?: WSCtor }).WebSocket;
    if (!Ctor) {
      this.log.error('global WebSocket unavailable — Node 22+ required');
      this.healthHandler(false, 'no WebSocket');
      return;
    }
    let ws: WSLike;
    try {
      ws = new Ctor(this.wssUrl);
    } catch (err) {
      this.log.error('failed to construct WebSocket', { err });
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    const gen = ++this.generation;

    ws.addEventListener('open', () => {
      if (gen !== this.generation) return;
      this.attempts = 0;
      this.seen.clear();
      this.useAtlas = false;
      this.fellBackToLogs = false;
      this.livenessMode = 'slot';
      if (this.atlasEnabled) {
        this.sendAtlasSubscribe(ws);
      } else {
        this.sendLogsSubscribe(ws, 1);
      }
    });

    ws.addEventListener('message', (ev) => {
      if (gen !== this.generation) return;
      const receivedAtNs = process.hrtime.bigint();
      this.handleMessage(ev.data, receivedAtNs);
    });

    ws.addEventListener('close', () => {
      if (gen !== this.generation) return;
      this.healthHandler(false, 'closed');
      if (!this.stopped) {
        this.log.warn('connection closed — reconnecting');
        this.scheduleReconnect();
      }
    });

    ws.addEventListener('error', (ev) => {
      if (gen !== this.generation) return;
      this.log.warn('websocket error', { detail: describeError(ev) });
    });
  }

  private sendLogsSubscribe(ws: WSLike, id: number): void {
    ws.send(
      JSON.stringify({
        jsonrpc: '2.0',
        id,
        method: 'logsSubscribe',
        params: [{ mentions: [this.pumpFun] }, { commitment: 'processed' }],
      }),
    );
  }

  private sendAtlasSubscribe(ws: WSLike): void {
    // accountRequired narrows the pump.fun firehose (~4 Mbit/s) to migrations
    // only: every Migrate/MigrateV2 tx references the migration authority.
    const filter = {
      vote: false,
      failed: false,
      accountInclude: [this.pumpFun],
      ...(this.migrationAuthority ? { accountRequired: [this.pumpFun, this.migrationAuthority] } : {}),
    };
    ws.send(
      JSON.stringify({
        jsonrpc: '2.0',
        id: ATLAS_SUB_ID,
        method: 'transactionSubscribe',
        params: [
          filter,
          {
            commitment: 'processed',
            encoding: 'jsonParsed',
            transactionDetails: 'full',
            showRewards: false,
            maxSupportedTransactionVersion: MAX_SUPPORTED_TX_VERSION,
          },
        ],
      }),
    );
  }

  /** Slot ticks (~400 ms) double as the liveness heartbeat for this socket. */
  private sendSlotSubscribe(ws: WSLike): void {
    ws.send(JSON.stringify({ jsonrpc: '2.0', id: SLOT_SUB_ID, method: 'slotSubscribe' }));
  }

  private handleMessage(data: unknown, receivedAtNs: bigint): void {
    let msg: WsInbound;
    try {
      msg = JSON.parse(typeof data === 'string' ? data : String(data)) as WsInbound;
    } catch {
      return;
    }
    const atMs = Date.now();
    if (msg.method === 'slotNotification') {
      const slot = msg.params?.result?.slot;
      this.activityHandler(
        typeof slot === 'number' ? { atMs, kind: 'slot', slot } : { atMs, kind: 'data' },
      );
      return;
    }
    this.activityHandler({ atMs, kind: 'data' });
    // Subscription replies carry an id.
    if (typeof msg.id === 'number') {
      this.handleReply(msg);
      return;
    }
    const result = msg.params?.result;
    if (!result) return;
    // logsSubscribe notifications keep their existing shape in either mode.
    if (result.value && typeof result.value === 'object') {
      const slot = result.context?.slot;
      this.handleLogsValue(
        result.value as { signature?: string; logs?: string[]; err?: unknown },
        receivedAtNs,
        typeof slot === 'number' ? slot : undefined,
      );
      return;
    }
    // Anything else with transaction content is an Atlas notification.
    if (this.useAtlas) this.handleAtlasResult(result, receivedAtNs);
  }

  private handleReply(msg: WsInbound): void {
    if (msg.id === SLOT_SUB_ID) {
      if (msg.error) {
        this.livenessMode = 'silence';
        this.log.warn('slotSubscribe rejected — liveness degrades to the absolute-silence bound', {
          detail: describeError(msg.error),
        });
        return;
      }
      this.log.debug('subscribed to slots (liveness heartbeat)', { subscription: msg.result });
      return;
    }
    if (msg.id === ATLAS_SUB_ID) {
      if (msg.error) {
        // Atlas unavailable (e.g. free plan) — fall back to logsSubscribe on
        // the same socket. One attempt per connection; the next reconnect
        // retries Atlas first again in case the plan changed.
        this.fellBackToLogs = true;
        this.useAtlas = false;
        this.log.warn('transactionSubscribe rejected — falling back to logsSubscribe', {
          detail: describeError(msg.error),
        });
        if (this.ws) this.sendLogsSubscribe(this.ws, LOGS_FALLBACK_SUB_ID);
        return;
      }
      this.useAtlas = true;
      this.log.info('subscribed to pump.fun transactions (atlas)', {
        subscription: msg.result,
        narrowed: Boolean(this.migrationAuthority),
      });
      if (this.ws) this.sendSlotSubscribe(this.ws);
      this.healthHandler(true);
      return;
    }
    this.log.info('subscribed to pump.fun logs', { subscription: msg.result });
    if (this.fellBackToLogs) {
      this.log.info('atlas fallback active — logsSubscribe carrying detection');
    }
    if (this.ws) this.sendSlotSubscribe(this.ws);
    this.healthHandler(true);
  }

  private handleLogsValue(
    value: { signature?: string; logs?: string[]; err?: unknown },
    receivedAtNs: bigint,
    slot: number | undefined,
  ): void {
    if (!value || value.err || !value.signature || !value.logs) return;
    if (!hasMigrateLog(value.logs)) return;

    const signature = value.signature;
    if (this.seen.has(signature)) return;
    this.seen.add(signature);
    if (this.seen.size > 5000) this.seen.clear(); // bound memory

    void this.emitGraduation(signature, receivedAtNs, slot);
  }

  private async emitGraduation(signature: string, receivedAtNs: bigint, slot: number | undefined): Promise<void> {
    // Recover the graduated mint from the tx's token balances, retrying past the
    // processed→confirmed lag. The token mint is the non-WSOL one.
    for (let attempt = 0; attempt < MINT_LOOKUP_RETRIES; attempt++) {
      try {
        const found = await this.rpc.getTransactionTokenMints(signature);
        const mints = found.mints.filter((m) => m !== WSOL_MINT && m !== PROGRAM_IDS.SYSTEM);
        if (mints.length === 1) {
          const grad: FeedGraduation = {
            mint: mints[0]!,
            feedSource: 'helius-ws',
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
          this.log.debug('ambiguous migrate mints — skipping (fallback feed covers)', { signature, mints });
          return;
        }
      } catch (err) {
        this.log.debug('mint lookup attempt failed', { signature, attempt, err });
      }
      await delay(MINT_LOOKUP_INTERVAL_MS);
    }
    this.log.debug('could not resolve migrate mint within budget', { signature });
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer) return;
    const wait = Math.min(this.reconnectBaseMs * 2 ** this.attempts, this.reconnectMaxMs);
    this.attempts++;
    this.reconnectTimer = setTimeout(() => this.connect(), wait);
  }

  /**
   * Atlas `transactionSubscribe` notification. The payload already carries the
   * logs and the token balances, so a Migrate yields the mint inline with no
   * `getTransaction` round trip. When the balances are absent (unexpected
   * shape), degrade to the logs-mode lookup rather than dropping the signal.
   */
  private handleAtlasResult(result: AtlasResult, receivedAtNs: bigint): void {
    const tx = extractAtlasTx(result);
    if (!tx || tx.err) return;
    if (!hasMigrateLog(tx.logs)) return;
    if (!tx.signature || this.seen.has(tx.signature)) return;
    this.seen.add(tx.signature);
    if (this.seen.size > 5000) this.seen.clear();

    const mints = tx.mints.filter((m) => m !== WSOL_MINT && m !== PROGRAM_IDS.SYSTEM);
    if (mints.length === 1) {
      const grad: FeedGraduation = {
        mint: mints[0]!,
        feedSource: 'helius-ws',
        receivedAtNs,
        venue: 'pumpswap',
        signature: tx.signature,
      };
      if (tx.slot !== undefined) grad.slot = tx.slot;
      this.gradHandler(grad);
      return;
    }
    if (mints.length > 1) {
      this.log.debug('ambiguous migrate mints — skipping (fallback feed covers)', { signature: tx.signature });
      return;
    }
    void this.emitGraduation(tx.signature, receivedAtNs, tx.slot);
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Subscription request ids on one socket (atlas first, logs as fallback, slots as heartbeat). */
const ATLAS_SUB_ID = 1;
const LOGS_FALLBACK_SUB_ID = 2;
const SLOT_SUB_ID = 3;

interface WsInbound {
  id?: number;
  method?: string;
  result?: unknown;
  error?: unknown;
  params?: { result?: AtlasResult & { value?: unknown; context?: { slot?: unknown } } };
}

interface AtlasResult {
  signature?: unknown;
  slot?: unknown;
  transaction?: {
    signatures?: unknown;
    signature?: unknown;
    meta?: AtlasMeta;
    transaction?: { meta?: AtlasMeta; signature?: unknown };
  };
  meta?: AtlasMeta;
}

interface AtlasMeta {
  err?: unknown;
  logMessages?: unknown;
  preTokenBalances?: Array<{ mint?: string }>;
  postTokenBalances?: Array<{ mint?: string }>;
}

/**
 * Pull signature + logs + token mints out of an Atlas transaction
 * notification, tolerating the nesting variants Helius may send
 * (`result.transaction.meta` vs `result.transaction.transaction.meta`,
 * string vs string[] signature).
 */
export function extractAtlasTx(result: AtlasResult): {
  signature: string | null;
  logs: string[];
  err: unknown;
  mints: string[];
  slot: number | undefined;
} | null {
  const tx = result.transaction;
  const metas = [tx?.meta, tx?.transaction?.meta, result.meta].filter(
    (m): m is AtlasMeta => Boolean(m),
  );
  const meta = metas[0];
  if (!meta && !tx) return null;
  const logs = Array.isArray(meta?.logMessages) ? (meta!.logMessages as string[]) : [];
  const balances = [...(meta?.preTokenBalances ?? []), ...(meta?.postTokenBalances ?? [])];
  const mints = [...new Set(balances.map((b) => b.mint).filter((m): m is string => typeof m === 'string'))];
  return {
    signature: extractAtlasSignature(result.signature ?? tx?.signature ?? tx?.signatures ?? tx?.transaction?.signature),
    logs,
    err: meta?.err ?? null,
    mints,
    slot: typeof result.slot === 'number' ? result.slot : undefined,
  };
}

function extractAtlasSignature(sig: unknown): string | null {
  if (typeof sig === 'string') return sig;
  if (Array.isArray(sig) && typeof sig[0] === 'string') return sig[0] as string;
  return null;
}

function describeError(ev: unknown): string {
  if (ev && typeof ev === 'object' && 'message' in ev) return String((ev as { message: unknown }).message);
  return 'unknown';
}
