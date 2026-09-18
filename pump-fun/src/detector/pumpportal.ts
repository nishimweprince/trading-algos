import type { FeedGraduation, FeedLaunch, Venue } from '../core/types.ts';
import type { DetectionFeed, FeedActivity, FeedLiveness } from './feed.ts';
import { logger } from '../core/logger.ts';

/**
 * PumpPortal WebSocket feed (Section 4.1, fallback → default on free tier).
 * Subscribes to `subscribeMigration` and surfaces each migration as a
 * FeedGraduation. Free, purpose-built, and needs no Helius plan.
 *
 * The exact payload schema is treated defensively: the first few raw messages
 * are logged at debug so the operator can confirm field names against a live
 * stream, and unrecognized shapes are skipped rather than crashing the feed.
 *
 * PumpPortal sends no heartbeat, so liveness is 'silence': the detector's
 * watchdog reconnects after an absolute-silence bound or when other feeds
 * delivered graduations this one missed.
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
  /**
   * Also subscribe to new-token creation events (pre-graduation lane, S0).
   * Defaults to true; the detector gates it via
   * detector.pumpportalNewTokenEnabled.
   */
  newTokenEnabled?: boolean;
}

export class PumpPortalFeed implements DetectionFeed {
  readonly name = 'pumpportal';
  readonly liveness: FeedLiveness = 'silence';

  private readonly url: string;
  private readonly reconnectBaseMs: number;
  private readonly reconnectMaxMs: number;
  private readonly newTokenEnabled: boolean;
  private readonly log = logger.child({ mod: 'pumpportal' });

  private ws: WSLike | null = null;
  private stopped = false;
  private attempts = 0;
  private reconnectTimer: NodeJS.Timeout | null = null;
  /** Bumped per connection; stale sockets' events are ignored (see reconnect). */
  private generation = 0;
  private rawLogged = 0;
  private rawLaunchLogged = 0;
  /** Launches seen (for sampled info logging — launch flow is high-volume). */
  private launchCount = 0;

  private gradHandler: (g: FeedGraduation) => void = () => {};
  private launchHandler: (l: FeedLaunch) => void = () => {};
  private healthHandler: (healthy: boolean, detail?: string) => void = () => {};
  private activityHandler: (a: FeedActivity) => void = () => {};

  constructor(opts: PumpPortalOptions) {
    this.url = opts.url;
    this.reconnectBaseMs = opts.reconnectBaseMs;
    this.reconnectMaxMs = opts.reconnectMaxMs;
    this.newTokenEnabled = opts.newTokenEnabled ?? true;
  }

  onGraduation(handler: (g: FeedGraduation) => void): void {
    this.gradHandler = handler;
  }
  onLaunch(handler: (l: FeedLaunch) => void): void {
    this.launchHandler = handler;
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
    // `close`, and a synchronous close event must not schedule a second
    // reconnect from the stale handler.
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
    const gen = ++this.generation;

    ws.addEventListener('open', () => {
      if (gen !== this.generation) return;
      this.attempts = 0;
      this.log.info('connected — subscribing to migrations', { newToken: this.newTokenEnabled });
      try {
        ws.send(JSON.stringify({ method: 'subscribeMigration' }));
        // Second subscription on the same socket: creation events arrive on
        // the same stream and are routed by payload shape (see handleMessage).
        // If the method is ever rejected server-side it surfaces as a text
        // message without a mint and is skipped — migration flow is unaffected.
        if (this.newTokenEnabled) ws.send(JSON.stringify({ method: 'subscribeNewToken' }));
      } catch (err) {
        this.log.error('failed to send subscribe', { err });
      }
      this.healthHandler(true);
    });

    ws.addEventListener('message', (ev) => {
      if (gen !== this.generation) return;
      const receivedAtNs = process.hrtime.bigint();
      this.activityHandler({ atMs: Date.now(), kind: 'data' });
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

    const mint = typeof msg['mint'] === 'string' ? (msg['mint'] as string) : undefined;
    if (!mint) {
      this.log.debug('message without mint — skipped', { keys: Object.keys(msg) });
      return;
    }

    // Creation events share this socket once subscribeNewToken is active.
    // Ambiguity resolves toward the launch path on purpose: a misrouted
    // migration costs a missed screening, but a misrouted creation reaching
    // the graduation path could spend capital. Only the verified migration
    // shape (signature + pool) stays on the graduation path without a txType.
    const kind = classifyPayload(msg);
    if (kind === 'skip') {
      this.log.debug('message routed to neither path — skipped', { keys: Object.keys(msg) });
      return;
    }
    if (kind === 'launch') {
      this.handleLaunch(msg, mint, receivedAtNs);
      return;
    }

    // Log the first few raw payloads so field names can be verified live.
    if (this.rawLogged < 3) {
      this.rawLogged++;
      this.log.debug('raw migration payload', { raw: msg });
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

  private handleLaunch(msg: Record<string, unknown>, mint: string, receivedAtNs: bigint): void {
    // Log the first few raw creation payloads so field names (creator keys,
    // metadata keys) can be verified against the live stream.
    if (this.rawLaunchLogged < 3) {
      this.rawLaunchLogged++;
      this.log.debug('raw creation payload', { raw: msg });
    }
    const launch: FeedLaunch = { mint, feedSource: 'pumpportal', receivedAtNs };
    const name = asString(msg['name'], 64);
    if (name !== undefined) launch.name = name;
    const symbol = asString(msg['symbol'], 16);
    if (symbol !== undefined) launch.symbol = symbol;
    const uri = asString(msg['uri'] ?? msg['image_uri'] ?? msg['metadata_uri'], 256);
    if (uri !== undefined) launch.uri = uri;
    const creator = asString(
      msg['creator'] ?? msg['dev'] ?? msg['deployer'] ?? msg['traderPublicKey'],
      64,
    );
    if (creator !== undefined) launch.creator = creator;
    const signature = asString(msg['signature'], 128);
    if (signature !== undefined) launch.signature = signature;
    if (typeof msg['slot'] === 'number') launch.slot = msg['slot'];
    this.launchCount++;
    // Launch flow is high-volume: info-log every 100th, debug the rest.
    if (this.launchCount % 100 === 0) {
      this.log.info('launch flow', {
        count: this.launchCount,
        mint,
        hasSignature: launch.signature !== undefined,
      });
    } else {
      this.log.debug('launch', { mint });
    }
    this.launchHandler(launch);
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer) return;
    const delay = Math.min(this.reconnectBaseMs * 2 ** this.attempts, this.reconnectMaxMs);
    this.attempts++;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }
}

/**
 * Route a mint-bearing message. txType is authoritative when present
 * ('create' → launch, 'migrat*' → graduation, anything else — buy/sell/
 * trade frames — belongs to neither path and is skipped). Without a txType,
 * only the verified migration shape (signature + pool) stays on the
 * graduation path; everything else with a mint is a launch. A launch
 * misroute costs a missed screening; the reverse could spend capital.
 */
export function classifyPayload(msg: Record<string, unknown>): 'launch' | 'graduation' | 'skip' {
  const txType = msg['txType'];
  if (typeof txType === 'string') {
    const t = txType.toLowerCase();
    if (t.includes('migrat')) return 'graduation';
    if (t === 'create') return 'launch';
    return 'skip';
  }
  if (typeof msg['signature'] === 'string' && msg['pool'] !== undefined) return 'graduation';
  return 'launch';
}

function inferVenue(pool: unknown): Venue {
  if (typeof pool === 'string' && pool.toLowerCase().includes('raydium')) return 'raydium';
  return 'pumpswap';
}

function asString(value: unknown, maxLen: number): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value.slice(0, maxLen) : undefined;
}

function describeError(ev: unknown): string {
  if (ev && typeof ev === 'object' && 'message' in ev) return String((ev as { message: unknown }).message);
  return 'unknown';
}
