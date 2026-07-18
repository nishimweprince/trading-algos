import type { FeedGraduation } from '../core/types.ts';
import type { DetectionFeed } from './feed.ts';
import type { RpcClient } from '../core/rpc.ts';
import { WSOL_MINT, PROGRAM_IDS } from '../core/constants.ts';
import { base58Encode } from '../core/base58.ts';
import { registerSecret, logger } from '../core/logger.ts';

/**
 * Yellowstone gRPC / LaserStream detection feed (Section 4.1 — the low-latency
 * primary signal on paid Helius infra).
 *
 * PLUG-AND-PLAY BY DESIGN. The `@triton-one/yellowstone-grpc` client is an
 * OPTIONAL dependency, loaded lazily at runtime. This feed is purely additive:
 * the detector always runs it ALONGSIDE the free PumpPortal + Helius-WS feeds
 * and keeps whichever sees a graduation first (cross-feed dedupe), so gRPC never
 * replaces the free path — it just wins the race when it's faster.
 *
 * Graceful fallback, three ways, none of which can break the bot:
 *  1. Package not installed (free tier)      → logs an actionable note, reports
 *     unhealthy once, and stops. The free feeds carry detection unchanged.
 *  2. Endpoint/token not configured          → same as above.
 *  3. Stream errors/drops after connecting    → reconnects with backoff; while
 *     it's down the free feeds cover, and dedupe reconciles on recovery.
 *
 * So an operator can leave `detector.grpcEnabled: true` on permanently: before
 * buying premium it's a harmless no-op that falls back; the moment the gRPC
 * endpoint + token are set (and the optional dep is present) it activates on the
 * next restart with zero code changes.
 *
 * Detection mirrors the Helius-WS feed exactly: a `Migrate` instruction log
 * yields the signature; the graduated mint is recovered index-independently from
 * the transaction's token balances (non-WSOL), with a short retry for the
 * processed→confirmed lag. Latency is stamped at stream receipt.
 */

/** Kept as a runtime variable so the optional dep is never resolved at build. */
const YELLOWSTONE_MODULE = '@triton-one/yellowstone-grpc';

const MIGRATE_LOG = /Instruction:\s*Migrate/i;
const MINT_LOOKUP_RETRIES = 4;
const MINT_LOOKUP_INTERVAL_MS = 600;

export interface GrpcFeedOptions {
  /** Yellowstone/LaserStream gRPC endpoint (e.g. https://…helius…:2053). */
  endpoint: string;
  /** x-token auth, already resolved from its env var (optional for some providers). */
  token?: string;
  /** RPC client used to recover the graduated mint from a migration signature. */
  rpc: RpcClient;
  pumpFunProgramId: string;
  reconnectBaseMs: number;
  reconnectMaxMs: number;
}

export class GrpcFeed implements DetectionFeed {
  readonly name = 'grpc';

  private readonly endpoint: string;
  private readonly token: string | undefined;
  private readonly rpc: RpcClient;
  private readonly pumpFun: string;
  private readonly reconnectBaseMs: number;
  private readonly reconnectMaxMs: number;
  private readonly log = logger.child({ mod: 'grpc' });

  private stream: GrpcStream | null = null;
  private stopped = false;
  private attempts = 0;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private seen = new Set<string>();

  private gradHandler: (g: FeedGraduation) => void = () => {};
  private healthHandler: (healthy: boolean, detail?: string) => void = () => {};

  constructor(opts: GrpcFeedOptions) {
    this.endpoint = opts.endpoint;
    this.token = opts.token;
    this.rpc = opts.rpc;
    this.pumpFun = opts.pumpFunProgramId;
    this.reconnectBaseMs = opts.reconnectBaseMs;
    this.reconnectMaxMs = opts.reconnectMaxMs;
    if (this.token) registerSecret(this.token);
    registerSecret(this.endpoint);
  }

  onGraduation(handler: (g: FeedGraduation) => void): void {
    this.gradHandler = handler;
  }
  onHealth(handler: (healthy: boolean, detail?: string) => void): void {
    this.healthHandler = handler;
  }

  start(): void {
    this.stopped = false;
    void this.connect();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.teardownStream();
  }

  private teardownStream(): void {
    try {
      this.stream?.end?.();
      this.stream?.cancel?.();
    } catch {
      /* ignore */
    }
    this.stream = null;
  }

  private async connect(): Promise<void> {
    if (this.stopped) return;

    if (!this.endpoint) {
      this.healthHandler(
        false,
        'grpc endpoint not configured (rpc.primaryGrpc) — running on free feeds',
      );
      return; // no endpoint: no point reconnecting.
    }

    // Optional dependency: absent on the free tier. Load it lazily so the build
    // and a free-tier install never require it.
    let mod: GrpcModule | null;
    try {
      mod = (await import(YELLOWSTONE_MODULE)) as unknown as GrpcModule;
    } catch {
      this.healthHandler(
        false,
        `${YELLOWSTONE_MODULE} not installed — falling back to free feeds. ` +
          `Install it (it is an optionalDependency) to activate the paid gRPC feed.`,
      );
      this.log.warn('gRPC optional dependency missing — feed disabled, free feeds still active', {
        module: YELLOWSTONE_MODULE,
      });
      return; // a missing package won't appear on reconnect; stop cleanly.
    }

    try {
      const Client = (mod.default ?? mod) as GrpcClientCtor;
      const client = new Client(this.endpoint, this.token, undefined);
      const stream = await client.subscribe();
      this.stream = stream;

      stream.on('data', (data: unknown) => {
        const receivedAtNs = process.hrtime.bigint();
        this.handleUpdate(data, receivedAtNs);
      });
      stream.on('error', (err: unknown) => {
        this.log.warn('gRPC stream error', { detail: describeError(err) });
        this.healthHandler(false, 'stream error');
        this.scheduleReconnect();
      });
      stream.on('end', () => this.onStreamClosed('end'));
      stream.on('close', () => this.onStreamClosed('close'));

      await this.writeSubscribeRequest(stream, mod);

      this.attempts = 0;
      this.seen.clear();
      this.log.info('gRPC subscribed to pump.fun transactions', { endpoint: '[redacted]' });
      this.healthHandler(true);
    } catch (err) {
      this.log.warn('gRPC connect failed — reconnecting', { detail: describeError(err) });
      this.healthHandler(false, 'connect failed');
      this.scheduleReconnect();
    }
  }

  private onStreamClosed(kind: string): void {
    if (this.stopped) return;
    this.log.warn('gRPC stream closed — reconnecting', { kind });
    this.healthHandler(false, 'closed');
    this.scheduleReconnect();
  }

  private async writeSubscribeRequest(stream: GrpcStream, mod: GrpcModule): Promise<void> {
    // PROCESSED for the earliest possible signal; the detector re-confirms
    // on-chain before emitting, so an unconfirmed head is fine here.
    const processed = mod.CommitmentLevel?.PROCESSED ?? 0;
    const request = {
      accounts: {},
      slots: {},
      transactions: {
        pumpfun: {
          vote: false,
          failed: false,
          accountInclude: [this.pumpFun],
          accountExclude: [],
          accountRequired: [],
        },
      },
      transactionsStatus: {},
      blocks: {},
      blocksMeta: {},
      entry: {},
      accountsDataSlice: [],
      commitment: processed,
    };
    await new Promise<void>((resolve, reject) => {
      stream.write(request, (err: unknown) => (err ? reject(err) : resolve()));
    });
  }

  private handleUpdate(data: unknown, receivedAtNs: bigint): void {
    // Keepalive: the server sends periodic pings and expects a ping back, or it
    // drops the stream. Respond and move on.
    if (data && typeof data === 'object' && 'ping' in data && (data as { ping: unknown }).ping) {
      try {
        this.stream?.write({ ping: { id: 1 } }, () => {});
      } catch {
        /* a failed pong surfaces as a stream error → reconnect */
      }
      return;
    }

    const tx = extractTransaction(data);
    if (!tx || tx.err) return;
    if (!tx.logs.some((l) => MIGRATE_LOG.test(l))) return;
    if (!tx.signature) return;
    if (this.seen.has(tx.signature)) return;
    this.seen.add(tx.signature);
    if (this.seen.size > 5000) this.seen.clear();

    void this.emitGraduation(tx.signature, receivedAtNs);
  }

  private async emitGraduation(signature: string, receivedAtNs: bigint): Promise<void> {
    for (let attempt = 0; attempt < MINT_LOOKUP_RETRIES; attempt++) {
      try {
        const mints = (await this.rpc.getTransactionTokenMints(signature)).filter(
          (m) => m !== WSOL_MINT && m !== PROGRAM_IDS.SYSTEM,
        );
        if (mints.length === 1) {
          const grad: FeedGraduation = {
            mint: mints[0]!,
            feedSource: 'grpc',
            receivedAtNs,
            venue: 'pumpswap',
            signature,
          };
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

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer) return;
    this.teardownStream();
    const wait = Math.min(this.reconnectBaseMs * 2 ** this.attempts, this.reconnectMaxMs);
    this.attempts++;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, wait);
  }
}

/** Minimal structural typing over the optional gRPC client (loosely typed on purpose). */
interface GrpcStream {
  on(event: string, cb: (arg: unknown) => void): void;
  write(request: unknown, cb: (err: unknown) => void): void;
  end?: () => void;
  cancel?: () => void;
}
interface GrpcClientCtor {
  new (endpoint: string, token: string | undefined, opts: unknown): {
    subscribe(): Promise<GrpcStream>;
  };
}
interface GrpcModule {
  default?: GrpcClientCtor;
  CommitmentLevel?: { PROCESSED?: number };
}

interface ExtractedTx {
  signature: string | null;
  logs: string[];
  err: unknown;
}

/**
 * Pull signature + log messages + error out of a Yellowstone transaction update,
 * tolerating the nesting the client uses (`transaction.transaction.{meta,signature}`)
 * and encoding signatures (bytes → base58) defensively.
 */
export function extractTransaction(data: unknown): ExtractedTx | null {
  const root = data as { transaction?: unknown } | null;
  const outer = root?.transaction as { transaction?: unknown } | undefined;
  if (!outer) return null;
  const inner = (outer.transaction as {
    signature?: unknown;
    meta?: { err?: unknown; logMessages?: unknown };
  }) ?? outer;

  const meta = inner.meta as { err?: unknown; logMessages?: unknown } | undefined;
  const logs = Array.isArray(meta?.logMessages) ? (meta!.logMessages as string[]) : [];
  return {
    signature: encodeSignature((inner as { signature?: unknown }).signature),
    logs,
    err: meta?.err ?? null,
  };
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

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function describeError(ev: unknown): string {
  if (ev instanceof Error) return ev.message;
  if (ev && typeof ev === 'object' && 'message' in ev) return String((ev as { message: unknown }).message);
  return 'unknown';
}
