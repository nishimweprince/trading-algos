import type { FeedGraduation } from '../core/types.ts';
import type { DetectionFeed } from './feed.ts';
import type { RpcClient } from '../core/rpc.ts';
import { WSOL_MINT, PROGRAM_IDS } from '../core/constants.ts';
import { registerSecret, logger } from '../core/logger.ts';
import { base58Encode } from '../core/base58.ts';

/**
 * LaserStream detection feed — the official Helius gRPC client
 * (`helius-laserstream`: native Rust bindings, automatic reconnect + slot
 * replay, zero data loss).
 *
 * Same detection contract used across all feeds: a `Migrate` instruction log
 * yields the signature; the graduated mint is recovered index-independently
 * from the transaction's token balances (non-WSOL), with a short retry for the
 * processed→confirmed lag. Latency is stamped at stream receipt.
 *
 * The SDK owns reconnection and replay internally (`replay: true`), so no
 * backoff loop is needed here: errors surface as unhealthy, data surfaces as
 * healthy, and the detector's grace window debounces the rest. Requires a
 * Developer+ Helius plan (even on devnet); without an endpoint this feed
 * reports unhealthy once and stops, leaving the other feeds to carry
 * detection.
 */

// Anchored to end-of-line: pump.fun also emits an unrelated
// `Instruction: MigrateBondingCurveCreator` (creator fee-sharing config
// migration, replayable on old/already-graduated mints) that an unanchored
// match would misread as a fresh bonding-curve-to-AMM graduation.
const MIGRATE_LOG = /Instruction:\s*Migrate\s*$/im;
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
}

export class LaserstreamFeed implements DetectionFeed {
  readonly name = 'laserstream';

  private readonly endpoint: string;
  private readonly token: string | undefined;
  private readonly rpc: RpcClient;
  private readonly pumpFun: string;
  private readonly log = logger.child({ mod: 'laserstream' });

  private handle: LaserstreamHandle | null = null;
  private stopped = false;
  private seen = new Set<string>();

  private gradHandler: (g: FeedGraduation) => void = () => {};
  private healthHandler: (healthy: boolean, detail?: string) => void = () => {};

  constructor(opts: LaserstreamFeedOptions) {
    this.endpoint = opts.endpoint;
    this.token = opts.token;
    this.rpc = opts.rpc;
    this.pumpFun = opts.pumpFunProgramId;
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
    try {
      this.handle?.cancel();
    } catch {
      /* ignore */
    }
    this.handle = null;
  }

  private async connect(): Promise<void> {
    if (this.stopped) return;

    if (!this.endpoint) {
      this.healthHandler(false, 'laserstream endpoint not configured (rpc.primaryGrpc) — running on other feeds');
      return;
    }

    let mod: LaserstreamModule | null;
    try {
      mod = (await import('helius-laserstream')) as unknown as LaserstreamModule;
    } catch {
      this.healthHandler(false, 'helius-laserstream not installed — falling back to other feeds');
      this.log.warn('LaserStream SDK missing — feed disabled, other feeds still active');
      return;
    }

    try {
      const handle = await mod.subscribe(
        { apiKey: this.token ?? '', endpoint: this.endpoint, replay: true },
        {
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
          commitment: COMMITMENT_PROCESSED,
        },
        (data: unknown) => {
          this.healthHandler(true);
          this.handleUpdate(data, process.hrtime.bigint());
        },
        (err: unknown) => {
          // The SDK reconnects + replays internally; surface the blip and let
          // the detector's grace window decide whether it is an outage.
          this.log.warn('laserstream stream error', { detail: describeError(err) });
          this.healthHandler(false, 'stream error (sdk reconnecting)');
        },
      );
      this.handle = handle;
      this.seen.clear();
      this.log.info('laserstream subscribed to pump.fun transactions', { endpoint: '[redacted]' });
      this.healthHandler(true);
    } catch (err) {
      this.log.warn('laserstream subscribe failed', { detail: describeError(err) });
      this.healthHandler(false, 'subscribe failed');
    }
  }

  private handleUpdate(data: unknown, receivedAtNs: bigint): void {
    if (data && typeof data === 'object' && 'ping' in data && (data as { ping: unknown }).ping) {
      // The SDK answers pongs internally; this is a no-op guard for forward
      // compatibility with raw ping updates.
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
            feedSource: 'laserstream',
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

interface ExtractedTx {
  signature: string | null;
  logs: string[];
  err: unknown;
}

/**
 * Pull signature + log messages + error out of a LaserStream transaction
 * update, tolerating the nesting the client uses
 * (`transaction.transaction.{meta,signature}`) and encoding signatures
 * (bytes → base58) defensively.
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
