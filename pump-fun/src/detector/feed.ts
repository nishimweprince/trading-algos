import type { FeedGraduation, FeedLaunch } from '../core/types.ts';

/** Any inbound frame on a feed — data, subscription ack, slot tick, ping. */
export interface FeedActivity {
  /** Date.now() at receipt. */
  atMs: number;
  /** 'slot' = heartbeat tick from a slot subscription (carries `slot`). */
  kind: 'slot' | 'data';
  slot?: number;
}

/**
 * Which silence bound the watchdog applies to a feed: slot-subscribed feeds
 * tick every ~400 ms (`liveness.slotSilenceMs`); feeds with no heartbeat
 * (PumpPortal) get the absolute-silence bound (`liveness.portalSilenceMs`).
 */
export type FeedLiveness = 'slot' | 'silence';

/**
 * Common interface every detection feed implements (Section 4). Making the
 * detector feed-agnostic means PumpPortal (free, default) and LaserStream
 * (paid, opt-in) are interchangeable — adding gRPC later is a drop-in, not a
 * rewrite.
 */
export interface DetectionFeed {
  readonly name: string;
  /** Which watchdog silence bound applies (may degrade at runtime). */
  readonly liveness: FeedLiveness;
  /** Begin streaming. Non-blocking; reconnects internally. */
  start(): void;
  /** Stop streaming and release resources. */
  stop(): Promise<void>;
  /** Register a handler for each raw graduation surfaced by this feed. */
  onGraduation(handler: (g: FeedGraduation) => void): void;
  /**
   * Register a handler for each raw token-creation (pre-graduation launch)
   * surfaced by this feed. Optional: feeds without a launch subscription
   * simply omit it, and the detector skips launch wiring for them.
   */
  onLaunch?(handler: (l: FeedLaunch) => void): void;
  /** Register a handler for feed health transitions. */
  onHealth(handler: (healthy: boolean, detail?: string) => void): void;
  /** Register a handler fired on EVERY inbound frame (acks and slot ticks included). */
  onActivity(handler: (a: FeedActivity) => void): void;
  /**
   * Tear down the established transport and reconnect through the feed's own
   * backoff loop (attempts reset). Must NOT wait for the dead socket's close
   * event — a half-open connection may never deliver one. No-op while a
   * reconnect is already pending or nothing is connected.
   */
  reconnect(reason: string): void;
}
