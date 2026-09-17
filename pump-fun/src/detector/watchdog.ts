import type { FeedLiveness } from './feed.ts';

/**
 * Feed-liveness watchdog. Health used to flip only on a socket `close`, so a
 * half-open connection (no close, no data) was invisible for 35 minutes. The
 * watchdog tracks the last inbound frame per feed and decides when a feed has
 * been silent past its bound so the detector can force a reconnect.
 *
 * Pure, clock-injected; the Detector drives `tick()` from a 1 s interval.
 */
export interface FeedWatchdogOptions {
  /** Bound for feeds with a slot heartbeat (~400 ms ticks). */
  slotSilenceMs: number;
  /** Bound for feeds with no heartbeat (PumpPortal). */
  portalSilenceMs: number;
  /** PumpPortal missed this many consecutive graduations another feed saw → reconnect. */
  portalMissedGraduations: number;
  /** Upper bound on a feed's own reconnect backoff — used for the safety re-arm. */
  reconnectMaxMs?: number;
  now?: () => number;
}

export interface ReconnectDecision {
  feed: string;
  reason: string;
  silentMs: number;
}

interface FeedState {
  liveness: () => FeedLiveness;
  lastActivityAtMs: number;
  /** Set after a forced reconnect; cleared by markConnected. */
  reconnectingSinceMs: number | null;
}

export class FeedWatchdog {
  private readonly opts: FeedWatchdogOptions;
  private readonly now: () => number;
  private readonly feeds = new Map<string, FeedState>();
  private portalMissStreak = 0;

  constructor(opts: FeedWatchdogOptions) {
    this.opts = opts;
    this.now = opts.now ?? Date.now;
  }

  /** Arm a feed (lastActivity = now). `liveness` is read lazily — it can degrade at runtime. */
  register(name: string, liveness: FeedLiveness | (() => FeedLiveness)): void {
    const fn = typeof liveness === 'function' ? liveness : () => liveness;
    this.feeds.set(name, { liveness: fn, lastActivityAtMs: this.now(), reconnectingSinceMs: null });
  }

  /** Any inbound frame. */
  touch(name: string, atMs: number = this.now()): void {
    const f = this.feeds.get(name);
    if (!f) return;
    if (atMs > f.lastActivityAtMs) f.lastActivityAtMs = atMs;
  }

  /** After the detector forced a reconnect: suspend until the feed reports healthy again. */
  markReconnecting(name: string): void {
    const f = this.feeds.get(name);
    if (!f) return;
    f.reconnectingSinceMs = this.now();
    f.lastActivityAtMs = f.reconnectingSinceMs;
  }

  /** Feed reported healthy (subscription ack) — resets its silence clock. */
  markConnected(name: string, atMs: number = this.now()): void {
    const f = this.feeds.get(name);
    if (!f) return;
    f.reconnectingSinceMs = null;
    if (atMs > f.lastActivityAtMs) f.lastActivityAtMs = atMs;
  }

  /** Silence bound that applies to a feed right now. */
  boundMs(name: string): number | undefined {
    const f = this.feeds.get(name);
    if (!f) return undefined;
    return f.liveness() === 'slot' ? this.opts.slotSilenceMs : this.opts.portalSilenceMs;
  }

  /** PumpPortal missed a graduation another feed delivered. True (and reset) at the threshold. */
  portalMissed(): boolean {
    this.portalMissStreak++;
    if (this.portalMissStreak >= this.opts.portalMissedGraduations) {
      this.portalMissStreak = 0;
      return true;
    }
    return false;
  }

  /** PumpPortal delivered a graduation — streak resets. */
  portalHit(): void {
    this.portalMissStreak = 0;
  }

  /** Feeds silent past their bound. Each is marked reconnecting until markConnected. */
  tick(now: number = this.now()): ReconnectDecision[] {
    const out: ReconnectDecision[] = [];
    for (const [name, f] of this.feeds) {
      const bound = f.liveness() === 'slot' ? this.opts.slotSilenceMs : this.opts.portalSilenceMs;
      if (f.reconnectingSinceMs !== null) {
        // Safety valve: a feed whose own backoff loop got stuck would otherwise
        // stay suspended forever. Its reconnect() is a no-op while a timer is
        // pending, so re-arming cannot storm.
        const rearmMs = 2 * Math.max(bound, this.opts.reconnectMaxMs ?? 0);
        if (now - f.reconnectingSinceMs < rearmMs) continue;
        f.reconnectingSinceMs = null;
        f.lastActivityAtMs = now;
        continue;
      }
      const silentMs = now - f.lastActivityAtMs;
      if (silentMs > bound) {
        f.reconnectingSinceMs = now;
        f.lastActivityAtMs = now;
        out.push({ feed: name, reason: `silent ${silentMs}ms (bound ${bound}ms)`, silentMs });
      }
    }
    return out;
  }
}
