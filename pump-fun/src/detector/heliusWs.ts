import type { FeedGraduation } from '../core/types.ts';
import type { DetectionFeed } from './feed.ts';
import type { RpcClient } from '../core/rpc.ts';
import { WSOL_MINT, PROGRAM_IDS } from '../core/constants.ts';
import { registerSecret, logger } from '../core/logger.ts';

/**
 * Helius WebSocket detection feed (Section 4.1). Subscribes directly to the
 * pump.fun program over `logsSubscribe` using the existing Helius API key (the
 * wss endpoint is derived from the HTTP RPC URL — no new credential). This is a
 * direct on-chain subscription, typically faster/more reliable than PumpPortal's
 * relay; PumpPortal stays enabled as a cross-feed fallback.
 *
 * Detection: a migration log (`Program log: Instruction: Migrate`) yields the
 * signature; the mint is recovered index-independently from the transaction's
 * token balances (filtering out WSOL) with a short retry for the
 * processed→confirmed lag. Detection latency is stamped at log receipt, before
 * the mint lookup.
 *
 * (Helius's enhanced `transactionSubscribe` needs the separate Atlas endpoint;
 * the standard endpoint accepts it but never delivers, so we use logsSubscribe.)
 */

interface WSLike {
  send(data: string): void;
  close(): void;
  addEventListener(type: string, cb: (ev: { data?: unknown; code?: number }) => void): void;
}
type WSCtor = new (url: string) => WSLike;

const MIGRATE_LOG = /Instruction:\s*Migrate/i;
const MINT_LOOKUP_RETRIES = 4;
const MINT_LOOKUP_INTERVAL_MS = 600;

export interface HeliusWsOptions {
  rpc: RpcClient;
  /** HTTP RPC URL (with api-key); the wss endpoint is derived from it. */
  httpUrl: string;
  pumpFunProgramId: string;
  reconnectBaseMs: number;
  reconnectMaxMs: number;
}

export class HeliusWsFeed implements DetectionFeed {
  readonly name = 'helius-ws';

  private readonly rpc: RpcClient;
  private readonly wssUrl: string;
  private readonly pumpFun: string;
  private readonly reconnectBaseMs: number;
  private readonly reconnectMaxMs: number;
  private readonly log = logger.child({ mod: 'helius-ws' });

  private ws: WSLike | null = null;
  private stopped = false;
  private attempts = 0;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private seen = new Set<string>(); // signatures handled this connection (cheap de-dupe)

  private gradHandler: (g: FeedGraduation) => void = () => {};
  private healthHandler: (healthy: boolean, detail?: string) => void = () => {};

  constructor(opts: HeliusWsOptions) {
    this.rpc = opts.rpc;
    this.wssUrl = opts.httpUrl.replace(/^http/, 'ws');
    this.pumpFun = opts.pumpFunProgramId;
    this.reconnectBaseMs = opts.reconnectBaseMs;
    this.reconnectMaxMs = opts.reconnectMaxMs;
    registerSecret(this.wssUrl);
  }

  onGraduation(handler: (g: FeedGraduation) => void): void {
    this.gradHandler = handler;
  }
  onHealth(handler: (healthy: boolean, detail?: string) => void): void {
    this.healthHandler = handler;
  }

  start(): void {
    this.stopped = false;
    this.connect();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    try {
      this.ws?.close();
    } catch {
      /* ignore */
    }
    this.ws = null;
  }

  private connect(): void {
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

    ws.addEventListener('open', () => {
      this.attempts = 0;
      this.seen.clear();
      ws.send(
        JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'logsSubscribe',
          params: [{ mentions: [this.pumpFun] }, { commitment: 'processed' }],
        }),
      );
    });

    ws.addEventListener('message', (ev) => {
      const receivedAtNs = process.hrtime.bigint();
      this.handleMessage(ev.data, receivedAtNs);
    });

    ws.addEventListener('close', () => {
      this.healthHandler(false, 'closed');
      if (!this.stopped) {
        this.log.warn('connection closed — reconnecting');
        this.scheduleReconnect();
      }
    });

    ws.addEventListener('error', (ev) => {
      this.log.warn('websocket error', { detail: describeError(ev) });
    });
  }

  private handleMessage(data: unknown, receivedAtNs: bigint): void {
    let msg: {
      id?: number;
      result?: unknown;
      params?: { result?: { value?: { signature?: string; logs?: string[]; err?: unknown } } };
    };
    try {
      msg = JSON.parse(typeof data === 'string' ? data : String(data));
    } catch {
      return;
    }
    if (msg.id === 1) {
      this.log.info('subscribed to pump.fun logs', { subscription: msg.result });
      this.healthHandler(true);
      return;
    }
    const value = msg.params?.result?.value;
    if (!value || value.err || !value.signature || !value.logs) return;
    if (!value.logs.some((l) => MIGRATE_LOG.test(l))) return;

    const signature = value.signature;
    if (this.seen.has(signature)) return;
    this.seen.add(signature);
    if (this.seen.size > 5000) this.seen.clear(); // bound memory

    void this.emitGraduation(signature, receivedAtNs);
  }

  private async emitGraduation(signature: string, receivedAtNs: bigint): Promise<void> {
    // Recover the graduated mint from the tx's token balances, retrying past the
    // processed→confirmed lag. The token mint is the non-WSOL one.
    for (let attempt = 0; attempt < MINT_LOOKUP_RETRIES; attempt++) {
      try {
        const mints = (await this.rpc.getTransactionTokenMints(signature)).filter(
          (m) => m !== WSOL_MINT && m !== PROGRAM_IDS.SYSTEM,
        );
        if (mints.length === 1) {
          const grad: FeedGraduation = {
            mint: mints[0]!,
            feedSource: 'helius-ws',
            receivedAtNs,
            venue: 'pumpswap',
            signature,
          };
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
    if (this.stopped) return;
    const wait = Math.min(this.reconnectBaseMs * 2 ** this.attempts, this.reconnectMaxMs);
    this.attempts++;
    this.reconnectTimer = setTimeout(() => this.connect(), wait);
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function describeError(ev: unknown): string {
  if (ev && typeof ev === 'object' && 'message' in ev) return String((ev as { message: unknown }).message);
  return 'unknown';
}
