import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { RpcClient } from '../core/rpc.ts';
import type { FeedGraduation, GraduationEvent } from '../core/types.ts';
import { logger } from '../core/logger.ts';
import { MintDedupe } from './dedupe.ts';
import { LatencyStats } from './latency.ts';
import type { DetectionFeed } from './feed.ts';
import { PumpPortalFeed } from './pumpportal.ts';
import { GrpcFeed } from './grpcStream.ts';

/**
 * Detection orchestrator (Section 4 / Phase 1). Wires every feed through
 * dedupe → on-chain confirmation → the event bus + decision log, tracks
 * detection latency, and debounces feed health into a single stream-health
 * signal the risk manager (Phase 5) will consume to gate new entries.
 */

const CONFIRM_MAX_ATTEMPTS = 6;
const CONFIRM_INTERVAL_MS = 500;

export interface DetectorDeps {
  config: Config;
  bus: TypedBus;
  repos: Repositories;
  /** Optional: without it, on-chain confirmation is skipped. */
  rpc?: RpcClient;
}

export class Detector {
  private readonly config: Config;
  private readonly bus: TypedBus;
  private readonly repos: Repositories;
  private readonly rpc: RpcClient | undefined;
  private readonly log = logger.child({ mod: 'detector' });

  private readonly dedupe: MintDedupe;
  private readonly latency = new LatencyStats();
  private readonly feeds: DetectionFeed[] = [];
  private readonly feedHealth = new Map<string, boolean>();

  private streamDown = false;
  private graceTimer: NodeJS.Timeout | null = null;

  constructor(deps: DetectorDeps) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.rpc = deps.rpc;
    this.dedupe = new MintDedupe(this.config.detector.dedupeTtlMs);
    this.feeds = this.buildFeeds();
  }

  private buildFeeds(): DetectionFeed[] {
    const d = this.config.detector;
    const feeds: DetectionFeed[] = [];
    if (d.pumpportalEnabled) {
      feeds.push(
        new PumpPortalFeed({
          url: this.config.rpc?.pumpportalWs ?? 'wss://pumpportal.fun/api/data',
          reconnectBaseMs: d.reconnectBaseMs,
          reconnectMaxMs: d.reconnectMaxMs,
        }),
      );
    }
    if (d.grpcEnabled) feeds.push(new GrpcFeed());
    return feeds;
  }

  async start(): Promise<void> {
    if (this.rpc) {
      const ok = await this.rpc.getHealth();
      this.log.info('rpc health check', { healthy: ok });
    } else if (this.config.detector.confirmOnChain) {
      this.log.warn('confirmOnChain enabled but no rpc.primaryHttp configured — graduations will be recorded unconfirmed');
    }

    for (const feed of this.feeds) {
      this.feedHealth.set(feed.name, false);
      feed.onGraduation((g) => void this.onFeedGraduation(g));
      feed.onHealth((healthy, detail) => this.onFeedHealth(feed.name, healthy, detail));
      feed.start();
    }
    this.log.info('detector started', {
      feeds: this.feeds.map((f) => f.name),
      confirmOnChain: this.config.detector.confirmOnChain && Boolean(this.rpc),
    });
  }

  async stop(): Promise<void> {
    if (this.graceTimer) clearTimeout(this.graceTimer);
    await Promise.all(this.feeds.map((f) => f.stop()));
  }

  private async onFeedGraduation(g: FeedGraduation): Promise<void> {
    if (!this.dedupe.firstSeen(g.mint)) {
      this.log.debug('duplicate graduation dropped', { mint: g.mint, feed: g.feedSource });
      return;
    }

    const confirm = await this.confirm(g);
    const latencyMs = Number(process.hrtime.bigint() - g.receivedAtNs) / 1e6;
    this.latency.add(latencyMs);

    const event: GraduationEvent = {
      mint: g.mint,
      venue: g.venue ?? 'pumpswap',
      poolAddress: g.poolAddress ?? '',
      slot: confirm.slot ?? g.slot ?? 0,
      feedSource: g.feedSource,
      receivedAtNs: g.receivedAtNs,
      detectionLatencyMs: latencyMs,
    };

    try {
      this.repos.recordGraduation(event);
    } catch (err) {
      this.log.error('failed to persist graduation', { mint: g.mint, err });
    }

    this.bus.emit('graduation', event);

    this.log.info('graduation detected', {
      mint: g.mint,
      venue: event.venue,
      feed: g.feedSource,
      confirmed: confirm.confirmed,
      slot: event.slot || undefined,
      // feed-receipt → on-chain-confirm round trip (true slot→receipt latency
      // needs the gRPC feed).
      confirmLatencyMs: Math.round(latencyMs),
    });

    if (this.latency.count % this.config.detector.latencyLogEveryN === 0) {
      this.log.info('detection latency stats', this.latency.summary());
    }

    this.bus.emit('alert', {
      level: 'info',
      message:
        `🎓 graduation ${short(g.mint)} via ${event.venue} ` +
        `(${g.feedSource}${confirm.confirmed ? ', confirmed' : ', unconfirmed'})`,
    });
  }

  /**
   * Confirm the migration landed on-chain (no tx error). Retries briefly since
   * `confirmed` commitment lags the WS signal. Returns confirmed=false (rather
   * than throwing) when unavailable so detection is still logged.
   */
  private async confirm(g: FeedGraduation): Promise<{ confirmed: boolean; slot?: number }> {
    if (!this.rpc || !this.config.detector.confirmOnChain || !g.signature) {
      return { confirmed: false };
    }
    for (let attempt = 0; attempt < CONFIRM_MAX_ATTEMPTS; attempt++) {
      try {
        const tx = await this.rpc.getTransactionConfirmation(g.signature);
        if (tx) {
          if (tx.err) {
            this.log.warn('migration tx failed on-chain', { mint: g.mint, err: tx.err });
            return { confirmed: false, slot: tx.slot };
          }
          return { confirmed: true, slot: tx.slot };
        }
      } catch (err) {
        this.log.debug('confirm attempt errored', { mint: g.mint, attempt, err });
      }
      await delay(CONFIRM_INTERVAL_MS);
    }
    this.log.warn('graduation not confirmed within budget', { mint: g.mint });
    return { confirmed: false };
  }

  private onFeedHealth(feed: string, healthy: boolean, detail?: string): void {
    this.feedHealth.set(feed, healthy);
    const anyHealthy = [...this.feedHealth.values()].some(Boolean);

    if (anyHealthy) {
      if (this.graceTimer) {
        clearTimeout(this.graceTimer);
        this.graceTimer = null;
      }
      if (this.streamDown) {
        this.streamDown = false;
        this.bus.emit('streamHealth', { source: 'detector', healthy: true });
        this.bus.emit('alert', { level: 'info', message: '✅ detection feed recovered' });
        this.log.info('detection feeds healthy again');
      }
      return;
    }

    // All feeds down — wait out the grace window before declaring an outage
    // (Section 4.2 / Section 8: alert if down > streamDownGraceMs).
    if (!this.streamDown && !this.graceTimer) {
      this.graceTimer = setTimeout(() => {
        this.streamDown = true;
        this.graceTimer = null;
        this.bus.emit('streamHealth', {
          source: 'detector',
          healthy: false,
          ...(detail ? { detail } : {}),
        });
        this.bus.emit('alert', {
          level: 'warn',
          message: `⚠️ detection feed down >${Math.round(this.config.risk.streamDownGraceMs / 1000)}s — new entries paused`,
        });
        this.log.warn('all detection feeds down past grace window', { detail });
      }, this.config.risk.streamDownGraceMs);
    }
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function short(mint: string): string {
  return mint.length > 10 ? `${mint.slice(0, 4)}…${mint.slice(-4)}` : mint;
}
