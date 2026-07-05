import type { FeedGraduation } from '../core/types.ts';
import type { DetectionFeed } from './feed.ts';
import { logger } from '../core/logger.ts';

/**
 * Yellowstone gRPC feed (Section 4.1, primary signal on paid infra).
 *
 * PLACEHOLDER — the streaming implementation is intentionally deferred: gRPC
 * requires a paid Helius/LaserStream plan and the `@triton-one/yellowstone-grpc`
 * client. The interface is fixed here so the detector treats it as a drop-in
 * once the plan and dependency are in place. Until then, enabling it surfaces a
 * clear, actionable error instead of silently degrading.
 */
export class GrpcFeed implements DetectionFeed {
  readonly name = 'grpc';
  private readonly log = logger.child({ mod: 'grpc' });
  private healthHandler: (healthy: boolean, detail?: string) => void = () => {};

  onGraduation(_handler: (g: FeedGraduation) => void): void {
    // Wired in when the streaming client is implemented.
    void _handler;
  }
  onHealth(handler: (healthy: boolean, detail?: string) => void): void {
    this.healthHandler = handler;
  }

  start(): void {
    const detail =
      'gRPC feed not implemented yet: requires a paid Helius/LaserStream plan and ' +
      'the @triton-one/yellowstone-grpc client. Set detector.grpcEnabled=false to ' +
      'run on the free PumpPortal feed.';
    this.log.error(detail);
    this.healthHandler(false, detail);
  }

  async stop(): Promise<void> {
    /* nothing to tear down */
  }
}
