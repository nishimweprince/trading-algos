import type { FeedGraduation } from '../core/types.ts';

/**
 * Common interface every detection feed implements (Section 4). Making the
 * detector feed-agnostic means PumpPortal (free, default) and Yellowstone gRPC
 * (paid, opt-in) are interchangeable — adding gRPC later is a drop-in, not a
 * rewrite.
 */
export interface DetectionFeed {
  readonly name: string;
  /** Begin streaming. Non-blocking; reconnects internally. */
  start(): void;
  /** Stop streaming and release resources. */
  stop(): Promise<void>;
  /** Register a handler for each raw graduation surfaced by this feed. */
  onGraduation(handler: (g: FeedGraduation) => void): void;
  /** Register a handler for feed health transitions. */
  onHealth(handler: (healthy: boolean, detail?: string) => void): void;
}
