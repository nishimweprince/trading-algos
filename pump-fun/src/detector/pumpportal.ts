import type { FeedGraduation, Venue } from '../core/types.ts';
import type { DetectionFeed } from './feed.ts';
import { logger } from '../core/logger.ts';

/**
 * PumpPortal WebSocket feed (Section 4.1, fallback → default on free tier).
 * Subscribes to `subscribeMigration` and surfaces each migration as a
 * FeedGraduation. Free, purpose-built, and needs no Helius plan.
 *
 * The exact payload schema is treated defensively: the first few raw messages
 * are logged at debug so the operator can confirm field names against a live
 * stream, and unrecognized shapes are skipped rather than crashing the feed.
 */

interface WSLike {
  send(data: string): void;
  close(): void;
  readyState: number;
  addEventListener(type: string, cb: (ev: { data?: unknown }) => void): void;
}
type WSCtor = new (url: string) => WSLike;

const OPEN = 1;

export interface PumpPortalOptions {
  url: string;
  reconnectBaseMs: number;
  reconnectMaxMs: number;
}

export class PumpPortalFeed implements DetectionFeed {
  readonly name = 'pumpportal';

  private readonly url: string;
  private readonly reconnectBaseMs: number;
  private readonly reconnectMaxMs: number;
  private readonly log = logger.child({ mod: 'pumpportal' });

  private ws: WSLike | null = null;
  private stopped = false;
  private attempts = 0;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private rawLogged = 0;

  private gradHandler: (g: FeedGraduation) => void = () => {};
  private healthHandler: (healthy: boolean, detail?: string) => void = () => {};

  constructor(opts: PumpPortalOptions) {
    this.url = opts.url;
    this.reconnectBaseMs = opts.reconnectBaseMs;
    this.reconnectMaxMs = opts.reconnectMaxMs;
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
      this.log.error('global WebSocket unavailable — Node 22+ required for PumpPortal feed');
      this.healthHandler(false, 'no WebSocket');
      return;
    }

    let ws: WSLike;
    try {
      ws = new Ctor(this.url);
    } catch (err) {
      this.log.error('failed to construct WebSocket', { err });
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.addEventListener('open', () => {
      this.attempts = 0;
      this.log.info('connected — subscribing to migrations');
      try {
        ws.send(JSON.stringify({ method: 'subscribeMigration' }));
      } catch (err) {
        this.log.error('failed to send subscribe', { err });
      }
      this.healthHandler(true);
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
      // The 'close' handler drives reconnect; just surface the error.
      this.log.warn('websocket error', { detail: describeError(ev) });
    });
  }

  private handleMessage(data: unknown, receivedAtNs: bigint): void {
    const text = typeof data === 'string' ? data : String(data);
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(text) as Record<string, unknown>;
    } catch {
      this.log.debug('non-JSON message', { text: text.slice(0, 200) });
      return;
    }

    // Subscription acknowledgements have a `message` field and no `mint`.
    if (typeof msg['message'] === 'string' && !msg['mint']) {
      this.log.info('subscription ack', { message: msg['message'] });
      return;
    }

    // Log the first few raw payloads so field names can be verified live.
    if (this.rawLogged < 3) {
      this.rawLogged++;
      this.log.debug('raw migration payload', { raw: msg });
    }

    const mint = typeof msg['mint'] === 'string' ? (msg['mint'] as string) : undefined;
    if (!mint) {
      this.log.debug('message without mint — skipped', { keys: Object.keys(msg) });
      return;
    }

    const grad: FeedGraduation = {
      mint,
      feedSource: 'pumpportal',
      receivedAtNs,
      venue: inferVenue(msg['pool']),
      raw: msg,
    };
    const sig = msg['signature'];
    if (typeof sig === 'string') grad.signature = sig;
    // `pool` may be a venue label ("pump"/"raydium") or, on some payloads, an
    // address. Only treat base58-length values as an address.
    const pool = msg['pool'];
    if (typeof pool === 'string' && pool.length >= 32) grad.poolAddress = pool;

    this.gradHandler(grad);
  }

  private scheduleReconnect(): void {
    if (this.stopped) return;
    const delay = Math.min(this.reconnectBaseMs * 2 ** this.attempts, this.reconnectMaxMs);
    this.attempts++;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }
}

function inferVenue(pool: unknown): Venue {
  if (typeof pool === 'string' && pool.toLowerCase().includes('raydium')) return 'raydium';
  return 'pumpswap';
}

function describeError(ev: unknown): string {
  if (ev && typeof ev === 'object' && 'message' in ev) return String((ev as { message: unknown }).message);
  return 'unknown';
}
